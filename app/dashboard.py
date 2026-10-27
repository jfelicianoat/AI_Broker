from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import BrokerConfig, served_remotely
from app.db import Database, loads_json
from app.repository import TaskRepository, _parse_dt
from app.schemas import (
    REQUEST_CARRYING_KINDS,
    DashboardEventItem,
    DashboardHistoryItem,
    DashboardHistoryOptions,
    DashboardHistoryPage,
    DashboardInvocationItem,
    DashboardSummaryResponse,
    DashboardTaskDetail,
    DashboardTaskItem,
    DashboardTaskPage,
    ExecutionPreset,
    ExecutionStrategy,
    LaneOccupancy,
    ModelReference,
    TaskKind,
    TaskStatus,
    UsageResponse,
)

# Trabajo en vuelo en CUALQUIER carril, conversiones incluidas: es lo que el
# panel debe llamar "activo" si quiere describir la carga real de la máquina.
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
    "converting",
)


def lane_capacities(config: BrokerConfig) -> dict[str, int]:
    """Cuánto trabajo simultáneo admite cada carril.

    Inferencia: el invariante de un único workflow. Ingesta: su propio tope,
    configurable, que es lo que impide que subir cinco PDFs asfixie la máquina.
    Embeddings: su concurrencia, y solo si el carril está activo; apagado, los
    embeddings cuentan en el carril de inferencia, que es donde corren."""
    capacities = {
        TaskKind.inference.value: config.processing.max_active_workflows,
        TaskKind.ingestion.value: config.ingestion.max_concurrent,
    }
    if config.processing.embedding_lane.enabled:
        capacities[TaskKind.embedding.value] = config.processing.embedding_lane.max_parallel
    if config.processing.cloud_lane.enabled:
        capacities[TaskKind.cloud.value] = config.processing.cloud_lane.max_parallel
    return capacities


# Orden de las columnas en el panel: las tres colas de modelos primero, y las
# conversiones al final porque no son trabajo de un LLM.
LANE_ORDER = (TaskKind.inference, TaskKind.cloud, TaskKind.embedding, TaskKind.ingestion)


def _lane_task_label(kind: TaskKind, row: Any) -> str:
    """Qué es la tarea, en una línea, según su carril."""
    request = loads_json(row["request_json"], {}) or {}
    if kind == TaskKind.ingestion:
        return str(request.get("filename") or "Conversión de fichero")
    if kind == TaskKind.embedding:
        total = int(row["texts"] or 1)
        return f"{total} texto{'s' if total != 1 else ''}"
    metadata = (request.get("content") or {}).get("metadata") or {}
    purpose = metadata.get("purpose") or metadata.get("origin")
    execution = request.get("execution") or {}
    strategy = execution.get("strategy") or "single"
    return f"{strategy} · {purpose}" if purpose else str(strategy)


