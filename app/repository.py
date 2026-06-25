from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from app.artifacts import ArtifactRecord
from app.db import Database, dumps_json, loads_json
from app.providers import ModelOutput, ProviderError
from app.schemas import (
    ExecutionStrategy,
    ModelReference,
    QueueItem,
    QueueResponse,
    TaskCreateRequest,
    TaskStateResponse,
    TaskStatus,
)


class IdempotencyConflict(ValueError):
    pass


class QueueFull(ValueError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_task(self, request: TaskCreateRequest, *, queue_max_size: int) -> tuple[TaskStateResponse, bool]:
        task_id = f"task_{uuid4().hex}"
        now = _utc_now_iso()
        request_json = request.model_dump(mode="json")
        canonical = json.dumps(request_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        progress = {
            "phase": TaskStatus.queued.value,
            "invocations_completed": 0,
            "invocations_total": 1
            if request.execution.strategy == ExecutionStrategy.single
            else request.execution.max_proposers,
        }

        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id, request_hash FROM tasks WHERE idempotency_key = ?", (request.idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(request.idempotency_key)
                return self.get_task(existing["id"]), False
            queued = connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued'").fetchone()[0]
            if int(queued) >= queue_max_size:
                raise QueueFull("QUEUE_FULL")
            row = connection.execute("SELECT COALESCE(MAX(queue_position), 0) AS pos FROM tasks").fetchone()
            queue_position = int(row["pos"]) + 1 if row else 1
            connection.execute(
                """
                INSERT INTO tasks (
                    id, request_id, idempotency_key, request_hash, request_json, status, priority, queue_position,
                    progress_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, request.request_id, request.idempotency_key, request_hash, dumps_json(request_json),
                 TaskStatus.queued.value, request.priority, queue_position, dumps_json(progress), now, now),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.created", dumps_json({"status": TaskStatus.queued.value}), now),
            )
        return self.get_task(task_id), True

    def get_task(self, task_id: str) -> TaskStateResponse:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return self._row_to_task_state(row)

    def get_task_request(self, task_id: str) -> TaskCreateRequest:
        row = self.db.query_one("SELECT request_json FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return TaskCreateRequest.model_validate(loads_json(row["request_json"]))

    def claim_next_queued_task_id(self) -> str | None:
        """Reclama como máximo un workflow dentro de una transacción inmediata."""
        active = (
            "routing", "planning", "resource_planning", "chunking", "generating",
            "proposing", "evaluating", "debating", "synthesizing", "verifying",
        )
        marks = ",".join("?" for _ in active)
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            if connection.execute(
                f"SELECT 1 FROM tasks WHERE status IN ({marks}) LIMIT 1", active
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT id, progress_json FROM tasks WHERE status = 'queued' "
                "ORDER BY queue_position ASC, priority ASC, created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            progress = loads_json(row["progress_json"], {})
            progress["phase"] = TaskStatus.routing.value
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, queue_position = NULL, progress_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (TaskStatus.routing.value, dumps_json(progress), now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "task.claimed", dumps_json({"status": "routing"}), now),
            )
            return str(row["id"])

    def update_task(
        self,
        task_id: str,
        status: TaskStatus,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        clear_queue_position: bool = False,
    ) -> TaskStateResponse:
        now = _utc_now_iso()
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, now]
        if progress is not None:
            assignments.append("progress_json = ?")
            params.append(dumps_json(progress))
        if result is not None:
            assignments.append("result_json = ?")
            params.append(dumps_json(result))
        if error is not None:
            assignments.append("error_json = ?")
            params.append(dumps_json(error))
        if clear_queue_position:
            assignments.append("queue_position = NULL")
        params.append(task_id)
        self.db.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", params)
        self.add_event(task_id, "task.status_changed", {"status": status.value})
        return self.get_task(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        row = self.db.query_one("SELECT cancel_requested, status FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return bool(row["cancel_requested"]) or row["status"] == TaskStatus.cancelled.value

    def get_consensus_run_id(self, task_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT id FROM consensus_runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        return str(row["id"]) if row else None

    def record_invocation(
        self,
        task_id: str,
        run_id: str | None,
        role: str,
        model: ModelReference,
        output: ModelOutput,
        status: str = "completed",
    ) -> str:
        invocation_id = f"inv_{uuid4().hex}"
        now = _utc_now_iso()
        started_at, completed_at = _invocation_window(now, output.latency_ms)
        self.db.execute(
            """
            INSERT INTO model_invocations (
                id, task_id, run_id, role, provider, deployment, model,
                output_json, tokens_input, tokens_output, cost_usd, latency_ms,
                started_at, completed_at, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invocation_id,
                task_id,
                run_id,
                role,
                model.provider,
                model.deployment,
                model.model,
                dumps_json(output.technical_output()),
                output.tokens_input,
                output.tokens_output,
                output.cost_usd,
                output.latency_ms,
                started_at,
                completed_at,
                status,
                now,
                now,
            ),
        )
        self.add_event(
            task_id,
            "model_invocation.completed",
            {"invocation_id": invocation_id, "role": role, "model": model.model},
        )
        return invocation_id

    def complete_single_task(
        self,
        task_id: str,
        model: ModelReference,
        output: ModelOutput,
        *,
        progress: dict[str, Any],
        result: dict[str, Any],
    ) -> str:
        """Persiste invocación y resultado terminal en la misma transacción."""
        invocation_id = f"inv_{uuid4().hex}"
        now = _utc_now_iso()
        started_at, completed_at = _invocation_window(now, output.latency_ms)
        with self.db.transaction() as connection:
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or row["status"] == TaskStatus.cancelled.value:
                raise ProviderError("TASK_CANCELLED", "La tarea fue cancelada antes de persistir el resultado")
            connection.execute(
                "INSERT INTO model_invocations (id, task_id, run_id, role, provider, deployment, model, "
                "output_json, tokens_input, tokens_output, cost_usd, latency_ms, started_at, completed_at, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, NULL, 'single', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)",
                (
                    invocation_id, task_id, model.provider, model.deployment, model.model,
                    dumps_json(output.technical_output()), output.tokens_input, output.tokens_output,
                    output.cost_usd, output.latency_ms, started_at, completed_at, now, now,
                ),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, progress_json = ?, result_json = ?, queue_position = NULL, updated_at = ? "
                "WHERE id = ?",
                (TaskStatus.completed.value, dumps_json(progress), dumps_json(result), now, task_id),
            )
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "model_invocation.completed", dumps_json({
                    "invocation_id": invocation_id, "role": "single", "model": model.model,
                }), now),
            )
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.status_changed", dumps_json({"status": TaskStatus.completed.value}), now),
            )
        return invocation_id

    def record_artifact(
        self,
        task_id: str,
        run_id: str | None,
        invocation_id: str | None,
        artifact_type: str,
        artifact: ArtifactRecord,
    ) -> str:
        artifact_id = f"art_{uuid4().hex}"
        self.db.execute(
            """
            INSERT INTO artifacts (
                id, task_id, run_id, invocation_id, artifact_type,
                path, sha256, size_bytes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                task_id,
                run_id,
                invocation_id,
                artifact_type,
                artifact.path,
                artifact.sha256,
                artifact.size_bytes,
                _utc_now_iso(),
            ),
        )
        self.add_event(
            task_id,
            "artifact.created",
            {"artifact_id": artifact_id, "type": artifact_type, "path": artifact.path},
        )
        return artifact_id

    def list_queue(self) -> QueueResponse:
        rows = self.db.query_all(
            """
            SELECT * FROM tasks
            ORDER BY
              CASE
                WHEN status = 'queued' THEN 0
                WHEN status IN ('routing','planning','resource_planning','chunking','generating','proposing','evaluating','debating','synthesizing','verifying') THEN 1
                ELSE 2
              END,
              queue_position ASC,
              updated_at DESC
            """
        )
        pending: list[QueueItem] = []
        active: list[QueueItem] = []
        terminal: list[QueueItem] = []
        active_statuses = {
            TaskStatus.routing,
            TaskStatus.planning,
            TaskStatus.resource_planning,
            TaskStatus.chunking,
            TaskStatus.generating,
            TaskStatus.proposing,
            TaskStatus.evaluating,
            TaskStatus.debating,
            TaskStatus.synthesizing,
            TaskStatus.verifying,
        }
        terminal_statuses = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
        for row in rows:
            item = self._row_to_queue_item(row)
            if item.status == TaskStatus.queued:
                pending.append(item)
            elif item.status in active_statuses:
                active.append(item)
            elif item.status in terminal_statuses:
                terminal.append(item)
        return QueueResponse(pending=pending, active=active, terminal=terminal)

    def request_cancel(self, task_id: str) -> TaskStateResponse:
        task = self.get_task(task_id)
        now = _utc_now_iso()
        if task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}:
            return task
        self.db.execute(
            """
            UPDATE tasks
            SET cancel_requested = 1, status = ?, queue_position = NULL, updated_at = ?
            WHERE id = ?
            """,
            (TaskStatus.cancelled.value, now, task_id),
        )
        self.add_event(task_id, "task.cancelled", {"requested": True})
        return self.get_task(task_id)

    def reorder_queue(self, task_ids: list[str]) -> QueueResponse:
        current = self.list_queue().pending
        current_ids = [item.task_id for item in current]
        if set(task_ids) != set(current_ids):
            raise ValueError("task_ids must contain exactly all queued task ids")
        now = _utc_now_iso()
        for position, task_id in enumerate(task_ids, start=1):
            self.db.execute(
                "UPDATE tasks SET queue_position = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                (position, now, task_id),
            )
        self.add_event(None, "queue.reordered", {"task_ids": task_ids})
        return self.list_queue()

    def add_event(self, task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event_type, dumps_json(payload), _utc_now_iso()),
        )

    def recover_interrupted_tasks(self) -> int:
        active_statuses = (
            "routing",
            "planning",
            "resource_planning",
            "chunking",
            "generating",
            "proposing",
            "evaluating",
            "debating",
            "synthesizing",
            "verifying",
        )
        placeholders = ",".join("?" for _ in active_statuses)
        now = _utc_now_iso()
        rows = self.db.query_all(
            f"SELECT id FROM tasks WHERE status IN ({placeholders})",
            active_statuses,
        )
        for index, row in enumerate(rows, start=1):
            self.db.execute(
                """
                UPDATE tasks
                SET status = 'queued',
                    queue_position = COALESCE(queue_position, ?),
                    attempt = attempt + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (index, now, row["id"]),
            )
            self.add_event(row["id"], "task.recovered", {"status": "queued"})
        return len(rows)

    def _row_to_task_state(self, row: Any) -> TaskStateResponse:
        request = TaskCreateRequest.model_validate(loads_json(row["request_json"]))
        return TaskStateResponse(
            task_id=row["id"],
            status=TaskStatus(row["status"]),
            request_id=row["request_id"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            execution_strategy=request.execution.strategy,
            execution_preset=request.execution.preset,
            selection_mode=request.execution.selection.mode,
            progress=loads_json(row["progress_json"], {}),
            result=loads_json(row["result_json"], None),
            error=loads_json(row["error_json"], None),
        )

    def _row_to_queue_item(self, row: Any) -> QueueItem:
        return QueueItem(
            task_id=row["id"],
            status=TaskStatus(row["status"]),
            request_id=row["request_id"],
            priority=row["priority"],
            queue_position=row["queue_position"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


def _invocation_window(completed_iso: str, latency_ms: float | None) -> tuple[str | None, str]:
    completed = _parse_dt(completed_iso)
    if latency_ms is None:
        return None, completed_iso
    started = completed - timedelta(milliseconds=max(0.0, float(latency_ms)))
    return started.isoformat(), completed_iso
