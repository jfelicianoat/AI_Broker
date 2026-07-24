"""Reclasifica las invocaciones anteriores a la columna `task_type`.

Sin esto, `load_model_stats` descarta todo el historial previo (task_type
NULL) y la selección adaptativa arranca en frío aunque la base guarde meses
de invocaciones.
"""
from __future__ import annotations

import argparse
import json

from app.db import Database
from app.maintenance import backfill_invocation_task_type


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AI Broker: clasifica retroactivamente model_invocations.task_type"
    )
    parser.add_argument("--database", default="state/broker.db")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Cuenta lo que se reclasificaría sin escribir en la base",
    )
    args = parser.parse_args()

    db = Database(args.database)
    try:
        result = backfill_invocation_task_type(db, dry_run=args.dry_run)
    finally:
        db.close()
    print(json.dumps({"dry_run": args.dry_run, **result.as_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
