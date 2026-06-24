from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import BrokerConfig, load_config
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.providers import build_provider
from app.repository import IdempotencyConflict, QueueFull, TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import (
    BrokerCapabilitiesResponse,
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
    provider = build_provider(broker_config)
    coordinator = ConsensusCoordinator(db, scheduler, provider=provider)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_schema()
        repository.recover_interrupted_tasks()
        stop_dispatcher = asyncio.Event()
        dispatcher_task = None
        if broker_config.processing.auto_dispatch:
            dispatcher_task = asyncio.create_task(
                _dispatcher_loop(
                    repository,
                    coordinator,
                    stop_dispatcher,
                    broker_config.processing.dispatcher_interval_seconds,
                )
            )
        try:
            yield
        finally:
            stop_dispatcher.set()
            if dispatcher_task is not None:
                await dispatcher_task
            await provider.close()
            db.close()

    app = FastAPI(title="AI Broker", version="0.1.0", lifespan=lifespan)
    app.state.config = broker_config
    app.state.db = db
    app.state.repository = repository
    app.state.scheduler = scheduler
    app.state.coordinator = coordinator
    app.state.provider = provider

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "CONTRACT_VALIDATION_FAILED",
                "message": "Request does not satisfy Broker contract v1",
                "fields": jsonable_encoder(exc.errors(), custom_encoder={ValueError: str}),
            },
        )

    @app.post("/api/v1/tasks", response_model=TaskAcceptedResponse, status_code=202)
    async def create_task(payload: TaskCreateRequest, response: Response) -> TaskAcceptedResponse:
        try:
            task, created = repository.create_task(
                payload, queue_max_size=broker_config.processing.queue_max_size
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT") from exc
        except QueueFull as exc:
            raise HTTPException(status_code=429, detail="QUEUE_FULL") from exc
        if created:
            coordinator.initialize_run(task.task_id, payload)
        else:
            response.status_code = 200
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
        return {"models": await provider.models()}

    @app.get("/api/v1/capabilities", response_model=BrokerCapabilitiesResponse)
    async def capabilities() -> BrokerCapabilitiesResponse:
        return BrokerCapabilitiesResponse(
            contract_version="2.1",
            strategies=["single", "mixture_of_agents"],
            presets={
                "single": ["fast"],
                "mixture_of_agents": ["fast", "slow"],
            },
            scheduling_by_preset={
                "fast": ["sequential"],
                "slow": ["adaptive", "parallel", "waves", "sequential"],
            },
            max_active_workflows=broker_config.processing.max_active_workflows,
            max_parallel_invocations=scheduler.max_parallel_invocations(),
            exact_target_model=True,
            task_timeout=True,
        )

    @app.get("/api/v1/usage", response_model=UsageResponse)
    async def get_usage() -> UsageResponse:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return UsageResponse(month=month, providers={})

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return await _health_response(db, provider)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready(response: Response) -> HealthResponse:
        health_response = await _health_response(db, provider)
        if health_response.dependencies["sqlite"].status == "unavailable":
            response.status_code = 503
        return health_response

    return app


async def _dispatcher_loop(
    repository: TaskRepository,
    coordinator: ConsensusCoordinator,
    stop: asyncio.Event,
    interval_seconds: float,
) -> None:
    while not stop.is_set():
        task_id = repository.claim_next_queued_task_id()
        if task_id is not None:
            try:
                await coordinator.process_task(repository, task_id)
            except Exception as error:
                current = repository.get_task(task_id)
                if current.status in {TaskStatus.completed, TaskStatus.cancelled}:
                    continue
                repository.update_task(
                    task_id,
                    TaskStatus.failed,
                    progress={"phase": TaskStatus.failed.value},
                    error={
                        "code": "INTERNAL_ERROR",
                        "message": f"Dispatcher failure: {type(error).__name__}",
                        "retryable": False,
                    },
                    clear_queue_position=True,
                )
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass


async def _health_response(db: Database, provider) -> HealthResponse:
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
    dependencies = {"sqlite": sqlite}
    for name, check in (await provider.health()).items():
        dependencies[name] = HealthDependency(
            status=check["status"],
            checked_at=datetime.now(timezone.utc),
            detail=check.get("detail"),
            latency_ms=check.get("latency_ms"),
        )
    states = {dependency.status for dependency in dependencies.values()}
    if "unavailable" in states and sqlite.status != "unavailable":
        status = "degraded"
    elif "degraded" in states:
        status = "degraded"
    return HealthResponse(status=status, checked_at=checked_at, dependencies=dependencies)


app = create_app()
