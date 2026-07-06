from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.admin_auth import resolve_admin_token, verify_admin_access
from app.config import BrokerConfig, load_config
from app.coordinator import ConsensusCoordinator
from app.dashboard import DashboardQueryRepository
from app.dashboard_web import create_dashboard_router, load_dashboard_resources
from app.db import Database
from app.logging_config import configure_logging
from app.maintenance import prune_terminal_task_artifacts, prune_terminal_task_events
from app.providers import build_provider
from app.repository import IdempotencyConflict, QueueFull, TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import (
    BrokerCapabilitiesResponse,
    DashboardResourcesResponse,
    DashboardSummaryResponse,
    DashboardTaskDetail,
    DashboardTaskPage,
    ExecutionPreset,
    ExecutionStrategy,
    HealthDependency,
    HealthResponse,
    ModelAvailabilityItem,
    ModelAvailabilityResponse,
    ModelContextResponse,
    QueueReorderRequest,
    QueueResponse,
    SchedulingPolicy,
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
        if broker_config.server.host not in {"127.0.0.1", "localhost", "::1"} and not resolve_admin_token(broker_config):
            logger.warning(
                "security.exposed_without_token",
                extra={
                    "event": "security.exposed_without_token",
                    "host": broker_config.server.host,
                    "detail": "El servidor escucha fuera de loopback sin token admin: la API mutable queda abierta a la red",
                },
            )
        for provider_id in _zero_cost_cloud_providers(broker_config):
            logger.warning(
                "providers.cloud_zero_cost",
                extra={
                    "event": "providers.cloud_zero_cost",
                    "provider": provider_id,
                    "detail": (
                        f"El proveedor cloud '{provider_id}' no tiene precios configurados: "
                        "el coste reportado será 0 y el presupuesto (max_cost_usd) nunca cortará"
                    ),
                },
            )
        if broker_config.providers.ollama.enabled or broker_config.providers.huggingface_local.enabled:
            detected_vram = await asyncio.to_thread(_detect_total_vram_gb)
            vram_alert = _vram_budget_mismatch(broker_config.resources.local_vram_budget_gb, detected_vram)
            if vram_alert:
                logger.warning(
                    "resources.vram_budget_mismatch",
                    extra={
                        "event": "resources.vram_budget_mismatch",
                        "budget_gb": broker_config.resources.local_vram_budget_gb,
                        "detected_gb": detected_vram,
                        "detail": vram_alert,
                    },
                )
        await _auto_start_local_provider_servers(broker_config, logger)
        db.init_schema()
        repository.recover_interrupted_tasks(max_attempts=broker_config.processing.max_task_attempts)
        pruned_events = prune_terminal_task_events(
            db, older_than_days=broker_config.persistence.events_retention_days
        )
        if pruned_events:
            logger.info(
                "maintenance.events_pruned",
                extra={"event": "maintenance.events_pruned", "removed": pruned_events},
            )
        pruned_artifacts = prune_terminal_task_artifacts(
            db,
            coordinator.artifacts.root,
            older_than_days=broker_config.persistence.artifacts_retention_days,
        )
        if pruned_artifacts:
            logger.info(
                "maintenance.artifacts_pruned",
                extra={"event": "maintenance.artifacts_pruned", "removed": pruned_artifacts},
            )
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
        app.state.dispatcher_task = dispatcher_task
        try:
            yield
        finally:
            stop_dispatcher.set()
            if dispatcher_task is not None:
                await asyncio.gather(dispatcher_task, return_exceptions=True)
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

    def _dispatcher_state() -> str | None:
        if not broker_config.processing.auto_dispatch:
            return None
        task = getattr(app.state, "dispatcher_task", None)
        if task is None or task.done():
            return "stopped"
        return "running"

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
            health_loader=lambda: _health_response(db, provider, _dispatcher_state()),
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
    def create_task(payload: TaskCreateRequest, response: Response, request: Request) -> TaskAcceptedResponse:
        verify_admin_access(request, broker_config)
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
    def get_task(task_id: str) -> TaskStateResponse:
        try:
            return repository.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from exc

    @app.delete("/api/v1/tasks/{task_id}", response_model=TaskStateResponse)
    def cancel_task(task_id: str, request: Request) -> TaskStateResponse:
        verify_admin_access(request, broker_config)
        try:
            return repository.request_cancel(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from exc

    @app.get("/api/v1/queue", response_model=QueueResponse)
    def get_queue() -> QueueResponse:
        return repository.list_queue()

    @app.patch("/api/v1/queue", response_model=QueueResponse)
    def reorder_queue(payload: QueueReorderRequest, request: Request) -> QueueResponse:
        verify_admin_access(request, broker_config)
        try:
            return repository.reorder_queue(payload.task_ids)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/dispatcher/tick")
    async def dispatcher_tick(request: Request) -> dict[str, str | None]:
        verify_admin_access(request, broker_config)
        task_id = await coordinator.process_next(repository)
        return {"task_id": task_id}

    @app.get("/api/v1/models")
    async def list_models() -> dict[str, object]:
        return {"models": await provider.models()}

    @app.get("/api/v1/models/availability", response_model=ModelAvailabilityResponse)
    async def model_availability(
        provider_filter: str | None = Query(default=None, alias="provider", min_length=1, max_length=64),
        deployment_filter: str | None = Query(default=None, alias="deployment", min_length=1, max_length=64),
        capability: str | None = Query(default=None, min_length=1, max_length=64),
        only_dispatchable: bool = Query(default=False),
    ) -> ModelAvailabilityResponse:
        checked_at = datetime.now(timezone.utc)
        catalog = await provider.models()
        health = await provider.health()
        items = [
            _model_availability_item(entry, health)
            for entry in catalog
            if provider_filter is None or str(entry.get("provider") or "").lower() == provider_filter.lower()
            if deployment_filter is None or str(entry.get("deployment") or "").lower() == deployment_filter.lower()
            if capability is None or capability.lower() in {str(item).lower() for item in entry.get("capabilities") or []}
        ]
        if only_dispatchable:
            items = [item for item in items if item.dispatchable]
        counts = {"online": 0, "offline": 0, "unknown": 0, "incompatible": 0, "dispatchable": 0}
        for item in items:
            counts[item.availability] += 1
            if item.dispatchable:
                counts["dispatchable"] += 1
        return ModelAvailabilityResponse(checked_at=checked_at, items=items, counts=counts)

    @app.get("/api/v1/models/context", response_model=ModelContextResponse)
    async def model_context(
        provider_id: str = Query(alias="provider", min_length=1, max_length=64),
        deployment: str = Query(min_length=1, max_length=64),
        model: str = Query(min_length=1, max_length=128),
    ) -> ModelContextResponse:
        catalog = await provider.models()
        entry = next(
            (
                item for item in catalog
                if str(item.get("provider") or "").lower() == provider_id.lower()
                and str(item.get("deployment") or "").lower() == deployment.lower()
                and str(item.get("name") or "") == model
            ),
            None,
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="MODEL_NOT_FOUND")
        context_window = entry.get("context_window")
        try:
            context_window = int(context_window) if context_window is not None else None
        except (TypeError, ValueError):
            context_window = None
        return ModelContextResponse(
            provider=str(entry.get("provider") or provider_id),
            deployment=str(entry.get("deployment") or deployment),
            model=str(entry.get("name") or model),
            context_window=context_window,
            context_window_known=context_window is not None,
            capabilities=list(entry.get("capabilities") or []),
            **_model_feature_profile(entry),
            compatibility=entry.get("compatibility"),
            compatibility_checked_at=entry.get("compatibility_checked_at"),
            compatibility_error=entry.get("compatibility_error"),
        )

    @app.get("/api/v1/capabilities", response_model=BrokerCapabilitiesResponse)
    async def capabilities() -> BrokerCapabilitiesResponse:
        return BrokerCapabilitiesResponse(
            contract_version="2.1",
            strategies=[ExecutionStrategy.single, ExecutionStrategy.mixture_of_agents],
            presets={
                "single": [ExecutionPreset.fast],
                "mixture_of_agents": [ExecutionPreset.fast, ExecutionPreset.slow],
            },
            scheduling_by_preset={
                "fast": [SchedulingPolicy.sequential],
                "slow": [
                    SchedulingPolicy.adaptive,
                    SchedulingPolicy.parallel,
                    SchedulingPolicy.waves,
                    SchedulingPolicy.sequential,
                ],
            },
            max_active_workflows=broker_config.processing.max_active_workflows,
            max_parallel_invocations=scheduler.max_parallel_invocations(),
            exact_target_model=True,
            task_timeout=True,
        )

    @app.get("/api/v1/usage", response_model=UsageResponse)
    def get_usage(month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$")) -> UsageResponse:
        selected_month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            return dashboard_queries.usage(selected_month)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="INVALID_MONTH") from error

    @app.get("/api/v1/dashboard/summary", response_model=DashboardSummaryResponse)
    def dashboard_summary(
        window_hours: int = Query(default=24, ge=1, le=24 * 90),
    ) -> DashboardSummaryResponse:
        return dashboard_queries.summary(window_hours=window_hours)

    @app.get("/api/v1/dashboard/tasks", response_model=DashboardTaskPage)
    def dashboard_tasks(
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
    def dashboard_task_detail(task_id: str) -> DashboardTaskDetail:
        try:
            return dashboard_queries.task_detail(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error

    @app.get("/api/v1/dashboard/resources", response_model=DashboardResourcesResponse)
    async def dashboard_resources() -> DashboardResourcesResponse:
        return await load_dashboard_resources(provider, scheduler, broker_config)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return await _health_response(db, provider, _dispatcher_state())

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready(response: Response) -> HealthResponse:
        health_response = await _health_response(db, provider, _dispatcher_state())
        if health_response.dependencies["sqlite"].status == "unavailable":
            response.status_code = 503
        return health_response

    return app


_VRAM_DETECTION_CACHE: dict[str, float | None] = {}


def _detect_total_vram_gb() -> float | None:
    """VRAM total de las GPU NVIDIA visibles, o None si no se puede detectar (cacheado)."""
    import subprocess

    if "value" in _VRAM_DETECTION_CACHE:
        return _VRAM_DETECTION_CACHE["value"]
    _VRAM_DETECTION_CACHE["value"] = None
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            totals = [float(line.strip()) for line in proc.stdout.splitlines() if line.strip()]
            if totals:
                _VRAM_DETECTION_CACHE["value"] = sum(totals) / 1024
    except Exception:
        pass
    return _VRAM_DETECTION_CACHE["value"]


def _vram_budget_mismatch(budget_gb: float, detected_gb: float | None) -> str | None:
    """Mensaje de alerta si el presupuesto configurado no cabe en la VRAM real."""
    if detected_gb is None or detected_gb <= 0:
        return None
    if budget_gb > detected_gb * 1.5:
        return (
            f"resources.local_vram_budget_gb={budget_gb:.0f} GB pero la VRAM detectada es "
            f"{detected_gb:.1f} GB: el planificador sobresuscribirá la GPU (olas paralelas "
            "imposibles, OOM y timeouts en cascada). Ajusta el presupuesto a la VRAM real."
        )
    return None


def _zero_cost_cloud_providers(config: BrokerConfig) -> list[str]:
    """Providers cloud habilitados sin precios: su gasto real será invisible (coste 0)."""
    zero: list[str] = []
    deepseek = config.providers.deepseek
    if deepseek.enabled and not deepseek.input_cost_per_million and not deepseek.output_cost_per_million:
        zero.append("deepseek")
    for item in config.providers.custom:
        if not item.enabled or item.deployment != "cloud":
            continue
        model_costs = any(
            model.input_cost_per_million or model.output_cost_per_million for model in item.models
        )
        if not item.input_cost_per_million and not item.output_cost_per_million and not model_costs:
            zero.append(item.id)
    return zero


async def _auto_start_local_provider_servers(config: BrokerConfig, logger: logging.Logger) -> None:
    for provider in config.providers.custom:
        if not provider.enabled or not provider.auto_start:
            continue
        if provider.deployment != "local":
            logger.warning(
                "provider.auto_start_ignored",
                extra={"provider": provider.id, "reason": "deployment_not_local"},
            )
            continue
        if provider.id.lower() not in {"lmstudio", "lm_studio"}:
            logger.warning(
                "provider.auto_start_ignored",
                extra={"provider": provider.id, "reason": "unsupported_provider"},
            )
            continue
        await _ensure_lmstudio_server(provider.base_url, logger)


async def _ensure_lmstudio_server(base_url: str, logger: logging.Logger) -> None:
    parsed = urlparse(base_url)
    port = parsed.port or 1234
    status = await _run_process(["lms", "server", "status"], timeout_seconds=10)
    status_text = f"{status['stdout']}\n{status['stderr']}".strip()
    if status["returncode"] == 0 and "not running" not in status_text.lower():
        logger.info("provider.auto_start_skipped", extra={"provider": "lmstudio", "detail": status_text})
        return

    started = await _run_process(["lms", "server", "start", "--port", str(port)], timeout_seconds=30)
    output = f"{started['stdout']}\n{started['stderr']}".strip()
    if started["returncode"] != 0:
        logger.warning(
            "provider.auto_start_failed",
            extra={"provider": "lmstudio", "returncode": started["returncode"], "detail": output},
        )
        return
    logger.info("provider.auto_started", extra={"provider": "lmstudio", "port": port, "detail": output})


async def _run_process(args: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        return {
            "returncode": int(process.returncode or 0),
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"No se encontro el ejecutable: {args[0]}"}
    except TimeoutError:
        return {"returncode": 124, "stdout": "", "stderr": "Timeout al ejecutar el comando"}


async def _dispatcher_loop(
    repository: TaskRepository,
    coordinator: ConsensusCoordinator,
    stop: asyncio.Event,
    interval_seconds: float,
) -> None:
    logger = logging.getLogger("ai_broker.dispatcher")
    while not stop.is_set():
        # Ningún fallo de una iteración puede matar el bucle: sin dispatcher
        # las tareas se acumularían en cola con el servidor aparentemente sano.
        try:
            task_id = await asyncio.to_thread(repository.claim_next_queued_task_id)
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
                continue
        except Exception:
            logger.exception("dispatcher.iteration_failed", extra={"event": "dispatcher.iteration_failed"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(interval_seconds, 1.0))
            except TimeoutError:
                pass
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def _health_response(db: Database, provider, dispatcher_state: str | None = None) -> HealthResponse:
    checked_at = datetime.now(timezone.utc)
    status: Literal["healthy", "degraded", "unavailable"]
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
    if dispatcher_state is not None:
        running = dispatcher_state == "running"
        dependencies["dispatcher"] = HealthDependency(
            status="healthy" if running else "unavailable",
            checked_at=datetime.now(timezone.utc),
            detail="Bucle de despacho activo" if running else "El bucle de despacho no está en ejecución",
        )
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


def _model_availability_item(entry: dict[str, Any], health: dict[str, dict[str, Any]]) -> ModelAvailabilityItem:
    provider_name = str(entry.get("provider") or "unknown")
    deployment_name = str(entry.get("deployment") or "unknown")
    raw_provider_status = str(
        (
            health.get(provider_name.lower())
            or health.get(provider_name)
            or health.get(deployment_name.lower())
            or health.get(deployment_name)
            or {}
        ).get("status")
        or "unknown"
    )
    provider_status = cast(
        'Literal["healthy", "degraded", "unavailable", "unknown"]',
        raw_provider_status if raw_provider_status in {"healthy", "degraded", "unavailable"} else "unknown",
    )
    model_status = str(entry.get("status") or "unknown")
    compatibility = str(entry.get("compatibility") or "unknown")
    capabilities = [str(item) for item in entry.get("capabilities") or []]
    context_window = entry.get("context_window")
    try:
        context_window = int(context_window) if context_window is not None else None
    except (TypeError, ValueError):
        context_window = None

    availability: Literal["online", "offline", "unknown", "incompatible"]
    if provider_status == "unavailable":
        availability = "offline"
        dispatchable = False
        reason = "Proveedor no disponible en este momento."
    elif compatibility == "incompatible":
        availability = "incompatible"
        dispatchable = False
        reason = entry.get("compatibility_error") or "Modelo marcado como incompatible con el endpoint de inferencia."
    elif compatibility == "compatible":
        available_capabilities = {item.lower() for item in capabilities}
        availability = "online"
        dispatchable = bool({"completion", "embedding"}.intersection(available_capabilities))
        reason = (
            "Modelo compatible y proveedor disponible."
            if dispatchable
            else "Modelo disponible, pero no declara una capacidad ejecutable por el Broker."
        )
    elif model_status in {"available", "online"} and provider_status in {"healthy", "degraded"}:
        availability = "unknown"
        dispatchable = False
        reason = "Proveedor disponible, pero compatibilidad del modelo no comprobada."
    else:
        availability = "unknown"
        dispatchable = False
        reason = "No hay suficiente informacion para confirmar disponibilidad operativa."

    return ModelAvailabilityItem(
        provider=provider_name,
        deployment=deployment_name,
        model=str(entry.get("name") or entry.get("model") or "unknown"),
        availability=availability,
        dispatchable=dispatchable,
        reason=str(reason),
        provider_status=provider_status,
        model_status=model_status,
        compatibility=compatibility,
        capabilities=capabilities,
        context_window=context_window,
        compatibility_error=entry.get("compatibility_error"),
    )


def _model_feature_profile(entry: dict[str, Any]) -> dict[str, Any]:
    raw_capabilities = {str(item).lower() for item in entry.get("capabilities") or []}
    model_name = str(entry.get("name") or "").lower()
    provider_name = str(entry.get("provider") or "").lower()
    compatibility = str(entry.get("compatibility") or "unknown").lower()
    notes: list[str] = []

    def status(*names: str, default: str = "unknown") -> str:
        return "supported" if raw_capabilities.intersection(names) else default

    def name_hints(*hints: str) -> bool:
        return any(item in model_name for item in hints)

    features: dict[str, dict[str, str]] = {
        "modalities": {
            "text_input": status("completion", "chat", "text", default="supported"),
            "text_output": status("completion", "chat", "text", default="supported"),
            "image_input": status("vision", "image", "multimodal", "visual"),
            "image_output": status("image_generation", "image-output", "text-to-image"),
            "audio_input": status("audio", "speech", "transcription", "asr"),
            "audio_output": status("tts", "speech_output", "audio-output"),
            "video_input": status("video", "video_input"),
            "video_output": status("video_generation", "video-output", "text-to-video"),
            "embedding_output": status("embedding", "embeddings"),
            "multimodal_input": status("multimodal", "omni"),
        },
        "files": {
            "file_upload": status("file", "files", "document", "pdf", "attachment"),
            "pdf_input": status("pdf", "document"),
            "document_input": status("document", "file", "files"),
            "spreadsheet_input": status("spreadsheet", "csv", "xlsx"),
            "presentation_input": status("ppt", "pptx", "presentation"),
            "archive_input": status("zip", "archive"),
            "image_file_input": status("image", "vision", "multimodal"),
            "audio_file_input": status("audio", "speech", "asr"),
            "video_file_input": status("video"),
        },
        "tools": {
            "function_calling": status("tools", "tool_calling", "function_calling", "functions"),
            "parallel_tool_calls": status("parallel_tool_calls"),
            "tool_choice": status("tool_choice", "tools"),
            "web_search": status("web_search", "search", "browser"),
            "deep_research": status("deep_research", "deep_search", "research"),
            "code_execution": status("code", "code_execution", "python"),
            "retrieval": status("retrieval", "rag", "vector_search"),
            "computer_use": status("computer_use", "desktop", "browser_control"),
            "mcp_tools": status("mcp", "tools"),
        },
        "understanding": {
            "ocr": status("ocr", "vision", "image", "multimodal"),
            "chart_understanding": status("chart", "vision", "image", "multimodal"),
            "table_understanding": status("table", "spreadsheet", "document"),
            "diagram_understanding": status("diagram", "vision", "image", "multimodal"),
            "math": status("math", "reasoning"),
            "coding": status("code", "coding", "programming"),
            "scientific_reasoning": status("science", "reasoning"),
            "legal_reasoning": status("legal"),
            "medical_reasoning": status("medical"),
            "financial_reasoning": status("finance", "financial"),
            "multilingual": status("multilingual", "translation"),
            "translation": status("translation", "multilingual"),
        },
        "reasoning": {
            "reasoning_optimized": status("reasoning", "thinking"),
            "chain_of_thought_private": status("reasoning", "thinking"),
            "planning": status("planning", "agent", "agentic"),
            "self_reflection": status("reflection", "critique", "reasoning"),
            "agentic": status("agent", "agentic", "tool_calling", "tools"),
            "mixture_compatible": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else "unknown",
        },
        "generation": {
            "chat_completions": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else status("completion", "chat"),
            "json_mode": status("json", "structured_output", "response_format"),
            "json_schema": status("json_schema", "structured_output"),
            "structured_outputs": status("structured_output", "json_schema"),
            "streaming": status("streaming", "stream"),
            "text_classification": status("classification"),
            "summarization": status("summarization", "summary", "completion"),
            "reranking": status("rerank", "reranking"),
            "moderation": status("moderation", "safety"),
        },
        "memory_and_state": {
            "conversation_state": status("stateful", "conversation_state"),
            "long_term_memory": status("memory", "long_term_memory"),
            "prompt_caching": status("prompt_cache", "caching", "cache"),
        },
        "operations": {
            "batch_inference": status("batch", "batch_inference"),
            "fine_tuning": status("fine_tuning", "finetune"),
            "distillation": status("distillation"),
            "quantized": status("quantized", "quantization"),
            "deterministic_seed": status("seed", "deterministic"),
            "logprobs": status("logprobs"),
            "token_counting": status("token_counting", "tokenizer"),
        },
        "deployment": {
            "local_execution": "supported" if str(entry.get("deployment") or "").lower() == "local" else "unsupported",
            "cloud_execution": "supported" if str(entry.get("deployment") or "").lower() in {"cloud", "api"} else "unsupported",
            "offline_capable": "supported" if str(entry.get("deployment") or "").lower() == "local" else "unknown",
            "privacy_boundary_local": "supported" if str(entry.get("deployment") or "").lower() == "local" else "unsupported",
        },
        "safety": {
            "safety_tuned": status("safety", "moderation", "guardrails"),
            "policy_guardrails": status("guardrails", "moderation", "safety"),
            "citation_grounding": status("citations", "grounding"),
        },
        "broker_support": {
            "single_prompt": "supported" if "completion" in raw_capabilities or compatibility == "compatible" else "unknown",
            "mixture_proposer": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else "unknown",
            "mixture_arbiter": "supported" if compatibility == "compatible" else "unsupported" if compatibility == "incompatible" else "unknown",
            "embedding_task": "supported" if "embedding" in raw_capabilities else "unsupported",
        },
    }

    if name_hints("vision", "vl", "visual", "multimodal", "omni", "phi-4-multimodal", "fuyu", "kosmos"):
        features["modalities"]["image_input"] = "supported"
        features["modalities"]["multimodal_input"] = "supported"
        features["files"]["image_file_input"] = "supported"
        features["understanding"]["ocr"] = "supported"
        features["understanding"]["chart_understanding"] = "supported"
        features["understanding"]["diagram_understanding"] = "supported"
        notes.append("image_input inferido por el nombre del modelo.")
    if name_hints("audio", "speech", "whisper", "asr", "tts"):
        features["modalities"]["audio_input"] = "supported"
        features["files"]["audio_file_input"] = "supported"
        notes.append("audio_input inferido por el nombre del modelo.")
    if name_hints("video"):
        features["modalities"]["video_input"] = "supported"
        features["files"]["video_file_input"] = "supported"
        notes.append("video_input inferido por el nombre del modelo.")
    if name_hints("embed", "embedding", "bge", "e5"):
        features["modalities"]["embedding_output"] = "supported"
        notes.append("embedding_output inferido por el nombre del modelo.")
    if name_hints("reason", "r1", "qwq", "thinking"):
        features["reasoning"]["reasoning_optimized"] = "supported"
        features["reasoning"]["chain_of_thought_private"] = "supported"
        notes.append("reasoning_optimized inferido por el nombre del modelo.")
    if name_hints("coder", "code", "starcoder", "codestral", "deepseek-coder", "devstral"):
        features["understanding"]["coding"] = "supported"
        notes.append("coding inferido por el nombre del modelo.")
    if name_hints("math", "qwq", "qwen"):
        features["understanding"]["math"] = "supported"
        notes.append("math inferido por el nombre del modelo.")
    if name_hints("translate", "nllb", "seamless", "multilingual", "aya", "sea-lion"):
        features["understanding"]["translation"] = "supported"
        features["understanding"]["multilingual"] = "supported"
        notes.append("multilingual inferido por el nombre del modelo.")
    if name_hints("guard", "safety", "moderation", "shield"):
        features["safety"]["safety_tuned"] = "supported"
        features["safety"]["policy_guardrails"] = "supported"
        notes.append("safety_tuned inferido por el nombre del modelo.")
    if name_hints("rerank", "reranker"):
        features["generation"]["reranking"] = "supported"
        notes.append("reranking inferido por el nombre del modelo.")
    if provider_name in {"ollama", "deepseek"} or compatibility in {"compatible", "incompatible"}:
        notes.append("Las capacidades no declaradas por el proveedor se devuelven como unknown.")

    return {"features": features, "feature_notes": notes}


app = create_app()
