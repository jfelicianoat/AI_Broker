"""Proceso hijo que ejecuta la fase pesada de una conversión.

Se invoca como `python -m app.ingestion.worker`, recibe el trabajo por STDIN y
publica el resultado por STDOUT. Existe por una razón muy concreta: un hilo de
Python no se puede matar, y un proceso sí. Con `asyncio.to_thread`, cancelar
una conversión solo cancelaba la espera — Docling seguía dentro del broker
consumiendo CPU hasta terminar, y un OOM suyo se llevaba el broker por delante.

Protocolo, líneas JSON por STDOUT:

- `{"event": "progress", "data": {...}}`  cero o más, para lo que debe verse
  antes de acabar (la duración de un vídeo largo).
- `{"event": "result", "data": {...}}`    exactamente una si todo fue bien.
- `{"event": "error", "code": ..., "message": ...}` si falló.

STDERR queda libre para el ruido de las librerías (torch y docling escriben
avisos por su cuenta), que así no contamina el canal de datos.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from app.ingestion import engines
from app.ingestion.conversion import ConversionInput, convert_file, load_settings


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as error:
        _emit({"event": "error", "code": "WORKER_BAD_INPUT", "message": str(error)})
        return 2
    try:
        source = ConversionInput(**job["source"])
        settings = load_settings(job["ingestion"])
    except Exception as error:  # noqa: BLE001 - cualquier fallo de contrato aquí
        _emit({"event": "error", "code": "WORKER_BAD_INPUT", "message": str(error)[:2000]})
        return 2
    try:
        result = convert_file(
            source, settings, on_progress=lambda data: _emit({"event": "progress", "data": data}),
        )
    except engines.EngineMissing as error:
        _emit({"event": "error", "code": "ENGINE_MISSING", "message": str(error)[:2000]})
        return 1
    except Exception as error:  # noqa: BLE001 - se traduce a fallo de conversión
        _emit({"event": "error", "code": "CONVERSION_FAILED", "message": str(error)[:2000]})
        return 1
    _emit({"event": "result", "data": result.to_json()})
    return 0


if __name__ == "__main__":  # pragma: no cover - punto de entrada del hijo
    raise SystemExit(main())
