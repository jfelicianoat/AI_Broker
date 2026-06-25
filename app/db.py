from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


class Database:
    def __init__(self, path: str | Path, journal_mode: str = "WAL") -> None:
        self.path = Path(path)
        self.journal_mode = journal_mode
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA journal_mode = {journal_mode}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                request_id TEXT,
                idempotency_key TEXT,
                request_hash TEXT,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                queue_position INTEGER,
                progress_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error_json TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS consensus_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                strategy TEXT NOT NULL,
                preset TEXT NOT NULL,
                selection_mode TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                rubric_json TEXT,
                limits_json TEXT NOT NULL,
                consensus_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS stages (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES consensus_runs(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                stage_type TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                dependencies_json TEXT NOT NULL DEFAULT '[]',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS model_invocations (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES consensus_runs(id) ON DELETE CASCADE,
                stage_id TEXT REFERENCES stages(id) ON DELETE SET NULL,
                role TEXT NOT NULL,
                provider TEXT NOT NULL,
                deployment TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_hash TEXT,
                output_json TEXT,
                tokens_input INTEGER DEFAULT 0,
                tokens_output INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                latency_ms REAL,
                started_at TEXT,
                completed_at TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES consensus_runs(id) ON DELETE CASCADE,
                invocation_id TEXT REFERENCES model_invocations(id) ON DELETE SET NULL,
                artifact_type TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_queue
            ON tasks(status, queue_position, priority, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_events_task
            ON events(task_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_updated
            ON tasks(status, updated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_invocations_task
            ON model_invocations(task_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_invocations_usage
            ON model_invocations(status, updated_at, provider)
            """,
        ]
        with self._lock:
            for statement in statements:
                self._conn.execute(statement)
            task_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "idempotency_key" not in task_columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
            if "request_hash" not in task_columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN request_hash TEXT")
            invocation_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(model_invocations)").fetchall()
            }
            if "started_at" not in invocation_columns:
                self._conn.execute("ALTER TABLE model_invocations ADD COLUMN started_at TEXT")
            if "completed_at" not in invocation_columns:
                self._conn.execute("ALTER TABLE model_invocations ADD COLUMN completed_at TEXT")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency "
                "ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            self._conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)
