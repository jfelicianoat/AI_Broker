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
# Plazo para recoger al CLI de Docker después de matarlo: sin esperarlo queda
# un proceso sin recoger; con un plazo, un kill atascado no cuelga la tarea.
_REAP_TIMEOUT_SECONDS = 5.0
_READ_CHUNK_BYTES = 65536


def _output_limit_bytes(max_output_chars: int) -> int:
    """Bytes que se guardan por flujo (stdout/stderr) antes de cortar.

    4 bytes por carácter es el máximo de UTF-8; el margen cubre la cabecera
    que añade _format_result. Lo que pase de aquí nunca llega a la memoria
    del broker: el límite de memoria del contenedor no acota su stdout."""
    return max_output_chars * 4 + _READ_CHUNK_BYTES


async def _reap(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=_REAP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("sandbox.reap_timeout", extra={"event": "sandbox.reap_timeout"})


async def _communicate_capped(
    process: asyncio.subprocess.Process, data: bytes, limit_bytes: int,
) -> tuple[bytes, bytes, bool]:
    """Como process.communicate(data), pero sin acumular más de limit_bytes
    por flujo. Si un flujo se pasa, se mata el proceso y se descarta el resto
    hasta EOF. Devuelve (stdout, stderr, se_paso_del_limite)."""
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    exceeded = False

    async def feed() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # El proceso terminó (o lo matamos) antes de leer todo el código:
            # lo mismo que ignora communicate().
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def read(stream: asyncio.StreamReader) -> bytes:
        nonlocal exceeded
        chunks: list[bytes] = []
        kept = 0
        while True:
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return b"".join(chunks)
            if kept >= limit_bytes:
                continue  # ya cortado: se descarta hasta que el proceso muera
            room = limit_bytes - kept
            chunks.append(chunk[:room])
            kept += min(len(chunk), room)
            if len(chunk) > room and not exceeded:
                exceeded = True
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    _, stdout, stderr = await asyncio.gather(feed(), read(process.stdout), read(process.stderr))
    await process.wait()
    return stdout, stderr, exceeded


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
        # Matar el CLI de `docker run` no para el contenedor: hay que rematarlo.
        return await self._collect(process, code, remove_container=container_name)

    async def _collect(
        self, process: asyncio.subprocess.Process, code: str, *, remove_container: str | None,
    ) -> str:
        """Alimenta el código por stdin y recoge la salida con tope de bytes.

        remove_container: contenedor que hay que rematar si se corta la
        ejecución (None cuando el llamante ya lo limpia en su finally)."""
        limit = _output_limit_bytes(self.settings.max_output_chars)
        try:
            stdout, stderr, exceeded = await asyncio.wait_for(
                _communicate_capped(process, code.encode("utf-8"), limit),
                timeout=self.settings.timeout_seconds + _STARTUP_MARGIN_SECONDS,
            )
        except asyncio.TimeoutError:
            await _reap(process)
            if remove_container is not None:
                await self._force_remove(remove_container)
            raise SandboxError(
                f"la ejecución superó el plazo de {self.settings.timeout_seconds}s y fue cancelada"
            ) from None

        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        if exceeded:
            # Es un resultado del código del modelo, no un fallo de
            # infraestructura (ver SandboxError): se devuelve como texto, con
            # el principio de la salida para que el modelo vea qué imprimía.
            if remove_container is not None:
                await self._force_remove(remove_container)
            logger.warning(
                "sandbox.output_limit_exceeded",
                extra={"event": "sandbox.output_limit_exceeded", "limit_bytes": limit},
            )
            head = self._format_result(0, out_text, err_text)
            return (
                f"ERROR: el código escribió más de {limit} bytes de salida y fue interrumpido. "
                "Reduce lo que imprimes.\n\n" + head
            )
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
                # `.name` de "../../" es "..": hay que descartarlo aparte.
                safe_name = PurePosixPath(dest_name).name
                if safe_name in ("", ".", ".."):
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
        # El contenedor lo retira el finally de _run_python_with_files.
        return await self._collect(process, code, remove_container=None)

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
            await _reap(process)
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