class DashboardQueryRepository:
    """Read-only projections for the dashboard and operational API."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def lane_occupancy(self, capacities: dict[str, int]) -> list[LaneOccupancy]:
        """Cuánto trabajo hay en vuelo y en espera en cada carril.

        Es la respuesta a «cuál es la carga real de la máquina»: hasta que las
        conversiones fueron tareas, la mitad de esa carga no se podía contar."""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = self.db.query_all(
            f"""
            SELECT kind,
                   SUM(CASE WHEN status IN ({placeholders}) THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued
            FROM tasks
            GROUP BY kind
            """,
            ACTIVE_STATUSES,
        )
        counts = {str(row["kind"]): row for row in rows}
        lanes: list[LaneOccupancy] = []
        for kind in [item for item in LANE_ORDER if item.value in capacities]:
            row = counts.get(kind.value)
            lanes.append(
                LaneOccupancy(
                    kind=kind,
                    active=int((row["active"] if row else 0) or 0),
                    queued=int((row["queued"] if row else 0) or 0),
                    capacity=int(capacities.get(kind.value, 1)),
                )
            )
        return lanes

    def summary(
        self, *, window_hours: int, lane_capacities: dict[str, int] | None = None,
    ) -> DashboardSummaryResponse:
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
            lanes=self.lane_occupancy(lane_capacities or {}),
        )

    def list_tasks(
        self,
        *,
        page: int,
        page_size: int,
        status: TaskStatus | tuple[TaskStatus, ...] | None,
        origin: str | None,
        kind: TaskKind | None = None,
    ) -> DashboardTaskPage:
        where: list[str] = []
        params: list[Any] = []
        if kind is not None:
            where.append("t.kind = ?")
            params.append(kind.value)
        if status is not None:
            # Acepta varios estados porque "en cola" ya no es uno solo: una
            # tarea esperando memoria sigue pendiente y debe verse en la cola.
            wanted = (status,) if isinstance(status, TaskStatus) else tuple(status)
            where.append(f"t.status IN ({','.join('?' for _ in wanted)})")
            params.extend(item.value for item in wanted)
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
              CASE WHEN t.status IN ('queued','waiting_for_memory','waiting_for_dependencies') THEN 0
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
        artifacts = self.db.query_all(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at ASC, id ASC",
            (task_id,),
        )
        return DashboardTaskDetail(
            artifacts=[TaskRepository._row_to_artifact(task_id, item) for item in artifacts],
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
                    error_code=_column(item, "error_code"),
                    execution_fingerprint_hash=_column(item, "fingerprint_hash"),
                    excluded_from_model_learning=bool(_column(item, "excluded_from_learning") or 0),
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

    def embedding_lane_view(self, config: BrokerConfig, *, window_hours: int = 1) -> dict[str, Any]:
        """Lo que está pasando en el carril de embeddings, para su propio panel.

        Existe porque un indexado masivo son cientos de tareas que se pierden
        en la cola general, y la que está corriendo no sale en «Tarea activa»,
        que es la del workflow de inferencia. Aquí se ve qué se vectoriza
        ahora, qué viene detrás —en el orden real de reclamo—, cuántos textos
        faltan y cómo van los modelos de embeddings."""
        lane = config.processing.embedding_lane
        active_marks = ",".join("?" for _ in ACTIVE_STATUSES)
        pending = ("queued", "waiting_for_memory", "waiting_for_dependencies")
        pending_marks = ",".join("?" for _ in pending)
        texts = "COALESCE(json_array_length(request_json, '$.content.inputs'), 1)"
        running_rows = self.db.query_all(
            f"""
            SELECT id, priority, status, progress_json, updated_at, {texts} AS texts,
                   json_extract(request_json, '$.content.metadata.origin') AS origin
            FROM tasks WHERE kind = 'embedding' AND status IN ({active_marks})
            ORDER BY updated_at ASC
            """,
            ACTIVE_STATUSES,
        )
        now = datetime.now(timezone.utc)
        running = []
        for row in running_rows:
            progress = loads_json(row["progress_json"], {}) or {}
            models = progress.get("active_invocations") or []
            model = models[0] if models and isinstance(models[0], dict) else {}
            running.append({
                "task_id": row["id"],
                "priority": row["priority"],
                "status": TaskStatus(row["status"]),
                "texts": int(row["texts"] or 1),
                "model": model.get("model"),
                "provider": model.get("provider"),
                "seconds": max(0.0, (now - _parse_dt(row["updated_at"])).total_seconds()),
                "origin": row["origin"],
            })
        totals = self.db.query_one(
            f"""
            SELECT COUNT(*) AS tasks, COALESCE(SUM({texts}), 0) AS texts,
                   SUM(CASE WHEN status = 'waiting_for_memory' THEN 1 ELSE 0 END) AS waiting_memory
            FROM tasks WHERE kind = 'embedding' AND status IN ({pending_marks})
            """,
            pending,
        )
        by_priority = self.db.query_all(
            f"""
            SELECT priority, COUNT(*) AS tasks, COALESCE(SUM({texts}), 0) AS texts
            FROM tasks WHERE kind = 'embedding' AND status IN ({pending_marks})
            GROUP BY priority ORDER BY priority ASC
            """,
            pending,
        )
        next_rows = self.db.query_all(
            f"""
            SELECT id, priority, status, {texts} AS texts, created_at
            FROM tasks WHERE kind = 'embedding' AND status IN ({pending_marks})
            ORDER BY queue_position ASC, priority ASC, created_at ASC LIMIT 5
            """,
            pending,
        )
        since = (now - timedelta(hours=window_hours)).isoformat()
        recent = self.db.query_one(
            f"""
            SELECT SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   COALESCE(SUM(CASE WHEN status = 'completed' THEN {texts} ELSE 0 END), 0) AS texts
            FROM tasks WHERE kind = 'embedding' AND updated_at >= ?
              AND status IN ('completed', 'failed', 'cancelled')
            """,
            (since,),
        )
        failures = self.db.query_all(
            """
            SELECT json_extract(error_json, '$.code') AS code, COUNT(*) AS total
            FROM tasks WHERE kind = 'embedding' AND status = 'failed' AND updated_at >= ?
            GROUP BY code ORDER BY total DESC LIMIT 3
            """,
            (since,),
        )
        # Mismo origen que el ranking del router (model_invocations con
        # task_type 'embedding'): lo que se ve aquí es lo que decide.
        models = self.db.query_all(
            """
            SELECT provider, deployment, model, COUNT(*) AS attempts,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS successes,
                   AVG(CASE WHEN status = 'completed' THEN latency_ms END) AS avg_latency_ms
            FROM model_invocations
            WHERE task_type = 'embedding' AND status IN ('completed', 'failed') AND created_at >= ?
            GROUP BY provider, deployment, model ORDER BY attempts DESC LIMIT 6
            """,
            ((now - timedelta(hours=24)).isoformat(),),
        )
        completed_texts = int((recent["texts"] if recent else 0) or 0)
        return {
            "enabled": lane.enabled,
            "capacity": lane.max_parallel,
            "batch_max": lane.batch_max,
            "running": running,
            "queued_tasks": int(totals["tasks"] or 0) if totals else 0,
            "queued_texts": int(totals["texts"] or 0) if totals else 0,
            "waiting_memory": int(totals["waiting_memory"] or 0) if totals else 0,
            "by_priority": [dict(row) for row in by_priority],
            "next": [
                {
                    "task_id": row["id"], "priority": row["priority"],
                    "status": TaskStatus(row["status"]), "texts": int(row["texts"] or 1),
                }
                for row in next_rows
            ],
            "window_hours": window_hours,
            "completed": int((recent["completed"] if recent else 0) or 0),
            "failed": int((recent["failed"] if recent else 0) or 0),
            "completed_texts": completed_texts,
            "texts_per_minute": completed_texts / (window_hours * 60),
            "failures": [dict(row) for row in failures],
            "models": [
                {
                    **dict(row),
                    # Tasa observada, sin el suavizado que aplica el router:
                    # en pantalla se lee mejor «9 de 10» que un 0,83 corregido.
                    "success_rate": int(row["successes"] or 0) / max(1, int(row["attempts"])),
                }
                for row in models
            ],
        }

    def lanes_view(self, config: BrokerConfig, *, window_hours: int = 1, next_limit: int = 5) -> list[dict[str, Any]]:
        """Las colas una al lado de otra: qué corre y qué viene en cada carril.

        Con el carril cloud la cola dejó de ser una: una tarea cloud puede ir
        por delante de una local llegada antes, y la tabla única de la cola no
        lo explicaba. Cada columna enseña su propio orden real de reclamo.
        """
        capacities = lane_capacities(config)
        active_marks = ",".join("?" for _ in ACTIVE_STATUSES)
        pending = ("queued", "waiting_for_memory", "waiting_for_dependencies")
        pending_marks = ",".join("?" for _ in pending)
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=window_hours)).isoformat()
        texts = "COALESCE(json_array_length(request_json, '$.content.inputs'), 1)"
        lanes = []
        for kind in LANE_ORDER:
            if kind.value not in capacities:
                continue
            running_rows = self.db.query_all(
                f"""
                SELECT id, priority, status, progress_json, request_json, updated_at, {texts} AS texts
                FROM tasks WHERE kind = ? AND status IN ({active_marks})
                ORDER BY updated_at ASC
                """,
                (kind.value, *ACTIVE_STATUSES),
            )
            next_rows = self.db.query_all(
                f"""
                SELECT id, priority, status, request_json, progress_json, {texts} AS texts
                FROM tasks WHERE kind = ? AND status IN ({pending_marks})
                ORDER BY queue_position ASC, priority ASC, created_at ASC LIMIT ?
                """,
                (kind.value, *pending, next_limit),
            )
            totals = self.db.query_one(
                f"""
                SELECT COUNT(*) AS pending,
                       SUM(CASE WHEN status <> 'queued' THEN 1 ELSE 0 END) AS waiting
                FROM tasks WHERE kind = ? AND status IN ({pending_marks})
                """,
                (kind.value, *pending),
            )
            recent = self.db.query_one(
                """
                SELECT SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM tasks WHERE kind = ? AND updated_at >= ? AND status IN ('completed', 'failed')
                """,
                (kind.value, since),
            )
            running = []
            for row in running_rows:
                progress = loads_json(row["progress_json"], {}) or {}
                models = progress.get("active_invocations") or []
                model = models[0] if models and isinstance(models[0], dict) else {}
                running.append({
                    "task_id": row["id"],
                    "status": TaskStatus(row["status"]),
                    "priority": row["priority"],
                    "what": _lane_task_label(kind, row),
                    "model": model.get("model"),
                    "provider": model.get("provider"),
                    # Una tarea de la cola local puede estar llamando a un
                    # remoto (un proponente del consenso, por ejemplo): ocupa
                    # el turno, pero la GPU está libre. Se marca para que no
                    # parezca un cloud colado en la cola equivocada.
                    "remote": bool(model) and served_remotely(
                        config, model.get("provider"), model.get("deployment"),
                    ),
                    "seconds": max(0.0, (now - _parse_dt(row["updated_at"])).total_seconds()),
                })
            lanes.append({
                "kind": kind,
                "capacity": int(capacities[kind.value]),
                "running": running,
                "next": [
                    {
                        "task_id": row["id"],
                        "status": TaskStatus(row["status"]),
                        "priority": row["priority"],
                        "what": _lane_task_label(kind, row),
                        "demoted": bool((loads_json(row["progress_json"], {}) or {}).get("demoted_from_cloud")),
                    }
                    for row in next_rows
                ],
                "pending": int((totals["pending"] if totals else 0) or 0),
                "waiting": int((totals["waiting"] if totals else 0) or 0),
                "completed": int((recent["completed"] if recent else 0) or 0),
                "failed": int((recent["failed"] if recent else 0) or 0),
                "window_hours": window_hours,
            })
        demoted = self.db.query_one(
            "SELECT COUNT(*) AS total FROM events WHERE event_type = 'task.lane_demoted' AND created_at >= ?",
            (since,),
        )
        for lane in lanes:
            if lane["kind"] == TaskKind.cloud:
                # Lo que el carril cloud no pudo atender y pasó a la GPU: sin
                # esta cifra, un proveedor caído solo se nota en que la cola
                # local crece.
                lane["demoted"] = int((demoted["total"] if demoted else 0) or 0)
        return lanes

    def active_task_detail(self) -> DashboardTaskDetail | None:
        """El workflow de inferencia en curso, que es único por invariante.

        Filtra por carril a propósito: el panel "Tarea activa" habla de la
        inferencia, y una conversión no tiene ni prompt ni estrategia que
        enseñar ahí. La carga de las conversiones se ve en la ocupación de
        carriles (`lane_occupancy`)."""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.db.query_one(
            f"""
            SELECT id FROM tasks
            WHERE kind = 'inference' AND status IN ({placeholders})
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

    def history_options(self) -> DashboardHistoryOptions:
        """Proveedores, modelos y orígenes presentes en el histórico."""
        def column(sql: str) -> list[str]:
            return [str(row[0]) for row in self.db.query_all(sql) if row[0]]

        return DashboardHistoryOptions(
            providers=column(
                "SELECT DISTINCT provider FROM model_invocations ORDER BY provider"
            ),
            models=column("SELECT DISTINCT model FROM model_invocations ORDER BY model"),
            # Solo las tiradas recientes: son cientos con el tiempo y el
            # desplegable dejaría de servir para elegir.
            groups=column(
                "SELECT task_group FROM tasks WHERE task_group IS NOT NULL "
                "GROUP BY task_group ORDER BY MAX(updated_at) DESC LIMIT 40"
            ),
            origins=column(
                "SELECT DISTINCT json_extract(request_json, '$.content.metadata.origin') "
                "FROM tasks ORDER BY 1"
            ),
        )

    def list_history(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: TaskStatus | None = None,
        provider: str | None = None,
        model: str | None = None,
        origin: str | None = None,
        kind: TaskKind | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        search: str | None = None,
        excluded: bool | None = None,
        group: str | None = None,
    ) -> DashboardHistoryPage:
        """Tareas terminadas, filtrables y paginadas.

        Existe porque el panel de Tareas solo enseña las últimas y, en una
        tirada de cientos, lo anterior desaparece de la vista aunque siga en la
        base de datos.

        **Filtrar por modelo o proveedor mira las invocaciones, no el resultado.**
        Es la diferencia entre encontrar las tareas de un modelo y encontrar
        solo aquellas en las que además firmó la respuesta: en un mixture
        intervienen varios y solo uno queda en `model_used`, y una tarea que
        falló no llegó a tener resultado pero sí invocó a alguien. Buscar «qué
        pasó con este modelo» tiene que devolver las dos cosas.
        """
        where = ["t.status IN ('completed', 'failed', 'cancelled')"]
        params: list[Any] = []
        if status is not None:
            where.append("t.status = ?")
            params.append(status.value)
        if kind is not None:
            where.append("t.kind = ?")
            params.append(kind.value)
        if origin is not None:
            where.append("json_extract(t.request_json, '$.content.metadata.origin') = ?")
            params.append(origin)
        if since is not None:
            where.append("t.updated_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            where.append("t.updated_at <= ?")
            params.append(until.isoformat())
        if excluded is not None:
            where.append(
                "COALESCE(json_extract(t.request_json, '$.exclude_from_model_learning'), 0) = ?"
            )
            params.append(1 if excluded else 0)
        if group is not None:
            # La tirada entera de una tanda de evaluación: es la unidad en la
            # que se lanza el trabajo, así que también debe serlo al revisarlo.
            where.append("t.task_group = ?")
            params.append(group)
        if search:
            # Sobre identificadores, que es lo que alguien tiene a mano al
            # llegar aquí desde un log o desde su propia aplicación.
            where.append("(t.id LIKE ? OR COALESCE(t.request_id, '') LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        for column, value in (("provider", provider), ("model", model)):
            if value is not None:
                where.append(
                    "EXISTS (SELECT 1 FROM model_invocations f "
                    f"WHERE f.task_id = t.id AND LOWER(f.{column}) = LOWER(?))"
                )
                params.append(value)
        where_sql = " AND ".join(where)

        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS total FROM tasks t WHERE {where_sql}", params
        )
        total = int(total_row["total"] if total_row else 0)
        # La página se recorta ANTES de unir con las invocaciones. Uniendo
        # primero, SQLite agrupa el histórico entero para quedarse con veinte
        # filas: son 400 ms contra 40 con los datos de esta máquina, y crece con
        # el histórico. Aquí el join solo ve las tareas de la página.
        rows = self.db.query_all(
            f"""
            SELECT t.*,
                   COUNT(mi.id) AS invocation_count,
                   COALESCE(SUM(mi.tokens_input), 0) AS invocation_tokens_input,
                   COALESCE(SUM(mi.tokens_output), 0) AS invocation_tokens_output,
                   COALESCE(SUM(mi.cost_usd), 0) AS invocation_cost_usd,
                   GROUP_CONCAT(
                       DISTINCT mi.provider || '/' || mi.deployment || '/' || mi.model
                   ) AS invocation_models
            FROM (
                SELECT * FROM tasks t
                WHERE {where_sql}
                ORDER BY t.updated_at DESC
                LIMIT ? OFFSET ?
            ) t
            LEFT JOIN model_invocations mi ON mi.task_id = t.id
            GROUP BY t.id
            ORDER BY t.updated_at DESC
            """,
            [*params, page_size, (page - 1) * page_size],
        )
        return DashboardHistoryPage(
            items=[self._history_item(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=math.ceil(total / page_size) if total else 0,
        )

    @staticmethod
    def _history_item(row: Any) -> DashboardHistoryItem:
        kind = TaskKind(row["kind"] if "kind" in row.keys() else TaskKind.inference.value)
        payload = loads_json(row["request_json"], {})
        result = loads_json(row["result_json"], None) or {}
        error = loads_json(row["error_json"], None) or {}
        request = payload if kind in REQUEST_CARRYING_KINDS else {}
        metadata = (request.get("content") or {}).get("metadata") or {}
        execution = request.get("execution") or {}
        created_at = _parse_dt(row["created_at"])
        updated_at = _parse_dt(row["updated_at"])
        return DashboardHistoryItem(
            task_id=row["id"],
            kind=kind,
            label=payload.get("filename") if kind == TaskKind.ingestion else None,
            request_id=row["request_id"],
            status=TaskStatus(row["status"]),
            origin=metadata.get("origin") if isinstance(metadata.get("origin"), str) else None,
            execution_strategy=(
                ExecutionStrategy(execution.get("strategy", "single"))
                if kind in REQUEST_CARRYING_KINDS
                else None
            ),
            execution_preset=(
                ExecutionPreset(execution.get("preset", "fast"))
                if kind in REQUEST_CARRYING_KINDS
                else None
            ),
            models=_parse_model_list(row["invocation_models"]),
            fallback_used=(
                result.get("fallback_used") if isinstance(result.get("fallback_used"), bool) else None
            ),
            error_code=str(error.get("code")) if error.get("code") else None,
            invocations=int(row["invocation_count"] or 0),
            tokens_input=int(row["invocation_tokens_input"] or 0),
            tokens_output=int(row["invocation_tokens_output"] or 0),
            cost_actual_usd=float(row["invocation_cost_usd"] or 0),
            duration_seconds=max(0.0, (updated_at - created_at).total_seconds()),
            excluded_from_model_learning=bool(request.get("exclude_from_model_learning") or False),
            created_at=created_at,
            updated_at=updated_at,
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
        kind = TaskKind(row["kind"] if "kind" in row.keys() else TaskKind.inference.value)
        payload = loads_json(row["request_json"], {})
        result = loads_json(row["result_json"], None) or {}
        # En el carril de ingesta el payload es un descriptor de conversión, no
        # una petición: leerle "execution" devolvería el default single/fast y
        # el panel enseñaría una estrategia que esa tarea nunca tuvo.
        request = payload if kind in REQUEST_CARRYING_KINDS else {}
        requirements = request.get("model_requirements") or {}
        requested_model = _model_reference(requirements.get("target_model"))
        effective_model = _model_reference(result.get("model_used"))
        metadata = (request.get("content") or {}).get("metadata") or {}
        execution = request.get("execution") or {}
        return DashboardTaskItem(
            task_id=row["id"],
            kind=kind,
            label=payload.get("filename") if kind == TaskKind.ingestion else None,
            request_id=row["request_id"],
            status=TaskStatus(row["status"]),
            priority=int(row["priority"]),
            queue_position=row["queue_position"],
            origin=metadata.get("origin") if isinstance(metadata.get("origin"), str) else None,
            execution_strategy=(
                ExecutionStrategy(execution.get("strategy", "single"))
                if kind in REQUEST_CARRYING_KINDS
                else None
            ),
            execution_preset=(
                ExecutionPreset(execution.get("preset", "fast"))
                if kind in REQUEST_CARRYING_KINDS
                else None
            ),
            requested_model=requested_model,
            effective_model=effective_model,
            fallback_used=result.get("fallback_used") if isinstance(result.get("fallback_used"), bool) else None,
            memory_block=(
                loads_json(row["memory_block_json"], None)
                if "memory_block_json" in row.keys()
                else None
            ),
            dependency_wait=(
                loads_json(row["dependency_wait_json"], None)
                if "dependency_wait_json" in row.keys()
                else None
            ),
            invocations=int(row["invocation_count"] or 0),
            tokens_input=int(row["invocation_tokens_input"] or 0),
            tokens_output=int(row["invocation_tokens_output"] or 0),
            cost_actual_usd=float(row["invocation_cost_usd"] or 0),
            # Se lee de la petición y no de las invocaciones porque la tarea
            # puede no haber invocado nada todavía y la marca ya es cierta.
            excluded_from_model_learning=bool(request.get("exclude_from_model_learning") or False),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


def _column(row: Any, name: str) -> Any:
    """Valor de una columna que puede no existir todavía en esta base.

    Las columnas nuevas llegan por migración; leerlas a pelo reventaría contra
    una fila leída antes de que la migración corriera."""
    return row[name] if name in row.keys() else None


def _parse_model_list(value: Any) -> list[ModelReference]:
    """Los modelos que intervinieron, desde el GROUP_CONCAT de la consulta.

    Cada entrada llega como `proveedor/deployment/modelo`. El nombre del modelo
    puede llevar barras (`qwen/qwen3-coder`), así que se parte solo por las dos
    primeras: partir por todas trocearía el nombre."""
    if not isinstance(value, str) or not value:
        return []
    references: list[ModelReference] = []
    for item in value.split(","):
        parts = item.split("/", 2)
        if len(parts) != 3 or not all(parts):
            continue
        try:
            references.append(
                ModelReference(provider=parts[0], deployment=parts[1], model=parts[2])
            )
        except Exception:
            continue
    return references


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
