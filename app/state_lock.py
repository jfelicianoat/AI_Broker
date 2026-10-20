"""Exclusión entre el servicio y las copias/restauraciones de su estado."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class StateLock:
    """Bloqueo del SO: se libera incluso si el proceso termina abruptamente.

    El archivo se conserva: borrarlo crearía dos bloqueos independientes sobre
    distintos archivos. Su existencia no significa que el servicio esté activo.
    """

    def __init__(self, database_path: str | Path, *, allow_incomplete_restore: bool = False) -> None:
        self.in_memory = str(database_path) == ":memory:"
        database = Path(database_path).resolve()
        self.path = database.with_name(database.name + ".state.lock")
        self.restore_marker = database.with_name(database.name + ".restore-in-progress")
        self.allow_incomplete_restore = allow_incomplete_restore
        self._handle: BinaryIO | None = None

    def __enter__(self) -> StateLock:
        # SQLite en memoria pertenece a esa conexión y no puede ser compartida
        # por otro proceso ni respaldada como un archivo.
        if self.in_memory:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError as error:
            handle.close()
            raise RuntimeError(
                "El estado está en uso. Detén el broker antes de crear o restaurar "
                "su copia de seguridad; no arranques otra instancia sobre la misma base."
            ) from error
        self._handle = handle
        if self.restore_marker.exists() and not self.allow_incomplete_restore:
            self.__exit__()
            raise RuntimeError("Hay una restauración interrumpida. Repite la restauración antes de arrancar el broker.")
        return self

    def __exit__(self, *args: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
