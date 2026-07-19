from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.admin_auth import (
    LOOPBACK_HOSTS,
    AdminTokenLookupError,
    resolve_admin_token,
    verify_admin_access,
)
from app.config import BrokerConfig, load_config
from app.coordinator import ConsensusCoordinator
from app.dashboard import DashboardQueryRepository
from app.dashboard_web import (
    CSRF_COOKIE_NAME,
    create_dashboard_router,
    load_dashboard_resources,
    set_csrf_cookie,
    valid_csrf_token_shape,
)
from app.db import Database
from app.dispatcher import dispatcher_loop
from app.health import HealthCache, health_response
from app.ingestion import (
    ALLOWED_FORMATS,
    AttachmentError,
    IngestionError,
    IngestionService,
    UnsupportedFormat,
)
from app.ingestion.service import stream_upload_to_temp
from app.logging_config import configure_logging
from app.maintenance import (
    prune_ingested_files,
    prune_terminal_task_artifacts,
    prune_terminal_task_events,
)
from app.model_catalog import model_availability_item, model_feature_profile
from app.model_stats import load_model_stats
from app.providers import build_provider
from app.repository import IdempotencyConflict, QueueFull, TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.sandbox import SandboxExecutor
from app.schemas import (
    AGENT_SKILLS,
    DEFAULT_AGENT_SKILLS,
    BrokerCapabilitiesResponse,
    DashboardResourcesResponse,
    DashboardSummaryResponse,
    DashboardTaskDetail,
    DashboardTaskPage,
    ExecutionPreset,
    ExecutionStrategy,
    FileAcceptedResponse,
    FileStateResponse,
    FileStatus,
    HealthResponse,
    ModelAvailabilityResponse,
    ModelContextResponse,
    QueueReorderRequest,
    QueueResponse,
    SchedulingPolicy,
    TaskAcceptedResponse,
    TaskCreateRequest,
    TaskStateResponse,
    TaskStatus,
    ToolResultsRequest,
    UsageResponse,
)
from app.startup import (
    auto_start_local_provider_servers,
    detect_total_vram_gb,
    ensure_admin_credential_for_exposed_host,
    vram_budget_mismatch,
    zero_cost_cloud_providers,
)


