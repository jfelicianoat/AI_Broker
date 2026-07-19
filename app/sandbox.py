"""Sandbox de ejecución de código: contenedores Docker desechables.

El broker delega en un contenedor efímero la ejecución de código no confiable
(generado por el modelo). Fronteras deliberadas, no configurables:

- Sin red (`--network none`): por el sandbox pasan prompts y documentos
  posiblemente confidenciales; sin red no hay exfiltración posible.
- Sin ficheros del host: no se monta ningún volumen; el rootfs va read-only
  y el directorio de trabajo es un tmpfs que muere con el contenedor.
- Sin privilegios: usuario nobody, --cap-drop ALL, no-new-privileges y tope
  de procesos, memoria y CPU.

El límite de tiempo tiene dos capas: `timeout` dentro del contenedor (mata el
proceso Python) y un plazo asyncio fuera con margen; si el CLI muere sin
limpiar, se remata con `docker rm -f` por nombre.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.config import BrokerConfig, SandboxConfig

logger = logging.getLogger("ai_broker.sandbox")

# Margen sobre el timeout interno: arranque del contenedor + pull ya cacheado.
_STARTUP_MARGIN_SECONDS = 25.0


class SandboxError(RuntimeError):
    """Fallo de infraestructura del sandbox (Docker ausente, parado...).

    El código que falla DENTRO del contenedor no es un SandboxError: su
    stderr/exit code son el resultado que se devuelve al modelo."""


class SandboxExecutor:
    def __init__(self, config: BrokerConfig) -> None:
        # Se guarda la config completa del broker (no la sección) para leer
        # sandbox.* en vivo: el panel de Configuración reemplaza el atributo
        # `sandbox` del BrokerConfig compartido y el cambio aplica sin reiniciar.
        self.config = config

    @property
    def settings(self) -> SandboxConfig:
        return self.config.sandbox

    async def run_python(self, code: str) -> str:
        """Ejecuta código Python en un contenedor desechable y devuelve su salida."""
        if not self.settings.enabled:
            raise SandboxError("el sandbox de código está desactivado (sandbox.enabled)")
        if not code.strip():
            raise SandboxError("no hay código que ejecutar")
        container_name = f"ai-broker-sbx-{uuid4().hex[:12]}"
        command = self._docker_command(container_name)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise SandboxError(
                f"docker no encontrado ({self.settings.docker_path}); "
                "instala Docker Desktop o corrige sandbox.docker_path"
            ) from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(code.encode("utf-8")),
                timeout=self.settings.timeout_seconds + _STARTUP_MARGIN_SECONDS,
            )
        except asyncio.TimeoutError:
            process.kill()
            await self._force_remove(container_name)
            raise SandboxError(
                f"la ejecución superó el plazo de {self.settings.timeout_seconds}s y fue cancelada"
            ) from None

        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0 and self._daemon_error(err_text):
            raise SandboxError(
                "Docker no responde (¿Docker Desktop arrancado?): " + err_text.strip()[:300]
            )
        return self._format_result(process.returncode or 0, out_text, err_text)

    def _docker_command(self, container_name: str) -> list[str]:
        settings = self.settings
        memory = f"{settings.memory_mb}m"
        return [
            settings.docker_path, "run", "--rm", "-i",
            "--name", container_name,
            "--network", "none",
            "--memory", memory, "--memory-swap", memory,
            "--cpus", str(settings.cpus),
            "--pids-limit", str(settings.pids_limit),
            "--read-only",
            "--tmpfs", "/work:rw,size=64m",
            "--tmpfs", "/tmp:rw,size=32m",
            "--workdir", "/work",
            "--user", "65534:65534",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            settings.image,
            "timeout", f"{int(settings.timeout_seconds)}s",
            "python", "-I", "-B", "-",
        ]

    def _format_result(self, returncode: int, stdout: str, stderr: str) -> str:
        limit = self.settings.max_output_chars
        parts: list[str] = []
        if returncode == 124:
            # Código de salida de `timeout`: el proceso agotó su plazo interno.
            parts.append(
                f"ERROR: el código superó el límite de {int(self.settings.timeout_seconds)}s y fue interrumpido."
            )
        elif returncode != 0:
            parts.append(f"(el proceso terminó con exit code {returncode})")
        if stdout.strip():
            parts.append(stdout.strip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        text = "\n\n".join(parts) or "(ejecución sin salida; usa print() para devolver resultados)"
        if len(text) > limit:
            text = text[:limit] + "\n[...salida truncada...]"
        return text

    @staticmethod
    def _daemon_error(stderr: str) -> bool:
        lowered = stderr.lower()
        return any(hint in lowered for hint in (
            "error during connect", "cannot connect to the docker daemon",
            "docker daemon is not running", "open //./pipe/docker",
        ))

    async def _force_remove(self, container_name: str) -> None:
        """Mejor esfuerzo: si el CLI murió sin limpiar, el contenedor no debe quedar vivo."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.docker_path, "rm", "-f", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=15)
        except (OSError, asyncio.TimeoutError):
            logger.warning(
                "sandbox.cleanup_failed",
                extra={"event": "sandbox.cleanup_failed", "container": container_name},
            )
