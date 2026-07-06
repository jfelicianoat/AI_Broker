from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    Devuelve el número de filas eliminadas. `older_than_days <= 0` desactiva la poda.
    """
    if older_than_days <= 0:
        return 0
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    cursor = db.execute(
        "DELETE FROM events WHERE task_id IN ("
        "SELECT id FROM tasks WHERE status IN ('completed', 'failed', 'cancelled') "
        "AND updated_at < ?)",
        (cutoff,),
    )
    return int(cursor.rowcount or 0)


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
