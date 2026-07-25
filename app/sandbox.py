"""Sandbox de ejecución de código: contenedores Docker desechables.

El broker delega en un contenedor efímero la ejecución de código no confiable
(generado por el modelo). Fronteras deliberadas, no configurables:

- Sin red (`--network none`): por el sandbox pasan prompts y documentos
  posiblemente confidenciales; sin red no hay exfiltración posible.
- Sin directorios del host: nunca se monta un bind mount; el rootfs va
  read-only. El directorio de trabajo (/work) es normalmente un tmpfs que
  muere con el contenedor; cuando la tarea tiene adjuntos tabulares que
  stagear (ver run_python(files=...)), /work pasa a ser un volumen de Docker
  efímero respaldado por tmpfs (nunca por disco) en vez de un --tmpfs
  directo, porque `docker cp` rechaza copiar contra un tmpfs montado en un
  contenedor --read-only (comprobado empíricamente) — el volumen intermedio
  sigue siendo RAM-only y muere con el contenedor, solo cambia el mecanismo.
- Sin privilegios: usuario nobody, --cap-drop ALL, no-new-privileges y tope
  de procesos, memoria y CPU.

El límite de tiempo tiene dos capas: `timeout` dentro del contenedor (mata el
proceso Python) y un plazo asyncio fuera con margen; si el CLI muere sin
limpiar, se remata con `docker rm -f` (y `docker volume rm -f` si aplica).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePosixPath
from uuid import uuid4

from app.config import BrokerConfig, SandboxConfig

logger = logging.getLogger("ai_broker.sandbox")

# Margen sobre el timeout interno: arranque del contenedor + pull ya cacheado.
_STARTUP_MARGIN_SECONDS = 25.0
# Subcomandos de preparación (volume create/create/start/cp): son metadata,
# no ejecución de código del modelo, así que un plazo corto y fijo basta.
_MANAGEMENT_TIMEOUT_SECONDS = 20.0


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

    async def run_python(self, code: str, files: dict[str, Path] | None = None) -> str:
        """Ejecuta código Python en un contenedor desechable y devuelve su salida.

        files (opcional): nombre-en-sandbox -> ruta local a copiar dentro de
        /work/attachments/<nombre> ANTES de ejecutar el código (ver
        app.ingestion.service.staged_attachment_name — el nombre debe venir
        ya calculado por esa función, con file_id de prefijo, para no chocar
        entre adjuntos). Sin files, el comportamiento es idéntico al camino
        clásico de un único `docker run`.
        """
        if not self.settings.enabled:
            raise SandboxError("el sandbox de código está desactivado (sandbox.enabled)")
        if not code.strip():
            raise SandboxError("no hay código que ejecutar")
        if files:
            return await self._run_python_with_files(code, files)
        return await self._run_python_plain(code)

    async def _run_python_plain(self, code: str) -> str:
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

    async def _run_python_with_files(self, code: str, files: dict[str, Path]) -> str:
        """Variante con adjuntos: create -> start (detached) -> cp por
        fichero -> attach (stdin=code). `docker cp` exige el contenedor ya
        arrancado; por eso no puede ser un único `docker run` como el camino
        clásico. Limpieza explícita siempre (contenedor + volumen), sin
        depender de --rm."""
        container_name = f"ai-broker-sbx-{uuid4().hex[:12]}"
        volume_name = f"ai-broker-sbx-vol-{uuid4().hex[:12]}"
        try:
            await self._run_management_command(self._docker_volume_create_command(volume_name))
            await self._run_management_command(self._docker_create_command(container_name, volume_name))
            await self._run_management_command([self.settings.docker_path, "start", container_name])
            # `docker cp` no crea directorios intermedios: sin esto, copiar a
            # /work/attachments/<nombre> falla si esa carpeta no existe aún.
            await self._run_management_command([
                self.settings.docker_path, "exec", container_name, "mkdir", "-p", "/work/attachments",
            ])
            for dest_name, local_path in files.items():
                # Defensa extra contra travesía de rutas: solo el nombre, sin componentes de ruta.
                safe_name = PurePosixPath(dest_name).name
                if not safe_name:
                    continue
                await self._run_management_command([
                    self.settings.docker_path, "cp", str(local_path),
                    f"{container_name}:/work/attachments/{safe_name}",
                ])
            return await self._attach_and_run(container_name, code)
        finally:
            await self._force_remove(container_name, volume_name=volume_name)

    async def _attach_and_run(self, container_name: str, code: str) -> str:
        """Alimenta el código por stdin a un contenedor ya arrancado (start
        detached previo) y espera su salida — el equivalente de _run_python_plain
        una vez que el contenedor ya existe y tiene ficheros copiados dentro."""
        command = [self.settings.docker_path, "attach", container_name]
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

    async def _run_management_command(self, command: list[str]) -> None:
        """Subcomandos cortos de preparación (volume create/create/start/cp):
        un fallo aquí es infraestructura (Docker ausente/parado o el propio
        adjunto), nunca el código del modelo."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise SandboxError(
                f"docker no encontrado ({self.settings.docker_path}); "
                "instala Docker Desktop o corrige sandbox.docker_path"
            ) from error
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_MANAGEMENT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            process.kill()
            raise SandboxError(
                f"la preparación del sandbox superó {_MANAGEMENT_TIMEOUT_SECONDS}s: {' '.join(command)}"
            ) from None
        err_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            if self._daemon_error(err_text):
                raise SandboxError(
                    "Docker no responde (¿Docker Desktop arrancado?): " + err_text.strip()[:300]
                )
            raise SandboxError(f"fallo preparando el sandbox: {err_text.strip()[:300]}")

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

    def _docker_volume_create_command(self, volume_name: str) -> list[str]:
        """Volumen efímero respaldado por tmpfs (nunca disco): mismo espíritu
        RAM-only que --tmpfs, pero copiable con `docker cp` estando el
        contenedor --read-only (--tmpfs directo no lo permite, verificado)."""
        return [
            self.settings.docker_path, "volume", "create",
            "--driver", "local",
            "--opt", "type=tmpfs", "--opt", "device=tmpfs",
            "--opt", f"o=size={self.settings.work_volume_mb}m,mode=1777",
            volume_name,
        ]

    def _docker_create_command(self, container_name: str, volume_name: str) -> list[str]:
        """Como _docker_command, pero `create` (sin arrancar) y con /work
        montado desde volume_name en vez de --tmpfs directo. Mismas fronteras
        de aislamiento (sin red, sin privilegios, rootfs read-only); sigue
        sin haber ningún bind mount a un directorio del host."""
        settings = self.settings
        memory = f"{settings.memory_mb}m"
        return [
            settings.docker_path, "create", "-i",
            "--name", container_name,
            "--network", "none",
            "--memory", memory, "--memory-swap", memory,
            "--cpus", str(settings.cpus),
            "--pids-limit", str(settings.pids_limit),
            "--read-only",
            "--mount", f"source={volume_name},destination=/work",
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

    async def _force_remove(self, container_name: str, *, volume_name: str | None = None) -> None:
        """Mejor esfuerzo: si el CLI murió sin limpiar (o el flujo con
        adjuntos no usa --rm, ver _run_python_with_files), ni el contenedor
        ni su volumen deben quedar vivos."""
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.docker_path, "rm", "-f", "-v", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=15)
        except (OSError, asyncio.TimeoutError):
            logger.warning(
                "sandbox.cleanup_failed",
                extra={"event": "sandbox.cleanup_failed", "container": container_name},
            )
        if volume_name is None:
            return
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.docker_path, "volume", "rm", "-f", volume_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=15)
        except (OSError, asyncio.TimeoutError):
            logger.warning(
                "sandbox.cleanup_failed",
                extra={"event": "sandbox.cleanup_failed", "volume": volume_name},
            )
