from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import BrokerConfig, load_config
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.dashboard import DashboardQueryRepository
from app.dashboard_web import create_dashboard_router, load_dashboard_resources
from app.logging_config import configure_logging
from app.providers import build_provider
from app.repository import IdempotencyConflict, QueueFull, TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import (
    BrokerCapabilitiesResponse,
    DashboardResourcesResponse,
    DashboardSummaryResponse,
    DashboardTaskDetail,
    DashboardTaskPage,
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


def create_app(config: BrokerConfig | None = None, config_path: str | Path = "broker_config.yaml") -> FastAPI:
    broker_config = config or load_config(config_path)
    configure_logging(broker_config.logging)
    logger = logging.getLogger("ai_broker.http")
    db = Database(Path(broker_config.persistence.database), broker_config.persistence.journal_mode)
    repository = TaskRepository(db)
    dashboard_queries = DashboardQueryRepository(db)
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
    app.state.dashboard_queries = dashboard_queries
    app.state.scheduler = scheduler
    app.state.coordinator = coordinator
    app.state.provider = provider

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        return response

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        logger.info(
            "http.request",
            extra={
                "event": "http.request",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            },
        )
        return response

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(
        create_dashboard_router(
            queries=dashboard_queries,
            repository=repository,
            coordinator=coordinator,
            provider=provider,
            scheduler=scheduler,
            config=broker_config,
            config_path=Path(config_path),
            health_loader=lambda: _health_response(db, provider),
        )
    )

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
    async def get_usage(month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$")) -> UsageResponse:
        selected_month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            return dashboard_queries.usage(selected_month)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="INVALID_MONTH") from error

    @app.get("/api/v1/dashboard/summary", response_model=DashboardSummaryResponse)
    async def dashboard_summary(
        window_hours: int = Query(default=24, ge=1, le=24 * 90),
    ) -> DashboardSummaryResponse:
        return dashboard_queries.summary(window_hours=window_hours)

    @app.get("/api/v1/dashboard/tasks", response_model=DashboardTaskPage)
    async def dashboard_tasks(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        status: TaskStatus | None = None,
        origin: str | None = Query(default=None, min_length=1, max_length=64),
    ) -> DashboardTaskPage:
        return dashboard_queries.list_tasks(
            page=page,
            page_size=page_size,
            status=status,
            origin=origin,
        )

    @app.get("/api/v1/dashboard/tasks/{task_id}", response_model=DashboardTaskDetail)
    async def dashboard_task_detail(task_id: str) -> DashboardTaskDetail:
        try:
            return dashboard_queries.task_detail(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error

    @app.get("/api/v1/dashboard/resources", response_model=DashboardResourcesResponse)
    async def dashboard_resources() -> DashboardResourcesResponse:
        return await load_dashboard_resources(provider, scheduler, broker_config)

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
