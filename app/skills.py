"""Skills técnicas del broker para la estrategia agent.

Capacidades genéricas y neutrales al dominio (buscar en la web, leer una URL):
la orquestación de negocio sigue perteneciendo a las apps cliente. Cada skill
devuelve TEXTO PLANO que se entrega al modelo como resultado de tool; ese
contenido es siempre datos externos no confiables, nunca instrucciones — el
system prompt del agente lo deja explícito.
"""
from __future__ import annotations

import ast
import html
import ipaddress
import json
import logging
import operator
import re
import socket
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("ai_broker.skills")

SKILL_TIMEOUT_SECONDS = 20.0
_SEARCH_URL = "https://lite.duckduckgo.com/lite/"
_MAX_FETCH_BYTES = 400_000
_MAX_RESULT_CHARS = 8_000
_USER_AGENT = "ai-broker-agent/1.0 (+local)"

# Definiciones OpenAI-compatible de cada skill (lo que ve el modelo).
SKILL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Busca en la web (DuckDuckGo) y devuelve una lista de resultados "
                "con título, URL y fragmento. Útil para información posterior a tu "
                "fecha de corte o datos actuales."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Consulta de búsqueda"},
                },
                "required": ["query"],
            },
        },
    },
    "fetch_url": {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Descarga una URL pública (http/https) y devuelve su contenido "
                "como texto plano, truncado. Útil para leer una página encontrada "
                "con web_search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL completa a leer"},
                },
                "required": ["url"],
            },
        },
    },
    "calculator": {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": (
                "Evalúa una expresión aritmética y devuelve el resultado exacto. "
                "Úsalo en vez de calcular mentalmente. Soporta + - * / // % ** y "
                "paréntesis. Ejemplo: '(1234 * 5.5) / 3'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Expresión aritmética"},
                },
                "required": ["expression"],
            },
        },
    },
    "current_datetime": {
        "type": "function",
        "function": {
            "name": "current_datetime",
            "description": (
                "Devuelve la fecha y hora actuales (UTC y hora local del servidor). "
                "Úsalo cuando necesites saber 'hoy', 'ahora' o la fecha actual."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "run_code": {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": (
                "Ejecuta código Python en un sandbox aislado y devuelve su salida. "
                "El entorno es efímero y sin estado: cada llamada empieza de cero, "
                "SIN acceso a red ni a ficheros del host, y con límite de tiempo y "
                "memoria. Imprime con print() todo lo que quieras recuperar. Útil "
                "para cálculos complejos, procesar texto/datos del prompt y "
                "verificar código antes de entregarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Código Python completo a ejecutar"},
                },
                "required": ["code"],
            },
        },
    },
}

# Operadores permitidos en la calculadora: aritmética pura, sin nombres ni llamadas.
_CALC_BINOPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_CALC_UNARYOPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos, ast.USub: operator.neg,
}
_CALC_MAX_POW = 1000


class SkillError(RuntimeError):
    """Fallo de una skill; su mensaje viaja al modelo como resultado de tool."""


def skill_definitions(names: Sequence[str]) -> list[dict[str, Any]]:
    return [SKILL_DEFINITIONS[name] for name in names if name in SKILL_DEFINITIONS]


def _reject_private_hosts(url: str) -> str:
    """Guardia SSRF: el agente no puede leer servicios internos de la máquina
    o de la LAN (dashboard del propio broker incluido)."""
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise SkillError(f"Esquema no permitido: {parsed.scheme or 'vacío'} (solo http/https)")
    host = parsed.host
    if not host:
        raise SkillError("URL sin host")
    try:
        addresses = {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except OSError as error:
        raise SkillError(f"No se pudo resolver {host}: {error}") from error
    for address in addresses:
        candidate = ipaddress.ip_address(address.split("%", 1)[0])
        if not candidate.is_global:
            raise SkillError(f"Host no público rechazado: {host}")
    return str(parsed)


def _html_to_text(markup: str) -> str:
    without_blocks = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", markup, flags=re.IGNORECASE | re.DOTALL
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_blocks)
    text = html.unescape(without_tags)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*(\s*\n\s*)+", "\n\n", text).strip()


