from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.artifacts import ArtifactRecord
from app.db import Database, dumps_json, loads_json
from app.providers import ModelOutput
from app.schemas import (
    ExecutionStrategy,
    ModelReference,
    QueueItem,
    QueueResponse,
    TaskCreateRequest,
    TaskStateResponse,
    TaskStatus,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_task(self, request: TaskCreateRequest) -> TaskStateResponse:
        task_id = f"task_{uuid4().hex}"
        now = _utc_now_iso()
        row = self.db.query_one("SELECT COALESCE(MAX(queue_position), 0) AS pos FROM tasks")
        queue_position = int(row["pos"]) + 1 if row else 1
        request_json = request.model_dump(mode="json")
        progress = {
            "phase": TaskStatus.queued.value,
            "invocations_completed": 0,
            "invocations_total": 1
            if request.execution.strategy == ExecutionStrategy.single
            else request.execution.max_proposers,
        }

        self.db.execute(
            """
            INSERT INTO tasks (
                id, request_id, request_json, status, priority, queue_position,
                progress_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                request.request_id,
                dumps_json(request_json),
                TaskStatus.queued.value,
                request.priority,
                queue_position,
                dumps_json(progress),
                now,
                now,
            ),
        )
        self.add_event(task_id, "task.created", {"status": TaskStatus.queued.value})
        return self.get_task(task_id)

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

    def get_next_queued_task_id(self) -> str | None:
        row = self.db.query_one(
            """
            SELECT id FROM tasks
            WHERE status = 'queued'
            ORDER BY queue_position ASC, priority ASC, created_at ASC
            LIMIT 1
            """
        )
        return str(row["id"]) if row else None

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
        self.db.execute(
            """
            INSERT INTO model_invocations (
                id, task_id, run_id, role, provider, deployment, model,
                output_json, tokens_input, tokens_output, cost_usd, latency_ms,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invocation_id,
                task_id,
                run_id,
                role,
                model.provider,
                model.deployment,
                model.model,
                dumps_json({"content": output.content}),
                output.tokens_input,
                output.tokens_output,
                output.cost_usd,
                output.latency_ms,
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
