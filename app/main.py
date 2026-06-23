from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import BrokerConfig, load_config
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.repository import TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import (
    HealthDependency,
    HealthResponse,
    QueueReorderRequest,
    QueueResponse,
    TaskAcceptedResponse,
    TaskCreateRequest,
    TaskStateResponse,
    TaskStatus,
    UsageResponse,
)


def create_app(config: BrokerConfig | None = None) -> FastAPI:
    broker_config = config or load_config()
    db = Database(Path(broker_config.persistence.database), broker_config.persistence.journal_mode)
    repository = TaskRepository(db)
    scheduler = ResourceScheduler(broker_config)
    coordinator = ConsensusCoordinator(db, scheduler)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_schema()
        repository.recover_interrupted_tasks()
        yield
        db.close()

    app = FastAPI(title="AI Broker", version="0.1.0", lifespan=lifespan)
    app.state.config = broker_config
    app.state.db = db
    app.state.repository = repository
    app.state.scheduler = scheduler
    app.state.coordinator = coordinator

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "CONTRACT_VALIDATION_FAILED",
                "message": "Request does not satisfy Broker contract v1",
                "fields": exc.errors(),
            },
        )

    @app.post("/api/v1/tasks", response_model=TaskAcceptedResponse, status_code=202)
    async def create_task(payload: TaskCreateRequest) -> TaskAcceptedResponse:
        queue = repository.list_queue()
        if len(queue.pending) >= broker_config.processing.queue_max_size:
            raise HTTPException(status_code=429, detail="QUEUE_FULL")

        task = repository.create_task(payload)
        coordinator.initialize_run(task.task_id, payload)
        return TaskAcceptedResponse(
            task_id=task.task_id,
            status=TaskStatus.queued,
            execution_strategy=payload.execution.strategy,
            execution_preset=payload.execution.preset,
            selection_mode=payload.execution.selection.mode,
            status_url=f"/api/v1/tasks/{task.task_id}",
            cancel_url=f"/api/v1/tasks/{task.task_id}",
        )

    @app.get("/api/v1/tasks/{task_id}", response_model=TaskStateResponse)
    async def get_task(task_id: str) -> TaskStateResponse:
        try:
            return repository.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from exc

    @app.delete("/api/v1/tasks/{task_id}", response_model=TaskStateResponse)
    async def cancel_task(task_id: str) -> TaskStateResponse:
        try:
            return repository.request_cancel(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from exc

    @app.get("/api/v1/queue", response_model=QueueResponse)
    async def get_queue() -> QueueResponse:
        return repository.list_queue()

    @app.patch("/api/v1/queue", response_model=QueueResponse)
    async def reorder_queue(payload: QueueReorderRequest) -> QueueResponse:
        try:
            return repository.reorder_queue(payload.task_ids)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/dispatcher/tick")
    async def dispatcher_tick() -> dict[str, str | None]:
        task_id = await coordinator.process_next(repository)
        return {"task_id": task_id}

    @app.get("/api/v1/models")
    async def list_models() -> dict[str, object]:
        return {
            "models": [],
            "note": "Ollama discovery will be added after the durable API and scheduler baseline.",
        }

    @app.get("/api/v1/usage", response_model=UsageResponse)
    async def get_usage() -> UsageResponse:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return UsageResponse(month=month, providers={})

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return _health_response(db)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready() -> HealthResponse:
        response = _health_response(db)
        if response.status == "unavailable":
            raise HTTPException(status_code=503, detail=response.model_dump(mode="json"))
        return response

    return app


def _health_response(db: Database) -> HealthResponse:
    checked_at = datetime.now(timezone.utc)
    try:
        start = datetime.now(timezone.utc)
        db.query_one("SELECT 1")
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        sqlite = HealthDependency(
            status="healthy",
            checked_at=checked_at,
            detail="SQLite reachable",
            latency_ms=latency_ms,
        )
        status = "healthy"
    except Exception as exc:  # pragma: no cover - defensive readiness path
        sqlite = HealthDependency(
            status="unavailable",
            checked_at=checked_at,
            detail=str(exc),
        )
        status = "unavailable"
    return HealthResponse(status=status, checked_at=checked_at, dependencies={"sqlite": sqlite})


app = create_app()