async def _run_web_search(
    arguments: dict[str, Any], transport: httpx.AsyncBaseTransport | None
) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise SkillError("web_search requiere el argumento query")
    async with httpx.AsyncClient(
        timeout=SKILL_TIMEOUT_SECONDS, transport=transport,
        headers={"User-Agent": _USER_AGENT}, follow_redirects=True,
    ) as client:
        response = await client.post(_SEARCH_URL, data={"q": query})
        response.raise_for_status()
        body = response.text
    # DuckDuckGo lite: enlaces de resultado (result-link) seguidos del snippet.
    links = re.findall(
        r"<a[^>]+href=\"([^\"]+)\"[^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>",
        body, flags=re.IGNORECASE | re.DOTALL,
    )
    snippets = re.findall(
        r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
        body, flags=re.IGNORECASE | re.DOTALL,
    )
    results = []
    for index, (url, title) in enumerate(links[:5]):
        snippet = _html_to_text(snippets[index]) if index < len(snippets) else ""
        results.append({"title": _html_to_text(title), "url": html.unescape(url), "snippet": snippet})
    if not results:
        return f"Sin resultados para: {query}"
    return json.dumps(results, ensure_ascii=False, indent=1)


async def _run_fetch_url(
    arguments: dict[str, Any], transport: httpx.AsyncBaseTransport | None
) -> str:
    raw_url = str(arguments.get("url") or "").strip()
    if not raw_url:
        raise SkillError("fetch_url requiere el argumento url")
    url = _reject_private_hosts(raw_url)
    async with httpx.AsyncClient(
        timeout=SKILL_TIMEOUT_SECONDS, transport=transport,
        headers={"User-Agent": _USER_AGENT}, follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                received += len(chunk)
                if received >= _MAX_FETCH_BYTES:
                    break
            body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    content_type = response.headers.get("content-type", "")
    text = _html_to_text(body) if "html" in content_type.lower() else body.strip()
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n[...contenido truncado...]"
    return text or "(respuesta vacía)"


def _eval_calc_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise SkillError("solo se permiten números")
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BINOPS:
        if isinstance(node.op, ast.Pow):
            exponent = _eval_calc_node(node.right)
            if abs(exponent) > _CALC_MAX_POW:
                raise SkillError("exponente demasiado grande")
        return _CALC_BINOPS[type(node.op)](_eval_calc_node(node.left), _eval_calc_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARYOPS:
        return _CALC_UNARYOPS[type(node.op)](_eval_calc_node(node.operand))
    raise SkillError("expresión no permitida (solo aritmética)")


def _run_calculator(arguments: dict[str, Any]) -> str:
    expression = str(arguments.get("expression") or "").strip()
    if not expression:
        raise SkillError("calculator requiere el argumento expression")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise SkillError(f"expresión inválida: {error.msg}") from error
    try:
        value = _eval_calc_node(tree.body)
    except ZeroDivisionError as error:
        raise SkillError("división por cero") from error
    if value == int(value):
        return str(int(value))
    return repr(round(value, 12))


def _run_current_datetime() -> str:
    now_utc = datetime.now(timezone.utc)
    local = now_utc.astimezone()
    return json.dumps({
        "utc": now_utc.isoformat(timespec="seconds"),
        "local": local.isoformat(timespec="seconds"),
        "weekday": now_utc.strftime("%A"),
    }, ensure_ascii=False)


async def _run_code(arguments: dict[str, Any], sandbox: Any) -> str:
    if sandbox is None:
        raise SkillError(
            "el sandbox de código no está habilitado en este broker "
            "(sandbox.enabled en la configuración)"
        )
    code = str(arguments.get("code") or "")
    if not code.strip():
        raise SkillError("run_code requiere el argumento code")
    from app.sandbox import SandboxError

    try:
        return await sandbox.run_python(code)
    except SandboxError as error:
        raise SkillError(str(error)) from error


async def run_skill(
    name: str,
    arguments: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sandbox: Any | None = None,
) -> str:
    """Ejecuta una skill y devuelve texto para el modelo. Los fallos se
    devuelven como texto de error (el agente puede reaccionar), nunca como
    excepción hacia el coordinador — salvo skill desconocida, que es un bug."""
    if name not in SKILL_DEFINITIONS:
        raise SkillError(f"Skill no soportada: {name}")
    try:
        if name == "web_search":
            return await _run_web_search(arguments, transport)
        if name == "fetch_url":
            return await _run_fetch_url(arguments, transport)
        if name == "calculator":
            return _run_calculator(arguments)
        if name == "run_code":
            return await _run_code(arguments, sandbox)
        return _run_current_datetime()
    except SkillError as error:
        return f"ERROR de {name}: {error}"
    except httpx.HTTPError as error:
        return f"ERROR de {name}: fallo de red: {error}"
    except Exception as error:  # noqa: BLE001 — una skill jamás tira la tarea.
        logger.warning(
            "skill.unexpected_error",
            extra={"event": "skill.unexpected_error", "skill": name, "detail": str(error)},
        )
        return f"ERROR de {name}: {type(error).__name__}: {error}"
