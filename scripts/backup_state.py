from __future__ import annotations

import argparse
import json

from app.maintenance import create_state_backup, restore_state_backup, verify_state_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Broker durable state backup/restore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="Create an atomic zip backup")
    backup.add_argument("--database", default="state/broker.db")
    backup.add_argument("--artifacts", default="state/tasks")
    backup.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify", help="Verify backup manifest and checksums")
    verify.add_argument("--backup", required=True)

    restore = subparsers.add_parser("restore", help="Restore a verified backup")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--database", default="state/broker.db")
    restore.add_argument("--artifacts", default="state/tasks")
    restore.add_argument("--replace", action="store_true", help="Allow replacing existing DB/artifacts")

    args = parser.parse_args()
    if args.command == "backup":
        result = create_state_backup(
            database_path=args.database,
            artifacts_root=args.artifacts,
            output_path=args.output,
        )
        print(json.dumps({
            "path": str(result.path),
            "sha256": result.sha256,
            "files": result.files,
            "size_bytes": result.size_bytes,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "verify":
        manifest = verify_state_backup(args.backup)
        print(json.dumps({
            "format": manifest["format"],
            "created_at": manifest["created_at"],
            "files": len(manifest["files"]),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "restore":
        restore_state_backup(
            backup_path=args.backup,
            database_path=args.database,
            artifacts_root=args.artifacts,
            replace=args.replace,
        )
        print(json.dumps({"restored": True}, ensure_ascii=False, indent=2))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