def create_app(config: BrokerConfig | None = None, config_path: str | Path = "broker_config.yaml") -> FastAPI:
    """Única vía para construir la app; no existe instancia global a nivel de módulo.

    Cada llamada abre su propia BD y clientes de proveedores, así que crear apps
    implícitas al importar duplicaría recursos sin cierre. Producción:
    scripts/run_broker.py (propaga --config). Desarrollo con autoreload:
    `uvicorn app.main:create_app --factory`. config_path es además el YAML que
    el dashboard edita y recarga en caliente.
    """
    broker_config = config or load_config(config_path)
    ensure_admin_credential_for_exposed_host(broker_config)
    configure_logging(broker_config.logging)
    logger = logging.getLogger("ai_broker.http")
    db = Database(Path(broker_config.persistence.database), broker_config.persistence.journal_mode)
    repository = TaskRepository(db)
    dashboard_queries = DashboardQueryRepository(db)
    scheduler = ResourceScheduler(broker_config)
    # La selección adaptativa lee la evidencia operativa de esta misma BD;
    # el lambda difiere la consulta a cada selección (ventana configurable).
    provider = build_provider(
        broker_config,
        stats_loader=lambda: load_model_stats(db, window_days=broker_config.routing.stats_window_days),
    )
    ingestion = IngestionService(db, broker_config)
    # Siempre construido: lee sandbox.* en vivo, así que activarlo/desactivarlo
    # desde el panel de Configuración aplica sin reiniciar el broker.
    sandbox = SandboxExecutor(broker_config)
    coordinator = ConsensusCoordinator(
        db, scheduler, provider=provider, ingestion=ingestion, sandbox=sandbox,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if broker_config.server.host not in LOOPBACK_HOSTS:
            try:
                exposed_token = resolve_admin_token(broker_config)
            except AdminTokenLookupError:
                exposed_token = None
            if not exposed_token:
                # Solo alcanzable con allow_unauthenticated_lan=true: el guard
                # de arranque fail-closed ya rechazó el resto de casos.
                logger.warning(
                    "security.exposed_without_token",
                    extra={
                        "event": "security.exposed_without_token",
                        "host": broker_config.server.host,
                        "detail": (
                            "allow_unauthenticated_lan=true sin token admin: "
                            "la API completa queda abierta a la red"
                        ),
                    },
                )
        for provider_id in zero_cost_cloud_providers(broker_config):
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
        if broker_config.providers.ollama.enabled:
            detected_vram = await asyncio.to_thread(detect_total_vram_gb)
            vram_alert = vram_budget_mismatch(broker_config.resources.local_vram_budget_gb, detected_vram)
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
        await auto_start_local_provider_servers(broker_config, logger)
        db.init_schema()
        repository.recover_interrupted_tasks(max_attempts=broker_config.processing.max_task_attempts)
        ingestion.cleanup_incoming()
        if broker_config.ingestion.enabled:
            # Conversiones interrumpidas por un reinicio: se relanzan (idempotentes).
            for pending_file_id in ingestion.recover_pending():
                ingestion.launch(pending_file_id)
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
        pruned_files = prune_ingested_files(
            db,
            broker_config.ingestion.storage_dir,
            older_than_days=broker_config.persistence.files_retention_days,
        )
        if pruned_files:
            logger.info(
                "maintenance.files_pruned",
                extra={"event": "maintenance.files_pruned", "removed": pruned_files},
            )
        stop_dispatcher = asyncio.Event()
        dispatcher_task = None
        if broker_config.processing.auto_dispatch:
            dispatcher_task = asyncio.create_task(
                dispatcher_loop(
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
            await ingestion.shutdown()
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
    app.state.ingestion = ingestion

    def _dispatcher_state() -> str | None:
        if not broker_config.processing.auto_dispatch:
            return None
        task = getattr(app.state, "dispatcher_task", None)
        if task is None or task.done():
            return "stopped"
        return "running"

    # Caché de dependencias de salud compartida por /health, /health/ready y
    # el panel; vive lo que la app y usa los TTL de config.health.
    health_cache: HealthCache = {}

    async def _health_snapshot() -> HealthResponse:
        return await health_response(db, provider, _dispatcher_state(), broker_config, health_cache)

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
    async def renew_dashboard_csrf_cookie(request: Request, call_next):
        response = await call_next(request)
        # Caducidad deslizante: cualquier tráfico del panel (fragmentos de
        # refresco incluidos) renueva el max_age de la cookie CSRF, para que
        # una pestaña abierta y activa no acabe en 403 por cookie expirada.
        if request.url.path.startswith("/dashboard") and "set-cookie" not in response.headers:
            token = request.cookies.get(CSRF_COOKIE_NAME)
            if valid_csrf_token_shape(token):
                set_csrf_cookie(response, token)
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
            health_loader=_health_snapshot,
            ingestion=ingestion,
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
        if not broker_config.sandbox.enabled and (
            "run_code" in payload.execution.agent.skills
            or "run_code" in payload.execution.proposer_skills
        ):
            # Fail-fast: sin sandbox la skill solo produciría errores de tool
            # en cada iteración del agente, gastando presupuesto.
            raise HTTPException(status_code=409, detail="SANDBOX_DISABLED")
        if payload.content.attachments:
            if not broker_config.ingestion.enabled:
                raise HTTPException(status_code=409, detail="INGESTION_DISABLED")
            try:
                # Fail-fast: no se encola una tarea cuyos adjuntos no están 'ready';
                # el cliente sondea GET /api/v1/files/{id} hasta entonces.
                ingestion.check_attachments(payload)
            except AttachmentError as exc:
                status = 404 if exc.code == "ATTACHED_FILE_NOT_FOUND" else 409
                raise HTTPException(
                    status_code=status, detail={"code": exc.code, "message": str(exc)},
                ) from exc
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
    def get_task(task_id: str, request: Request) -> TaskStateResponse:
        # La respuesta incluye result/progress (salida completa del modelo):
        # con token configurado es una lectura protegida, igual que las mutaciones.
        verify_admin_access(request, broker_config)
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

    @app.post("/api/v1/tasks/{task_id}/tool_results", response_model=TaskStateResponse)
    def submit_tool_results(task_id: str, payload: ToolResultsRequest, request: Request) -> TaskStateResponse:
        """Passthrough: el cliente entrega los resultados de sus tools de dominio
        y el broker reanuda el bucle del agente re-encolando la tarea."""
        verify_admin_access(request, broker_config)
        try:
            repository.resume_with_tool_results(
                task_id, [item.model_dump() for item in payload.tool_results],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return repository.get_task(task_id)

    def _file_state_response(record) -> FileStateResponse:
        return FileStateResponse(
            file_id=record.id,
            status=FileStatus(record.status),
            filename=record.filename,
            kind=record.kind,
            engine=record.engine,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            meta=record.meta,
            error=record.error,
            created_at=datetime.fromisoformat(record.created_at),
            updated_at=datetime.fromisoformat(record.updated_at),
            markdown_url=(
                f"/api/v1/files/{record.id}/markdown" if record.status == "ready" else None
            ),
        )

    @app.post("/api/v1/files", response_model=FileAcceptedResponse, status_code=202)
    async def upload_file(request: Request, file: UploadFile) -> FileAcceptedResponse:
        verify_admin_access(request, broker_config)
        if not broker_config.ingestion.enabled:
            raise HTTPException(status_code=409, detail="INGESTION_DISABLED")
        max_bytes = broker_config.ingestion.max_file_mb * 1024 * 1024
        try:
            # Streaming a disco por chunks: un vídeo de gigabytes no pasa por RAM
            # y el límite corta la subida en cuanto se supera.
            temp_path = await stream_upload_to_temp(file, max_bytes, ingestion.incoming_dir)
        except IngestionError as exc:
            raise HTTPException(status_code=413, detail="INGEST_TOO_LARGE") from exc
        try:
            record, created = await asyncio.to_thread(
                ingestion.store_upload_from_file, file.filename or "fichero", temp_path,
            )
        except (IngestionError, UnsupportedFormat) as exc:
            status = 415 if exc.code in {"INGEST_UNSUPPORTED_FORMAT", "INGEST_CONTENT_MISMATCH"} else 422
            raise HTTPException(
                status_code=status, detail={"code": exc.code, "message": str(exc)},
            ) from exc
        if created:
            ingestion.launch(record.id)
        return FileAcceptedResponse(
            file_id=record.id,
            status=FileStatus(record.status),
            filename=record.filename,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            created=created,
            status_url=f"/api/v1/files/{record.id}",
        )

    @app.get("/api/v1/files/{file_id}", response_model=FileStateResponse)
    def get_file(file_id: str, request: Request) -> FileStateResponse:
        verify_admin_access(request, broker_config)
        record = ingestion.get(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="FILE_NOT_FOUND")
        return _file_state_response(record)

    @app.get("/api/v1/files/{file_id}/markdown")
    def get_file_markdown(file_id: str, request: Request) -> PlainTextResponse:
        # Devuelve el documento convertido íntegro: lectura protegida, como
        # los resultados de tareas.
        verify_admin_access(request, broker_config)
        record = ingestion.get(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="FILE_NOT_FOUND")
        if record.status != "ready" or not record.markdown_path:
            raise HTTPException(status_code=409, detail="FILE_NOT_READY")
        content = Path(record.markdown_path).read_text(encoding="utf-8")
        return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")

    @app.get("/api/v1/queue", response_model=QueueResponse)
    def get_queue() -> QueueResponse:
        # Lectura abierta a propósito: solo ids, estados y posiciones — sin
        # prompts ni resultados. Reordenar (PATCH) sí exige credencial.
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
            model_availability_item(entry, health)
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
            **model_feature_profile(entry),
            compatibility=entry.get("compatibility"),
            compatibility_checked_at=entry.get("compatibility_checked_at"),
            compatibility_error=entry.get("compatibility_error"),
        )

    @app.get("/api/v1/capabilities", response_model=BrokerCapabilitiesResponse)
    async def capabilities() -> BrokerCapabilitiesResponse:
        return BrokerCapabilitiesResponse(
            contract_version="2.5",
            strategies=[
                ExecutionStrategy.single,
                ExecutionStrategy.mixture_of_agents,
                ExecutionStrategy.agent,
            ] + ([ExecutionStrategy.auto] if broker_config.strategy_router.enabled else []),
            presets={
                "single": [ExecutionPreset.fast],
                "mixture_of_agents": [ExecutionPreset.fast, ExecutionPreset.slow],
                "agent": [ExecutionPreset.fast],
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
            prompt_compression_override=True,
            agent_skills=list(AGENT_SKILLS if broker_config.sandbox.enabled else DEFAULT_AGENT_SKILLS),
            sandbox_run_code=broker_config.sandbox.enabled,
            proposer_skills=True,
            client_tool_passthrough=True,
            auto_strategy=broker_config.strategy_router.enabled,
            confidence_escalation=(
                broker_config.strategy_router.enabled
                and broker_config.strategy_router.confidence_escalation
            ),
            adaptive_strategy_learning=(
                broker_config.strategy_router.enabled
                and broker_config.strategy_router.adaptive_learning
            ),
            file_ingestion=broker_config.ingestion.enabled,
            ingestion_formats=ALLOWED_FORMATS if broker_config.ingestion.enabled else {},
            long_context_map_reduce=True,
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
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        status: TaskStatus | None = None,
        origin: str | None = Query(default=None, min_length=1, max_length=64),
    ) -> DashboardTaskPage:
        verify_admin_access(request, broker_config)
        return dashboard_queries.list_tasks(
            page=page,
            page_size=page_size,
            status=status,
            origin=origin,
        )

    @app.get("/api/v1/dashboard/tasks/{task_id}", response_model=DashboardTaskDetail)
    def dashboard_task_detail(task_id: str, request: Request) -> DashboardTaskDetail:
        # Devuelve request_json (el prompt íntegro) y result_json: lectura
        # protegida cuando hay token configurado.
        verify_admin_access(request, broker_config)
        try:
            return dashboard_queries.task_detail(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error

    @app.get("/api/v1/dashboard/resources", response_model=DashboardResourcesResponse)
    async def dashboard_resources() -> DashboardResourcesResponse:
        return await load_dashboard_resources(provider, scheduler, broker_config)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return await _health_snapshot()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready(response: Response) -> HealthResponse:
        snapshot = await _health_snapshot()
        # No listo sin SQLite ni con el bucle de despacho muerto: aceptar
        # tareas que nadie va a despachar sería un 200 engañoso. Los
        # proveedores caídos solo degradan: encolar sigue siendo válido.
        dispatcher = snapshot.dependencies.get("dispatcher")
        if snapshot.dependencies["sqlite"].status == "unavailable" or (
            dispatcher is not None and dispatcher.status == "unavailable"
        ):
            response.status_code = 503
        return snapshot

    return app

