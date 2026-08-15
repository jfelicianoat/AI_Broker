"""Cliente MCP sobre stdio: herramientas de terceros para el bucle agéntico.

Un servidor MCP se lanza como subproceso y se habla con él por JSON-RPC 2.0
sobre stdin/stdout, con framing por líneas. El broker hace `initialize`, pide
`tools/list` una vez y a partir de ahí ejecuta `tools/call` como ejecuta
cualquier otra skill.

Tres fronteras que no se negocian aquí:

- **Solo stdio.** No hay transporte HTTP. El broker no depende de ninguna nube
  para funcionar, y un servidor remoto convertiría cada herramienta en una
  salida de datos menos visible que un proveedor cloud.
- **La frontera de datos la declara el operador**, servidor por servidor, y es
  obligatoria (`MCPServerConfig.data_boundary`). Deducirla del transporte
  mentiría en el caso que importa: un servidor stdio que por dentro llama a una
  API de internet.
- **Los nombres van con prefijo** (`mcp__<servidor>__<tool>`). Sin él, un
  servidor podría publicar `web_search` y suplantar a la skill del broker o a
  una tool del cliente en la misma llamada al modelo.

Lo que devuelve una herramienta MCP es TEXTO para el modelo, y es tan poco
confiable como el resultado de cualquier skill: el system prompt del agente ya
lo marca como datos externos, nunca instrucciones.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from app.config import MCPConfig, MCPServerConfig

logger = logging.getLogger("ai_broker.mcp")

# Prefijo de espacio de nombres. Doble guion bajo para que no colisione con un
# nombre de herramienta legítimo que lleve guiones bajos sueltos.
TOOL_PREFIX = "mcp__"
_MAX_RESULT_CHARS = 8_000
# Una línea de stdout que no es JSON no es un error del protocolo: muchos
# servidores escriben avisos por ahí. Se ignoran hasta este tope para no leer
# indefinidamente si el proceso escupe basura sin parar.
_MAX_NOISE_LINES = 50
_ENV_REFERENCE_PREFIX = "env:"


class MCPError(RuntimeError):
    """Fallo hablando con un servidor MCP; su mensaje viaja al modelo."""


def qualified_name(server_id: str, tool: str) -> str:
    return f"{TOOL_PREFIX}{server_id}__{tool}"


def split_qualified_name(name: str) -> tuple[str, str] | None:
    """(servidor, herramienta) de un nombre cualificado, o None si no lo es."""
    if not name.startswith(TOOL_PREFIX):
        return None
    rest = name[len(TOOL_PREFIX):]
    server, separator, tool = rest.partition("__")
    if not separator or not server or not tool:
        return None
    return server, tool


@dataclass
class MCPTool:
    server_id: str
    name: str
    description: str
    schema: dict[str, Any]
    data_boundary: str

    def definition(self) -> dict[str, Any]:
        """Definición OpenAI-compatible, que es lo que ve el modelo."""
        return {
            "type": "function",
            "function": {
                "name": qualified_name(self.server_id, self.name),
                "description": self.description,
                "parameters": self.schema or {"type": "object", "properties": {}},
            },
        }


@dataclass
class _Server:
    config: MCPServerConfig
    process: asyncio.subprocess.Process | None = None
    tools: list[MCPTool] = field(default_factory=list)
    started: bool = False
    error: str | None = None
    _counter: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MCPRegistry:
    """Servidores MCP configurados, sus herramientas y sus llamadas.

    Los procesos se arrancan de forma perezosa —la primera tarea que pida ese
    servidor— y se mantienen vivos: el handshake cuesta, y una tarea agéntica
    llama varias veces seguidas. Un servidor que no arranca no tumba nada: se
    queda marcado con su error y sus herramientas simplemente no existen.
    """

    def __init__(self, config: MCPConfig) -> None:
        self.config = config
        self._servers: dict[str, _Server] = {
            server.id: _Server(config=server)
            for server in config.servers
            if server.enabled
        }

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self._servers)

    def server_ids(self) -> list[str]:
        return list(self._servers)

    def data_boundary(self, server_id: str) -> str | None:
        server = self._servers.get(server_id)
        return server.config.data_boundary if server is not None else None

    def egress_server_ids(self) -> list[str]:
        return sorted(
            server_id for server_id, server in self._servers.items()
            if server.config.data_boundary == "egress"
        )

    async def tools_for(self, server_ids: list[str]) -> list[MCPTool]:
        """Herramientas de esos servidores, arrancándolos si hace falta.

        Un servidor caído aporta cero herramientas y no interrumpe a los demás:
        que una integración opcional esté rota no puede impedir que la tarea
        corra con lo que sí funciona.
        """
        if not self.config.enabled:
            return []
        tools: list[MCPTool] = []
        for server_id in server_ids:
            server = self._servers.get(server_id)
            if server is None:
                continue
            try:
                await self._ensure_started(server)
            except MCPError as error:
                logger.warning(
                    "mcp.server_unavailable",
                    extra={"event": "mcp.server_unavailable", "server": server_id, "detail": str(error)},
                )
                continue
            tools.extend(server.tools)
        return tools

    async def call(self, server_id: str, tool: str, arguments: dict[str, Any]) -> str:
        server = self._servers.get(server_id)
        if server is None:
            raise MCPError(f"El servidor MCP '{server_id}' no está configurado o está desactivado")
        await self._ensure_started(server)
        payload = await self._request(
            server, "tools/call", {"name": tool, "arguments": arguments or {}},
            timeout=server.config.timeout_seconds,
        )
        return _render_result(payload)

    async def aclose(self) -> None:
        for server in self._servers.values():
            process = server.process
            server.process = None
            server.started = False
            if process is None or process.returncode is not None:
                continue
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError, OSError):
                with_kill = getattr(process, "kill", None)
                if with_kill is not None:
                    try:
                        process.kill()
                    except (ProcessLookupError, OSError):
                        pass

    # ------------------------------------------------------------------ interno

    async def _ensure_started(self, server: _Server) -> None:
        async with server._lock:
            if server.started and server.process is not None and server.process.returncode is None:
                return
            server.started = False
            server.tools = []
            await self._spawn(server)
            try:
                await asyncio.wait_for(
                    self._handshake(server), timeout=self.config.startup_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                server.error = "el servidor no completó el arranque a tiempo"
                raise MCPError(f"{server.config.id}: {server.error}") from error
            server.started = True
            server.error = None
            logger.info("mcp.server_ready", extra={
                "event": "mcp.server_ready", "server": server.config.id,
                "tools": len(server.tools),
            })

    async def _spawn(self, server: _Server) -> None:
        command = shutil.which(server.config.command) or server.config.command
        environment = {**os.environ}
        for key, value in server.config.env.items():
            if value.startswith(_ENV_REFERENCE_PREFIX):
                # `env:NOMBRE` toma el valor del entorno del broker: así una
                # credencial no acaba escrita en el YAML.
                resolved = os.environ.get(value[len(_ENV_REFERENCE_PREFIX):])
                if resolved is None:
                    continue
                environment[key] = resolved
            else:
                environment[key] = value
        try:
            server.process = await asyncio.create_subprocess_exec(
                command, *server.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
        except (OSError, ValueError) as error:
            server.error = f"no se pudo lanzar «{server.config.command}»: {error}"
            raise MCPError(f"{server.config.id}: {server.error}") from error

    async def _handshake(self, server: _Server) -> None:
        await self._request(server, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ai-broker", "version": "1.0"},
        }, timeout=self.config.startup_timeout_seconds)
        await self._notify(server, "notifications/initialized", {})
        listed = await self._request(
            server, "tools/list", {}, timeout=self.config.startup_timeout_seconds,
        )
        server.tools = [
            MCPTool(
                server_id=server.config.id,
                name=str(item.get("name") or ""),
                description=str(item.get("description") or "")[:1024],
                schema=item.get("inputSchema") or item.get("input_schema") or {},
                data_boundary=server.config.data_boundary,
            )
            for item in (listed.get("tools") or [])
            if item.get("name")
        ]

    async def _notify(self, server: _Server, method: str, params: dict[str, Any]) -> None:
        await self._write(server, {"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(
        self, server: _Server, method: str, params: dict[str, Any], *, timeout: float,
    ) -> dict[str, Any]:
        server._counter += 1
        request_id = server._counter
        await self._write(server, {
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        })
        try:
            payload = await asyncio.wait_for(self._read_response(server, request_id), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise MCPError(
                f"{server.config.id}: sin respuesta a {method} en {timeout:.0f} s"
            ) from error
        if "error" in payload:
            detail = payload.get("error") or {}
            raise MCPError(
                f"{server.config.id}: {detail.get('message') or 'error del servidor MCP'}"
            )
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def _write(self, server: _Server, message: dict[str, Any]) -> None:
        process = server.process
        if process is None or process.stdin is None:
            raise MCPError(f"{server.config.id}: el proceso no está disponible")
        line = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as error:
            server.started = False
            raise MCPError(f"{server.config.id}: el servidor cerró la conexión") from error

    async def _read_response(self, server: _Server, request_id: int) -> dict[str, Any]:
        """Lee hasta encontrar la respuesta a `request_id`.

        Por el mismo canal llegan notificaciones del servidor y, en la práctica,
        líneas que no son JSON (avisos que el servidor escribe en stdout en vez
        de stderr). Ni unas ni otras son un fallo del protocolo: se descartan.
        """
        process = server.process
        if process is None or process.stdout is None:
            raise MCPError(f"{server.config.id}: el proceso no está disponible")
        noise = 0
        while noise <= _MAX_NOISE_LINES:
            raw = await process.stdout.readline()
            if not raw:
                server.started = False
                raise MCPError(f"{server.config.id}: el servidor terminó sin responder")
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                noise += 1
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
            noise += 1
        raise MCPError(f"{server.config.id}: demasiadas líneas ilegibles antes de responder")


def _render_result(payload: dict[str, Any]) -> str:
    """Convierte el resultado de `tools/call` en el texto que lee el modelo."""
    blocks = payload.get("content")
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif block.get("type") == "resource":
                resource = block.get("resource") or {}
                text = resource.get("text") or resource.get("uri") or ""
                if text:
                    parts.append(str(text))
            elif block.get("type"):
                # Imágenes y audio: el bucle agéntico entrega texto, así que se
                # nombra lo que llegó en vez de inventar una transcripción.
                parts.append(f"[contenido {block['type']} no textual omitido]")
    text = "\n\n".join(part for part in parts if part).strip()
    if not text:
        text = json.dumps(payload, ensure_ascii=False)[:_MAX_RESULT_CHARS]
    if payload.get("isError"):
        return f"ERROR: {text[:_MAX_RESULT_CHARS]}"
    return text[:_MAX_RESULT_CHARS]
