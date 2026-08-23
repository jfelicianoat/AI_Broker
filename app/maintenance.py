from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # Solo para tipar: el grafo de importación en ejecución no cambia.
    from app.config import BrokerConfig
    from app.providers.ollama import OllamaLifecycleManager
    from app.repository import TaskRepository

BACKUP_FORMAT_VERSION = "ai-broker-backup-v1"
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "broker.db"
ARTIFACTS_PREFIX = "artifacts/"


@dataclass(frozen=True)
class BackupResult:
    path: Path
    sha256: str
    files: int
    size_bytes: int


def create_state_backup(
    *,
    database_path: str | Path,
    artifacts_root: str | Path,
    output_path: str | Path,
) -> BackupResult:
    """Create an atomic zip backup containing SQLite state and task artifacts."""
    database = Path(database_path)
    artifacts = Path(artifacts_root)
    output = Path(output_path)
    if not database.exists():
        raise FileNotFoundError(f"database not found: {database}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=output.parent) as temp_dir:
        temp_root = Path(temp_dir)
        db_snapshot = temp_root / DATABASE_NAME
        _sqlite_backup(database, db_snapshot)

        records: list[dict[str, Any]] = []
        records.append(_file_record(db_snapshot, DATABASE_NAME))
        artifact_files = _artifact_files(artifacts)
        for source, archive_name in artifact_files:
            records.append(_file_record(source, archive_name))

        manifest = {
            "format": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": DATABASE_NAME,
            "artifacts_prefix": ARTIFACTS_PREFIX,
            "files": records,
        }

        temp_zip = temp_root / f".{output.name}.tmp"
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_snapshot, DATABASE_NAME)
            for source, archive_name in artifact_files:
                archive.write(source, archive_name)
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            )
        os.replace(temp_zip, output)

    return BackupResult(
        path=output,
        sha256=_sha256_file(output),
        files=len(records),
        size_bytes=output.stat().st_size,
    )


def verify_state_backup(backup_path: str | Path) -> dict[str, Any]:
    backup = Path(backup_path)
    with zipfile.ZipFile(backup, "r") as archive:
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("format") != BACKUP_FORMAT_VERSION:
            raise ValueError("unsupported backup format")
        names = set(archive.namelist())
        for record in manifest.get("files", []):
            archive_name = record["path"]
            if archive_name not in names:
                raise ValueError(f"missing file in backup: {archive_name}")
            with archive.open(archive_name) as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            if digest != record["sha256"]:
                raise ValueError(f"checksum mismatch: {archive_name}")
        return manifest


def restore_state_backup(
    *,
    backup_path: str | Path,
    database_path: str | Path,
    artifacts_root: str | Path,
    replace: bool = False,
) -> None:
    """Restore a verified backup. Existing targets require replace=True."""
    backup = Path(backup_path)
    database = Path(database_path)
    artifacts = Path(artifacts_root)
    manifest = verify_state_backup(backup)

    if database.exists() and not replace:
        raise FileExistsError(f"database exists: {database}")
    artifact_targets = [
        artifacts / Path(record["path"]).relative_to(ARTIFACTS_PREFIX)
        for record in manifest["files"]
        if str(record["path"]).startswith(ARTIFACTS_PREFIX)
    ]
    existing_artifacts = [target for target in artifact_targets if target.exists()]
    if existing_artifacts and not replace:
        raise FileExistsError(f"artifact exists: {existing_artifacts[0]}")

    database.parent.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=database.parent) as temp_dir:
        temp_root = Path(temp_dir)
        with zipfile.ZipFile(backup, "r") as archive:
            archive.extract(DATABASE_NAME, temp_root)
            temp_database = temp_root / DATABASE_NAME
            _validate_sqlite(temp_database)
            os.replace(temp_database, database)

            for record in manifest["files"]:
                archive_name = str(record["path"])
                if not archive_name.startswith(ARTIFACTS_PREFIX):
                    continue
                target = artifacts / Path(archive_name).relative_to(ARTIFACTS_PREFIX)
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = temp_root / archive_name
                archive.extract(archive_name, temp_root)
                os.replace(extracted, target)
    _cleanup_empty_dirs(artifacts)


