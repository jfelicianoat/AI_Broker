from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import Database, loads_json
from app.repository import _parse_dt
from app.schemas import (
    DashboardEventItem,
    DashboardInvocationItem,
    DashboardSummaryResponse,
    DashboardTaskDetail,
    DashboardTaskItem,
    DashboardTaskPage,
    ExecutionPreset,
    ExecutionStrategy,
    ModelReference,
    TaskStatus,
    UsageResponse,
)

ACTIVE_STATUSES = (
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


class DashboardQueryRepository:
    """Read-only projections for the dashboard and operational API."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def summary(self, *, window_hours: int) -> DashboardSummaryResponse:
        checked_at = datetime.now(timezone.utc)
        window_start = (checked_at - timedelta(hours=window_hours)).isoformat()
        active_placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        current = self.db.query_one(
            f"""
            SELECT
                SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN status IN ({active_placeholders}) THEN 1 ELSE 0 END) AS active,
                MIN(CASE WHEN status = 'queued' THEN created_at END) AS oldest_queued_at
            FROM tasks
            """,
            ACTIVE_STATUSES,
        )
        terminal = self.db.query_one(
            """
            SELECT
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
            FROM tasks
            WHERE updated_at >= ?
            """,
            (window_start,),
        )
        invocation_rows = self.db.query_all(
            """
            SELECT tokens_input, tokens_output, cost_usd, latency_ms
            FROM model_invocations
            WHERE status = 'completed' AND updated_at >= ?
            """,
            (window_start,),
        )
        latencies = sorted(
            float(row["latency_ms"])
            for row in invocation_rows
            if row["latency_ms"] is not None
        )
        completed = int((terminal["completed"] if terminal else 0) or 0)
        failed = int((terminal["failed"] if terminal else 0) or 0)
        success_denominator = completed + failed
        oldest = current["oldest_queued_at"] if current else None
        oldest_seconds = None
        if oldest:
            oldest_seconds = max(0.0, (checked_at - _parse_dt(oldest)).total_seconds())
        return DashboardSummaryResponse(
            checked_at=checked_at,
            window_hours=window_hours,
            queued=int((current["queued"] if current else 0) or 0),
            active=int((current["active"] if current else 0) or 0),
            completed=completed,
            failed=failed,
            cancelled=int((terminal["cancelled"] if terminal else 0) or 0),
            success_rate=(completed / success_denominator) if success_denominator else None,
            invocations=len(invocation_rows),
            latency_p50_ms=_percentile(latencies, 50),
            latency_p95_ms=_percentile(latencies, 95),
            tokens_input=sum(int(row["tokens_input"] or 0) for row in invocation_rows),
            tokens_output=sum(int(row["tokens_output"] or 0) for row in invocation_rows),
            cost_actual_usd=round(sum(float(row["cost_usd"] or 0) for row in invocation_rows), 8),
            oldest_queued_seconds=oldest_seconds,
        )

    def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        status: TaskStatus | None,
        origin: str | None,
    ) -> DashboardTaskPage:
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("t.status = ?")
            params.append(status.value)
        if origin is not None:
            where.append("json_extract(t.request_json, '$.content.metadata.origin') = ?")
            params.append(origin)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS total FROM tasks t {where_sql}",
            params,
        )
        total = int(total_row["total"] if total_row else 0)
        offset = (page - 1) * page_size
        rows = self.db.query_all(
            f"""
            SELECT t.*,
                   COUNT(mi.id) AS invocation_count,
                   COALESCE(SUM(mi.tokens_input), 0) AS invocation_tokens_input,
                   COALESCE(SUM(mi.tokens_output), 0) AS invocation_tokens_output,
                   COALESCE(SUM(mi.cost_usd), 0) AS invocation_cost_usd
            FROM tasks t
            LEFT JOIN model_invocations mi ON mi.task_id = t.id
            {where_sql}
            GROUP BY t.id
            ORDER BY
              CASE WHEN t.status = 'queued' THEN 0
                   WHEN t.status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) THEN 1
                   ELSE 2 END,
              t.queue_position ASC,
              t.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, *ACTIVE_STATUSES, page_size, offset],
        )
        return DashboardTaskPage(
            items=[self._task_item(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=math.ceil(total / page_size) if total else 0,
        )

    def task_detail(self, task_id: str) -> DashboardTaskDetail:
        row = self.db.query_one(
            """
            SELECT t.*,
                   COUNT(mi.id) AS invocation_count,
                   COALESCE(SUM(mi.tokens_input), 0) AS invocation_tokens_input,
                   COALESCE(SUM(mi.tokens_output), 0) AS invocation_tokens_output,
                   COALESCE(SUM(mi.cost_usd), 0) AS invocation_cost_usd
            FROM tasks t
            LEFT JOIN model_invocations mi ON mi.task_id = t.id
            WHERE t.id = ?
            GROUP BY t.id
            """,
            (task_id,),
        )
        if row is None:
            raise KeyError(task_id)
        invocations = self.db.query_all(
            """
            SELECT * FROM model_invocations
            WHERE task_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (task_id,),
        )
        events = self.db.query_all(
            """
            SELECT * FROM events
            WHERE task_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 500
            """,
            (task_id,),
        )
        return DashboardTaskDetail(
            task=self._task_item(row),
            request=loads_json(row["request_json"], {}),
            progress=loads_json(row["progress_json"], {}),
            result=loads_json(row["result_json"], None),
            error=loads_json(row["error_json"], None),
            invocations=[
                DashboardInvocationItem(
                    invocation_id=item["id"],
                    role=item["role"],
                    provider=item["provider"],
                    deployment=item["deployment"],
                    model=item["model"],
                    status=item["status"],
                    tokens_input=int(item["tokens_input"] or 0),
                    tokens_output=int(item["tokens_output"] or 0),
                    cost_usd=float(item["cost_usd"] or 0),
                    latency_ms=float(item["latency_ms"]) if item["latency_ms"] is not None else None,
                    started_at=_parse_dt(item["started_at"]) if item["started_at"] else None,
                    completed_at=_parse_dt(item["completed_at"]) if item["completed_at"] else None,
                    created_at=_parse_dt(item["created_at"]),
                    updated_at=_parse_dt(item["updated_at"]),
                )
                for item in invocations
            ],
            events=[
                DashboardEventItem(
                    event_id=int(item["id"]),
                    event_type=item["event_type"],
                    payload=loads_json(item["payload_json"], {}),
                    created_at=_parse_dt(item["created_at"]),
                )
                for item in events
            ],
        )

    def active_task_detail(self) -> DashboardTaskDetail | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.db.query_one(
            f"""
            SELECT id FROM tasks
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            ACTIVE_STATUSES,
        )
        return self.task_detail(row["id"]) if row is not None else None

    def list_comparison_tasks(self, *, page_size: int = 25) -> DashboardTaskPage:
        rows = self.db.query_all(
            """
            SELECT t.*,
                   COUNT(mi.id) AS invocation_count,
                   COALESCE(SUM(mi.tokens_input), 0) AS invocation_tokens_input,
                   COALESCE(SUM(mi.tokens_output), 0) AS invocation_tokens_output,
                   COALESCE(SUM(mi.cost_usd), 0) AS invocation_cost_usd
            FROM tasks t
            LEFT JOIN model_invocations mi ON mi.task_id = t.id
            WHERE json_extract(t.request_json, '$.execution.strategy') = 'mixture_of_agents'
            GROUP BY t.id
            ORDER BY t.updated_at DESC
            LIMIT ?
            """,
            (page_size,),
        )
        return DashboardTaskPage(
            items=[self._task_item(row) for row in rows],
            page=1,
            page_size=page_size,
            total=len(rows),
            total_pages=1 if rows else 0,
        )

    def list_terminal_tasks(self, *, page_size: int = 25) -> DashboardTaskPage:
        rows = self.db.query_all(
            """
            SELECT t.*,
                   COUNT(mi.id) AS invocation_count,
                   COALESCE(SUM(mi.tokens_input), 0) AS invocation_tokens_input,
                   COALESCE(SUM(mi.tokens_output), 0) AS invocation_tokens_output,
                   COALESCE(SUM(mi.cost_usd), 0) AS invocation_cost_usd
            FROM tasks t
            LEFT JOIN model_invocations mi ON mi.task_id = t.id
            WHERE t.status IN ('completed', 'failed', 'cancelled')
            GROUP BY t.id
            ORDER BY t.updated_at DESC
            LIMIT ?
            """,
            (page_size,),
        )
        return DashboardTaskPage(
            items=[self._task_item(row) for row in rows],
            page=1,
            page_size=page_size,
            total=len(rows),
            total_pages=1 if rows else 0,
        )

    def usage(self, month: str) -> UsageResponse:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        rows = self.db.query_all(
            """
            SELECT provider,
                   COUNT(*) AS invocations,
                   COALESCE(SUM(tokens_input), 0) AS tokens_input,
                   COALESCE(SUM(tokens_output), 0) AS tokens_output,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(AVG(latency_ms), 0) AS latency_avg_ms
            FROM model_invocations
            WHERE status = 'completed' AND updated_at >= ? AND updated_at < ?
            GROUP BY provider
            ORDER BY provider
            """,
            (start.isoformat(), end.isoformat()),
        )
        return UsageResponse(
            month=month,
            providers={
                row["provider"]: {
                    "invocations": float(row["invocations"] or 0),
                    "tokens_input": float(row["tokens_input"] or 0),
                    "tokens_output": float(row["tokens_output"] or 0),
                    "cost_usd": float(row["cost_usd"] or 0),
                    "latency_avg_ms": float(row["latency_avg_ms"] or 0),
                }
                for row in rows
            },
        )

    @staticmethod
    def _task_item(row: Any) -> DashboardTaskItem:
        request = loads_json(row["request_json"], {})
        result = loads_json(row["result_json"], None) or {}
        requirements = request.get("model_requirements") or {}
        requested_model = _model_reference(requirements.get("target_model"))
        effective_model = _model_reference(result.get("model_used"))
        metadata = (request.get("content") or {}).get("metadata") or {}
        execution = request.get("execution") or {}
        return DashboardTaskItem(
            task_id=row["id"],
            request_id=row["request_id"],
            status=TaskStatus(row["status"]),
            priority=int(row["priority"]),
            queue_position=row["queue_position"],
            origin=metadata.get("origin") if isinstance(metadata.get("origin"), str) else None,
            execution_strategy=ExecutionStrategy(execution.get("strategy", "single")),
            execution_preset=ExecutionPreset(execution.get("preset", "fast")),
            requested_model=requested_model,
            effective_model=effective_model,
            fallback_used=result.get("fallback_used") if isinstance(result.get("fallback_used"), bool) else None,
            invocations=int(row["invocation_count"] or 0),
            tokens_input=int(row["invocation_tokens_input"] or 0),
            tokens_output=int(row["invocation_tokens_output"] or 0),
            cost_actual_usd=float(row["invocation_cost_usd"] or 0),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


def _model_reference(value: Any) -> ModelReference | None:
    if not isinstance(value, dict):
        return None
    try:
        return ModelReference.model_validate(value)
    except Exception:
        return None


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    rank = max(1, math.ceil((percentile / 100) * len(values)))
    return round(values[rank - 1], 3)