def _sqlite_backup(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source)) as source_conn:
        with closing(sqlite3.connect(destination)) as destination_conn:
            source_conn.backup(destination_conn)
            result = destination_conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise ValueError("SQLite backup failed integrity_check")


def _validate_sqlite(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise ValueError("restored SQLite database failed integrity_check")


def _artifact_files(root: Path) -> list[tuple[Path, str]]:
    if not root.exists():
        return []
    result: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result.append((path, f"{ARTIFACTS_PREFIX}{path.relative_to(root).as_posix()}"))
    return result


def _file_record(path: Path, archive_name: str) -> dict[str, Any]:
    return {
        "path": archive_name,
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cleanup_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def prune_terminal_task_events(db: Any, *, older_than_days: int) -> int:
    """Borra eventos de tareas terminales antiguas para acotar el crecimiento de la tabla.

    El flujo de consenso genera decenas de eventos de progreso por tarea; sin poda,
    `events` degrada progresivamente todas las consultas (conexión SQLite única).
    `prompt.compressed` queda exento: es el único registro del prompt que viajó
    realmente a los modelos (una fila por tarea) y debe durar lo que la tarea.
    Devuelve el número de filas eliminadas. `older_than_days <= 0` desactiva la poda.
    """
    if older_than_days <= 0:
        return 0
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    cursor = db.execute(
        "DELETE FROM events WHERE event_type != 'prompt.compressed' AND task_id IN ("
        "SELECT id FROM tasks WHERE status IN ('completed', 'failed', 'cancelled') "
        "AND updated_at < ?)",
        (cutoff,),
    )
    return int(cursor.rowcount or 0)


def prune_ingested_files(db: Any, files_root: str | Path, *, older_than_days: int) -> int:
    """Borra ficheros ingeridos (original + Markdown + fila) en estado terminal.

    Solo ready/failed: una conversión en curso nunca se poda. Una tarea encolada
    que referencie un fichero podado fallará en el despacho con
    ATTACHED_FILE_NOT_FOUND — retención corta con colas largas es mala idea.
    Desactivada por defecto (older_than_days <= 0): borrar documentos del
    usuario debe ser decisión explícita del operador.
    """
    if older_than_days <= 0:
        return 0
    import shutil
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    rows = db.query_all(
        "SELECT id FROM ingested_files WHERE status IN ('ready', 'failed') AND updated_at < ?",
        (cutoff,),
    )
    root = Path(files_root)
    removed = 0
    for row in rows:
        file_dir = root / row["id"]
        try:
            if file_dir.exists():
                shutil.rmtree(file_dir)
        except OSError:
            continue
        db.execute("DELETE FROM ingested_files WHERE id = ?", (row["id"],))
        removed += 1
    return removed


def prune_terminal_task_artifacts(db: Any, artifacts_root: str | Path, *, older_than_days: int) -> int:
    """Borra artefactos en disco (y sus filas) de tareas terminales antiguas.

    Sin retención el directorio de artefactos crece sin límite hasta llenar el disco.
    Desactivada por defecto (older_than_days <= 0): borrar salidas del usuario debe
    ser una decisión explícita del operador.
    """
    if older_than_days <= 0:
        return 0
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    rows = db.query_all(
        "SELECT id, path FROM artifacts WHERE task_id IN ("
        "SELECT id FROM tasks WHERE status IN ('completed', 'failed', 'cancelled') "
        "AND updated_at < ?)",
        (cutoff,),
    )
    removed = 0
    for row in rows:
        artifact_path = Path(row["path"])
        try:
            if artifact_path.exists():
                artifact_path.unlink()
        except OSError:
            continue
        db.execute("DELETE FROM artifacts WHERE id = ?", (row["id"],))
        removed += 1
    root = Path(artifacts_root)
    if root.exists():
        _cleanup_empty_dirs(root)
    return removed


@dataclass(frozen=True)
class BackfillResult:
    """Recuento de una reclasificación de `model_invocations.task_type`."""

    pending: int
    classified: int
    reconstructed: int
    skipped: int

    def as_dict(self) -> dict[str, int]:
        return {
            "pending": self.pending,
            "classified": self.classified,
            "reconstructed": self.reconstructed,
            "skipped": self.skipped,
        }


def backfill_invocation_task_type(db: Any, *, dry_run: bool = False) -> BackfillResult:
    """Clasifica retroactivamente las invocaciones anteriores a `task_type`.

    `load_model_stats` descarta las filas con task_type NULL: sin clasificar no
    se sabe a qué segmento pertenecen. Eso deja el enrutamiento adaptativo
    arrancando desde cero pese a tener meses de historial en la base, y con
    routing.min_invocations sin alcanzar, el score de todos los modelos es
    neutro. Reclasificar recupera esa evidencia aplicando el mismo
    clasificador determinista a la petición ya guardada.

    Cuando la petición guardada no valida contra el esquema actual (el formato
    ha evolucionado) se reconstruye una equivalente mínima a partir del prompt.
    Puede desviar la estimación de contexto de una tarea con adjuntos, así que
    se cuenta aparte. Si no hay ni prompt, la fila se deja como estaba.
    """
    from app.schemas import TaskCreateRequest
    from app.task_classifier import classify_task_type

    rows = db.query_all(
        "SELECT i.id AS invocation_id, t.request_json AS request_json "
        "FROM model_invocations i JOIN tasks t ON t.id = i.task_id "
        "WHERE i.task_type IS NULL"
    )
    classified = reconstructed = skipped = 0
    for row in rows:
        try:
            payload = json.loads(row["request_json"] or "{}")
        except json.JSONDecodeError:
            skipped += 1
            continue
        try:
            request = TaskCreateRequest.model_validate(payload)
            was_reconstructed = False
        except Exception:
            prompt = (payload.get("content") or {}).get("prompt")
            if not prompt:
                skipped += 1
                continue
            try:
                request = TaskCreateRequest.model_validate({
                    "idempotency_key": f"backfill:{row['invocation_id']}",
                    "content": {"prompt": prompt},
                })
            except Exception:
                skipped += 1
                continue
            was_reconstructed = True
        if not dry_run:
            db.execute(
                "UPDATE model_invocations SET task_type = ? WHERE id = ?",
                (classify_task_type(request), row["invocation_id"]),
            )
        classified += 1
        reconstructed += int(was_reconstructed)
    return BackfillResult(
        pending=len(rows), classified=classified,
        reconstructed=reconstructed, skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Descarga de modelos locales por inactividad
# ---------------------------------------------------------------------------

# Techo del sondeo: por encima de esto la descarga llegaría bastante más tarde
# de lo que promete el plazo configurado, y una consulta cada medio minuto no
# se nota en ninguna parte.
_IDLE_POLL_CEILING_SECONDS = 30.0


def idle_poll_seconds(idle_seconds: float) -> float:
    """Cada cuánto mirar la máquina, deducido del plazo en vez de configurado.

    Una fracción del plazo para que la descarga no se retrase mucho más de lo
    prometido, con techo para no sondear en balde cuando el plazo son horas y
    con suelo de un segundo para que un plazo corto no se convierta en una
    espera activa.
    """
    if idle_seconds <= 0:
        return _IDLE_POLL_CEILING_SECONDS
    return max(1.0, min(_IDLE_POLL_CEILING_SECONDS, idle_seconds / 4))


class IdleUnloadTracker:
    """Cuenta cuánto lleva el broker sin trabajo y decide cuándo soltar.

    Va aparte del bucle porque lo único interesante es la decisión, y con el
    reloj inyectado se prueba entera sin esperar un segundo real.

    Empieza a contar en el momento en que se construye: los modelos que dejó
    cargados una ejecución anterior también se reclaman, pero no antes de que
    el broker lleve el plazo completo levantado sin que nadie le pida nada.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._last_busy = now()
        self._reclaimed = False

    def should_unload(self, *, busy: bool, idle_seconds: float) -> bool:
        """Anota el estado actual y responde si toca descargar.

        El plazo se pasa en cada llamada en vez de guardarse al construir: la
        configuración se edita desde el panel con el broker en marcha, y un
        valor leído una sola vez convertiría ese cambio en un reinicio
        obligatorio.

        El reloj se actualiza aunque la función esté desactivada (plazo 0). Es
        deliberado: así, al activarla, el broker sabe desde cuándo lleva
        realmente parado en vez de empezar a contar desde el clic.
        """
        now = self._now()
        if busy:
            self._last_busy = now
            self._reclaimed = False
            return False
        if idle_seconds <= 0 or self._reclaimed:
            return False
        return now - self._last_busy >= idle_seconds

    def mark_reclaimed(self) -> None:
        """La memoria ya está devuelta: no se vuelve a intentar hasta que entre
        trabajo nuevo. Sin esto, cada sondeo posterior le pediría al runtime una
        lista que ya sabemos vacía, para siempre."""
        self._reclaimed = True


async def idle_unload_loop(
    repository: TaskRepository,
    lifecycle: OllamaLifecycleManager,
    config: BrokerConfig,
    stop: asyncio.Event,
    *,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Devuelve la memoria de los modelos locales cuando el broker lleva un rato
    sin nada que hacer.

    Es el contrapeso de `processing.unload_after_task=false`. Descargar al
    terminar cada tarea hace que dos tareas seguidas con el mismo modelo paguen
    la carga dos veces —y, peor, deja el ranking ciego al modelo caliente,
    porque cuando la siguiente tarea enruta ya no hay nada cargado que preferir.
    No descargar nunca tiene el defecto simétrico: la máquina se queda con un
    modelo dentro toda la noche. Este bucle pone el plazo entre ambos extremos.

    "Sin nada que hacer" se mide con `has_unfinished_task`, que es más ancho que
    los leases del proveedor y tiene que serlo por dos motivos. Dentro de un
    mixture hay instantes sin ningún lease vivo —entre dos olas, mientras
    arbitra— y descargar ahí recargaría el mismo modelo dos frases después. Y
    una tarea en cola todavía sin turno es trabajo que llega: soltar la memoria
    delante de ella sería regalarla para volver a pedirla enseguida. El precio
    de esa anchura es que una tarea parada mucho tiempo esperando dependencias o
    herramientas mantiene el modelo residente; se prefiere ese error al
    contrario, porque este solo cuesta memoria y el otro cuesta esperas.

    La configuración se relee en cada vuelta: los dos ajustes se cambian desde
    el panel con el broker en marcha.
    """
    logger = logging.getLogger("ai_broker.maintenance")
    tracker = IdleUnloadTracker(now=now)
    while not stop.is_set():
        idle_seconds = config.processing.idle_unload_seconds
        # Con descarga al terminar cada tarea no queda nunca nada que reclamar:
        # el plazo se anula en vez de gastar una ronda de sondeos inútil.
        if config.processing.unload_after_task:
            idle_seconds = 0.0
        try:
            # La consulta se hace también con la función desactivada, y cuesta
            # un LIMIT 1 cada medio minuto: es lo que mantiene el reloj honesto
            # para cuando se active.
            busy = await asyncio.to_thread(repository.has_unfinished_task)
            if tracker.should_unload(busy=busy, idle_seconds=idle_seconds):
                unloaded = await lifecycle.unload_idle()
                tracker.mark_reclaimed()
                if unloaded:
                    logger.info(
                        "maintenance.idle_models_unloaded",
                        extra={
                            "event": "maintenance.idle_models_unloaded",
                            "models": unloaded,
                            "idle_seconds": idle_seconds,
                        },
                    )
        except Exception:
            # Ni el runtime caído ni la BD ocupada pueden matar el bucle: sin él
            # la memoria dejaría de recuperarse hasta el siguiente reinicio, y
            # nadie lo notaría hasta la primera tarea grande que no cupiera.
            logger.warning(
                "maintenance.idle_unload_failed",
                exc_info=True,
                extra={"event": "maintenance.idle_unload_failed"},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=idle_poll_seconds(idle_seconds))
        except TimeoutError:
            pass
