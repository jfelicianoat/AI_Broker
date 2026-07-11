from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.admin_auth import (
    ADMIN_COOKIE_NAME,
    ADMIN_SESSION_SECONDS,
    AdminTokenLookupError,
    LoginThrottle,
    admin_cookie_value,
    resolve_admin_token,
    verify_admin_access,
)
from app.config import (
    BrokerConfig,
    OpenAICompatibleProviderConfig,
    save_config,
)
from app.coordinator import ConsensusCoordinator
from app.dashboard import DashboardQueryRepository
from app.dashboard_filters import register_filters
from app.dashboard_forms import (
    PromptTesterError,
    _apply_config_update,
    _apply_probe_results,
    _build_dashboard_config,
    _build_prompt_tester_request,
    _config_review_items,
    _find_custom_provider,
    _prompt_tester_defaults,
    _prompt_tester_impact,
    _validation_messages,
)
from app.providers import OpenAICompatibleProvider, ProviderError
from app.repository import IdempotencyConflict, QueueFull, TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import (
    DashboardInvocationItem,
    DashboardResourcesResponse,
    DashboardTaskDetail,
    HealthResponse,
    TaskStatus,
)

TEMPLATES_ROOT = Path(__file__).parent / "templates"
CSRF_COOKIE_NAME = "ai_broker_dashboard_csrf"
templates = Jinja2Templates(directory=TEMPLATES_ROOT)
register_filters(templates.env)

PROBE_PROGRESS: dict[str, dict[str, Any]] = {}


def create_dashboard_router(
    *,
    queries: DashboardQueryRepository,
    repository: TaskRepository,
    coordinator: ConsensusCoordinator,
    provider,
    scheduler: ResourceScheduler,
    config: BrokerConfig,
    config_path: Path,
    health_loader: Callable[[], Awaitable[HealthResponse]],
) -> APIRouter:
    login_throttle = LoginThrottle()

    def _require_dashboard_access(request: Request) -> None:
        """Guard único de todo el panel salvo el login: las vistas muestran
        prompts y resultados completos, así que exigen la misma credencial que
        las mutaciones. Las páginas HTML redirigen al login (el cliente es un
        navegador); fragmentos HTMX, progreso de probes y acciones POST
        responden 403 porque solo se invocan desde una página ya autenticada."""
        try:
            verify_admin_access(request, config)
        except HTTPException as error:
            is_page = request.method == "GET" and not (
                request.url.path.startswith("/dashboard/fragments")
                or request.url.path.startswith("/dashboard/actions")
            )
            if is_page:
                raise HTTPException(status_code=303, headers={"Location": "/dashboard/login"}) from None
            raise error

    public = APIRouter()
    # Todo el panel salvo el login exige credencial por construcción: una ruta
    # nueva registrada en `protected` queda cubierta sin ningún guard manual
    # (antes cada vista llevaba su verify_admin_access a mano y era fácil
    # olvidarlo al añadir rutas).
    protected = APIRouter(dependencies=[Depends(_require_dashboard_access)])

    async def resources() -> DashboardResourcesResponse:
        return await load_dashboard_resources(provider, scheduler, config)

    async def models() -> tuple[list[dict[str, Any]], str | None]:
        try:
            return await provider.models(), None
        except ProviderError as error:
            return [], f"{error.code}: catalogo no disponible"

    @protected.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request, config_saved: bool = False):
        context: dict[str, Any] = {
            "summary": queries.summary(window_hours=24),
            "queue": queries.list_tasks(page=1, page_size=50, status=TaskStatus.queued, origin=None),
            "active": queries.active_task_detail(),
            "health": await health_loader(),
            "resources": await resources(),
            "history": queries.list_terminal_tasks(page_size=20),
            "config": config,
            "config_saved": config_saved,
            "config_errors": [],
            "config_review": [],
        }
        return _template_response(request, "dashboard.html", context)

    @protected.get("/dashboard/prompt-tester", response_class=HTMLResponse)
    async def prompt_tester(request: Request):
        catalog, catalog_error = await models()
        return _template_response(
            request,
            "prompt_tester.html",
            {
                "models": catalog,
                "catalog_error": catalog_error,
                "form": _prompt_tester_defaults(),
                "errors": [],
                "request_preview": None,
                "impact_preview": None,
                "accepted": None,
            },
        )

    @protected.get("/dashboard/models", response_class=HTMLResponse)
    async def model_dashboard(
        request: Request,
        model_probe: str | None = None,
        model_name: str | None = None,
        model_error: str | None = None,
    ):
        catalog, catalog_error = await models()
        resource_snapshot = await resources()
        return _template_response(
            request,
            "models.html",
            {
                "models": catalog,
                "catalog_error": catalog_error,
                "resources": resource_snapshot,
                "config": config,
                "model_stats": _model_dashboard_stats(catalog, resource_snapshot),
                "probeable_provider_ids": _probeable_provider_ids(config),
                "model_probe": _model_probe_notice(model_probe, model_name, model_error),
            },
        )

    @protected.get("/dashboard/comparison", response_class=HTMLResponse)
    async def comparison(request: Request, task_id: str | None = None):
        tasks = queries.list_comparison_tasks(page_size=25)
        selected = None
        comparison_view = None
        if task_id is not None:
            try:
                selected = queries.task_detail(task_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error
            if selected.task.execution_strategy.value != "mixture_of_agents":
                raise HTTPException(status_code=422, detail="TASK_IS_NOT_MIXTURE")
            comparison_view = _comparison_view(selected)
        elif tasks.items:
            selected = queries.task_detail(tasks.items[0].task_id)
            comparison_view = _comparison_view(selected)
        return _template_response(
            request,
            "comparison.html",
            {
                "tasks": tasks,
                "selected": selected,
                "comparison": comparison_view,
            },
        )

    @protected.get("/dashboard/tasks/{task_id}", response_class=HTMLResponse)
    async def task_view(request: Request, task_id: str):
        try:
            detail = queries.task_detail(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error
        return _template_response(
            request,
            "task_detail.html",
            {
                "detail": detail,
                "task_result": _task_result_view(detail),
            },
        )

    @protected.get("/dashboard/fragments/summary", response_class=HTMLResponse)
    async def summary_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/summary.html",
            context={"summary": queries.summary(window_hours=24)},
        )

    @protected.get("/dashboard/fragments/queue", response_class=HTMLResponse)
    async def queue_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/queue.html",
            context={
                "queue": queries.list_tasks(
                    page=1,
                    page_size=50,
                    status=TaskStatus.queued,
                    origin=None,
                )
            },
        )

    @protected.get("/dashboard/fragments/active", response_class=HTMLResponse)
    async def active_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/active.html",
            context={"active": queries.active_task_detail()},
        )

    @protected.get("/dashboard/fragments/health", response_class=HTMLResponse)
    async def health_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/health.html",
            context={"health": await health_loader()},
        )

    @protected.get("/dashboard/fragments/resources", response_class=HTMLResponse)
    async def resources_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/resources.html",
            context={"resources": await resources()},
        )

    @protected.get("/dashboard/fragments/history", response_class=HTMLResponse)
    async def history_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/history.html",
            context={"history": queries.list_terminal_tasks(page_size=20)},
        )

    @protected.get("/dashboard/fragments/config", response_class=HTMLResponse)
    async def config_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/config.html",
            context={
                "config": config,
                "csrf_token": _csrf_token(request),
                "config_saved": False,
                "config_errors": [],
                "config_review": [],
            },
        )

    @protected.post("/dashboard/actions/config", response_class=HTMLResponse)
    async def update_config(request: Request):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        errors: list[str] = []
        try:
            updated = _build_dashboard_config(config, form)
            if form.get("config_action") == "validate":
                context: dict[str, Any] = {
                    "summary": queries.summary(window_hours=24),
                    "queue": queries.list_tasks(page=1, page_size=50, status=TaskStatus.queued, origin=None),
                    "active": queries.active_task_detail(),
                    "health": await health_loader(),
                    "resources": await resources(),
                    "history": queries.list_terminal_tasks(page_size=20),
                    "config": updated,
                    "config_saved": False,
                    "config_errors": [],
                    "config_review": _config_review_items(config, updated),
                }
                return _template_response(request, "dashboard.html", context)
            save_config(updated, config_path)
            _apply_config_update(config, updated)
            if hasattr(provider, "reload_config"):
                await provider.reload_config(config)
        except PromptTesterError as error:
            errors.append(str(error))
        except ValidationError as error:
            errors.extend(_validation_messages(error))
        if errors:
            context = {
                "summary": queries.summary(window_hours=24),
                "queue": queries.list_tasks(page=1, page_size=50, status=TaskStatus.queued, origin=None),
                "active": queries.active_task_detail(),
                "health": await health_loader(),
                "resources": await resources(),
                "history": queries.list_terminal_tasks(page_size=20),
                "config": config,
                "config_saved": False,
                "config_errors": errors,
                "config_review": [],
            }
            return _template_response(request, "dashboard.html", context)
        return RedirectResponse("/dashboard?config_saved=true#config-panel", status_code=303)

    @protected.post("/dashboard/actions/providers/{provider_id}/probe", response_class=HTMLResponse)
    async def probe_provider_models(request: Request, provider_id: str):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        errors: list[str] = []
        progress_id = form.get("probe_progress_id", "").strip()
        if progress_id:
            PROBE_PROGRESS[progress_id] = {
                "phase": "preparing",
                "provider_id": provider_id,
                "completed": 0,
                "total": None,
                "current_model": None,
                "last_result": None,
                "error": None,
                "updated_at": _utc_now().isoformat(),
            }
        try:
            updated = _build_dashboard_config(config, form)
            provider_config = _find_custom_provider(updated, provider_id)
            if provider_config is None:
                raise PromptTesterError(f"Proveedor custom no encontrado: {provider_id}")
            if not provider_config.enabled:
                raise PromptTesterError(f"Activa el proveedor {provider_id} antes de analizarlo.")
            probe = OpenAICompatibleProvider(provider_config)
            try:
                catalog = await probe.models()

                async def update_probe_progress(payload: dict[str, Any]) -> None:
                    if not progress_id:
                        return
                    PROBE_PROGRESS[progress_id] = {
                        **PROBE_PROGRESS.get(progress_id, {}),
                        **payload,
                        "provider_id": provider_config.id,
                        "updated_at": _utc_now().isoformat(),
                    }

                results = await probe.probe_all_models(progress_callback=update_probe_progress)
            finally:
                await probe.close()
            _apply_probe_results(updated, provider_config.id, results, catalog)
            save_config(updated, config_path)
            _apply_config_update(config, updated)
            if hasattr(provider, "reload_config"):
                await provider.reload_config(config)
            if progress_id:
                PROBE_PROGRESS[progress_id] = {
                    **PROBE_PROGRESS.get(progress_id, {}),
                    "phase": "completed",
                    "completed": len(results),
                    "total": PROBE_PROGRESS.get(progress_id, {}).get("total", len(results)),
                    "current_model": None,
                    "error": None,
                    "updated_at": _utc_now().isoformat(),
                }
        except PromptTesterError as error:
            errors.append(str(error))
        except ProviderError as error:
            errors.append(f"{error.code}: {error}")
        except ValidationError as error:
            errors.extend(_validation_messages(error))
        if errors and progress_id:
            PROBE_PROGRESS[progress_id] = {
                **PROBE_PROGRESS.get(progress_id, {}),
                "phase": "failed",
                "error": "; ".join(errors),
                "updated_at": _utc_now().isoformat(),
            }
        if errors:
            context: dict[str, Any] = {
                "summary": queries.summary(window_hours=24),
                "queue": queries.list_tasks(page=1, page_size=50, status=TaskStatus.queued, origin=None),
                "active": queries.active_task_detail(),
                "health": await health_loader(),
                "resources": await resources(),
                "history": queries.list_terminal_tasks(page_size=20),
                "config": config,
                "config_saved": False,
                "config_errors": errors,
                "config_review": [],
            }
            return _template_response(request, "dashboard.html", context)
        return RedirectResponse("/dashboard?config_saved=true#config-panel", status_code=303)

    @protected.get("/dashboard/actions/providers/{provider_id}/probe/progress")
    async def probe_provider_progress(request: Request, provider_id: str, progress_id: str) -> dict[str, Any]:
        progress = PROBE_PROGRESS.get(progress_id)
        if progress is None or str(progress.get("provider_id") or "").lower() != provider_id.lower():
            return {
                "phase": "unknown",
                "provider_id": provider_id,
                "completed": 0,
                "total": None,
                "current_model": None,
                "last_result": None,
                "error": None,
                "updated_at": _utc_now().isoformat(),
            }
        return progress

    @protected.post("/dashboard/actions/models/probe", response_class=HTMLResponse)
    async def probe_single_model(request: Request):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        provider_id = form.get("provider", "").strip()
        model_name = form.get("model", "").strip()
        query: dict[str, str] = {"model_name": model_name}
        json_response = request.headers.get("accept", "").lower().find("application/json") >= 0
        try:
            if not provider_id or not model_name:
                raise PromptTesterError("Referencia de modelo incompleta.")
            updated = config.model_copy(deep=True)
            provider_config = _find_custom_provider(updated, provider_id)
            if provider_config is None:
                raise PromptTesterError("Este modelo no admite analisis puntual desde el catalogo.")
            if not provider_config.enabled:
                raise PromptTesterError(f"Activa el proveedor {provider_id} antes de analizar modelos.")
            result, catalog = await _probe_single_custom_model(provider_config, model_name)
            _apply_probe_results(updated, provider_config.id, [result], catalog)
            save_config(updated, config_path)
            _apply_config_update(config, updated)
            if hasattr(provider, "reload_config"):
                await provider.reload_config(config)
            query["model_probe"] = str(result.get("compatibility") or "unknown")
            if json_response:
                return JSONResponse(_model_probe_payload(model_name, result))
        except PromptTesterError as error:
            query["model_probe"] = "error"
            query["model_error"] = str(error)
        except ProviderError as error:
            query["model_probe"] = "error"
            query["model_error"] = f"{error.code}: {error}"
        except ValidationError as error:
            query["model_probe"] = "error"
            query["model_error"] = "; ".join(_validation_messages(error))
        if json_response:
            return JSONResponse(
                {
                    "ok": False,
                    "model": model_name,
                    "message": query.get("model_error") or "No se ha podido comprobar el modelo.",
                },
                status_code=422,
            )
        return RedirectResponse(f"/dashboard/models?{urlencode(query)}", status_code=303)

    @protected.post("/dashboard/actions/prompt-tester", response_class=HTMLResponse)
    async def submit_prompt_tester(request: Request):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        action = form.get("action", "validate")
        errors: list[str] = []
        accepted = None
        request_preview = None
        impact_preview = None
        try:
            payload = _build_prompt_tester_request(form)
            request_preview = payload.model_dump(mode="json")
            impact_preview = _prompt_tester_impact(payload)
            if action == "enqueue":
                task, created = repository.create_task(
                    payload,
                    queue_max_size=config.processing.queue_max_size,
                )
                if created:
                    coordinator.initialize_run(task.task_id, payload)
                accepted = {
                    "task_id": task.task_id,
                    "status_url": f"/api/v1/tasks/{task.task_id}",
                    "created": created,
                }
        except PromptTesterError as error:
            errors.append(str(error))
        except ValidationError as error:
            errors.extend(_validation_messages(error))
        except IdempotencyConflict:
            errors.append("La clave idempotente ya existe con otro contenido.")
        except QueueFull:
            errors.append("La cola esta llena; no se ha creado la prueba.")

        catalog, catalog_error = await models()
        return _template_response(
            request,
            "prompt_tester.html",
            {
                "models": catalog,
                "catalog_error": catalog_error,
                "form": {**_prompt_tester_defaults(), **form},
                "errors": errors,
                "request_preview": request_preview,
                "impact_preview": impact_preview,
                "accepted": accepted,
            },
        )

    @public.get("/dashboard/login", response_class=HTMLResponse)
    async def dashboard_login(request: Request):
        try:
            admin_enabled = resolve_admin_token(config) is not None
        except AdminTokenLookupError:
            # Fail-closed: si el backend de credenciales falla, la página se
            # comporta como si hubiera token (pide credencial) en vez de
            # anunciar que la auth está desactivada.
            admin_enabled = True
        return _template_response(
            request,
            "login.html",
            {
                "admin_enabled": admin_enabled,
                "admin_error": None,
            },
        )

    @public.post("/dashboard/actions/login", response_class=HTMLResponse)
    async def dashboard_login_action(request: Request):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        throttle_key = request.client.host if request.client else "unknown"
        if login_throttle.blocked_for(throttle_key) > 0:
            raise HTTPException(status_code=429, detail="ADMIN_LOGIN_RATE_LIMITED")
        try:
            expected = resolve_admin_token(config)
        except AdminTokenLookupError as error:
            # Sin backend no se puede validar ninguna credencial: 503, no login abierto.
            raise HTTPException(status_code=503, detail="ADMIN_AUTH_BACKEND_UNAVAILABLE") from error
        if not expected:
            return RedirectResponse("/dashboard", status_code=303)
        supplied = form.get("admin_token") or ""
        if not secrets.compare_digest(supplied, expected):
            login_throttle.record_failure(throttle_key)
            response = _template_response(
                request,
                "login.html",
                {"admin_enabled": True, "admin_error": "Token de administración incorrecto."},
            )
            response.status_code = 403
            return response
        login_throttle.reset(throttle_key)
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            ADMIN_COOKIE_NAME,
            admin_cookie_value(expected),
            httponly=True,
            samesite="strict",
            secure=False,
            path="/dashboard",
            max_age=ADMIN_SESSION_SECONDS,
        )
        return response

    @protected.post("/dashboard/actions/tasks/{task_id}/cancel", status_code=204)
    async def cancel_task(request: Request, task_id: str) -> Response:
        _verify_dashboard_mutation(request)
        try:
            repository.request_cancel(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error
        return Response(status_code=204, headers={"HX-Trigger": "dashboard-refresh"})

    @protected.post("/dashboard/actions/queue/{task_id}/{direction}", status_code=204)
    async def move_task(request: Request, task_id: str, direction: str) -> Response:
        _verify_dashboard_mutation(request)
        if direction not in {"up", "down"}:
            raise HTTPException(status_code=422, detail="INVALID_DIRECTION")
        ids = [item.task_id for item in repository.list_queue().pending]
        if task_id not in ids:
            raise HTTPException(status_code=409, detail="TASK_NOT_QUEUED")
        index = ids.index(task_id)
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            repository.reorder_queue(ids)
        return Response(status_code=204, headers={"HX-Trigger": "dashboard-refresh"})

    router = APIRouter()
    router.include_router(public)
    router.include_router(protected)
    return router


def _template_response(request: Request, name: str, context: dict[str, Any]):
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={"request": request, "csrf_token": token, **context},
    )
    if request.cookies.get(CSRF_COOKIE_NAME) != token:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/dashboard",
            max_age=60 * 60 * 8,
        )
    return response


def _csrf_token(request: Request) -> str:
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and 24 <= len(existing) <= 160:
        return existing
    return secrets.token_urlsafe(32)


def _verify_dashboard_mutation(request: Request, form: dict[str, str] | None = None) -> None:
    _verify_same_origin(request)
    expected = request.cookies.get(CSRF_COOKIE_NAME)
    supplied = request.headers.get("x-csrf-token") or (form or {}).get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF_VALIDATION_FAILED")


def _verify_same_origin(request: Request) -> None:
    host = request.headers.get("host")
    if not host:
        raise HTTPException(status_code=403, detail="HOST_HEADER_REQUIRED")
    for header_name in ("origin", "referer"):
        header_value = request.headers.get(header_name)
        if not header_value:
            continue
        parsed = urlparse(header_value)
        if parsed.netloc and parsed.netloc.lower() != host.lower():
            raise HTTPException(status_code=403, detail="ORIGIN_VALIDATION_FAILED")


async def _read_urlencoded_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _model_dashboard_stats(
    catalog: list[dict[str, Any]],
    resources: DashboardResourcesResponse,
) -> dict[str, Any]:
    providers = {str(item.get("provider") or "unknown") for item in catalog}
    deployments = {str(item.get("deployment") or "unknown") for item in catalog}
    compatible = sum(1 for item in catalog if str(item.get("compatibility") or "unknown") == "compatible")
    incompatible = sum(1 for item in catalog if str(item.get("compatibility") or "unknown") == "incompatible")
    unknown = len(catalog) - compatible - incompatible
    return {
        "total": len(catalog),
        "providers": len(providers),
        "deployments": len(deployments),
        "compatible": compatible,
        "incompatible": incompatible,
        "unknown": unknown,
        "loaded": len(resources.loaded_models),
    }


def _probeable_provider_ids(config: BrokerConfig) -> list[str]:
    return [
        item.id.lower()
        for item in config.providers.custom
        if item.enabled
    ]


def _model_probe_notice(
    result: str | None,
    model_name: str | None,
    error: str | None,
) -> dict[str, str] | None:
    if result is None:
        return None
    if result == "error":
        return {
            "kind": "danger",
            "title": "No se ha podido comprobar el modelo",
            "message": error or "El proveedor no ha devuelto un resultado valido.",
        }
    label = {
        "compatible": "compatible con mixture",
        "incompatible": "no compatible con mixture",
        "unknown": "pendiente de clasificar",
    }.get(result, result)
    return {
        "kind": "success" if result == "compatible" else "warning",
        "title": "Compatibilidad actualizada",
        "message": f"{model_name or 'Modelo'} queda marcado como {label}.",
    }


def _model_probe_payload(model_name: str, result: dict[str, Any]) -> dict[str, Any]:
    compatibility = str(result.get("compatibility") or "unknown")
    catalog_model = {
        "compatibility": compatibility,
        "compatibility_error": result.get("compatibility_error"),
    }
    return {
        "ok": True,
        "model": model_name,
        "compatibility": compatibility,
        "compatibility_text": templates.env.filters["model_compatibility_text"](catalog_model),
        "compatibility_class": templates.env.filters["model_compatibility_class"](catalog_model),
        "compatibility_error": result.get("compatibility_error"),
        "checked_at": result.get("compatibility_checked_at"),
        "message": (_model_probe_notice(compatibility, model_name, None) or {}).get("message", ""),
    }


async def _probe_single_custom_model(
    provider_config: OpenAICompatibleProviderConfig,
    model_name: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    probe = OpenAICompatibleProvider(provider_config)
    try:
        catalog = await probe.models()
        entry = next((item for item in catalog if str(item.get("name") or "") == model_name), None)
        if entry is None:
            raise PromptTesterError(f"Modelo no encontrado en {provider_config.id}: {model_name}")
        capabilities = {str(capability).lower() for capability in entry.get("capabilities") or []}
        if "completion" in capabilities:
            return await probe.probe_chat_compatibility(model_name), catalog
        if "embedding" in capabilities:
            return await probe.probe_embedding_compatibility(model_name), catalog
        return {
            "name": model_name,
            "compatibility": "unknown",
            "compatibility_checked_at": _utc_now().isoformat(),
            "compatibility_error": "Capacidad no-chat catalogada; endpoint de ejecucion aun no soportado.",
        }, catalog
    finally:
        await probe.close()


def _comparison_view(detail: DashboardTaskDetail) -> dict[str, Any]:
    invocations = detail.invocations
    timed = [item for item in invocations if item.started_at is not None and item.completed_at is not None]
    timeline_available = len(timed) == len(invocations) and bool(invocations)
    base = min((item.started_at for item in timed if item.started_at is not None), default=None)
    end = max((item.completed_at for item in timed if item.completed_at is not None), default=None)
    total_ms = (
        max(1.0, (end - base).total_seconds() * 1000)
        if base is not None and end is not None
        else 1.0
    )
    lanes = [_invocation_lane(item, base, total_ms) for item in invocations]
    proposer_lanes = [item for item in lanes if item["role"] != "arbiter"]
    arbiter_lanes = [item for item in lanes if item["role"] == "arbiter"]
    overlap_detected = _has_overlap([item for item in lanes if item["role"] != "arbiter" and item["timed"]])
    result = detail.result or {}
    consensus = result.get("consensus") if isinstance(result.get("consensus"), dict) else {}
    scheduling = result.get("scheduling") if isinstance(result.get("scheduling"), dict) else {}
    return {
        "timeline_available": timeline_available,
        "overlap_detected": overlap_detected,
        "total_ms": total_ms if timeline_available else None,
        "proposers": proposer_lanes,
        "arbiter": arbiter_lanes,
        "consensus": consensus,
        "scheduling": scheduling,
        "warnings": _comparison_warnings(detail, timeline_available, overlap_detected),
    }


def _invocation_lane(
    invocation: DashboardInvocationItem,
    base,
    total_ms: float,
) -> dict[str, Any]:
    timed = invocation.started_at is not None and invocation.completed_at is not None and base is not None
    if timed:
        assert invocation.started_at is not None
        assert invocation.completed_at is not None
        left = max(0.0, ((invocation.started_at - base).total_seconds() * 1000) / total_ms * 100)
        width = max(2.0, ((invocation.completed_at - invocation.started_at).total_seconds() * 1000) / total_ms * 100)
    else:
        left = 0.0
        width = 100.0
    return {
        "invocation": invocation,
        "role": invocation.role,
        "timed": timed,
        "left": round(left, 3),
        "width": round(min(width, 100.0 - left), 3),
        "start_ms": ((invocation.started_at - base).total_seconds() * 1000) if timed else None,
        "end_ms": ((invocation.completed_at - base).total_seconds() * 1000) if timed else None,
    }


def _has_overlap(lanes: list[dict[str, Any]]) -> bool:
    windows = [
        (float(item["start_ms"]), float(item["end_ms"]))
        for item in lanes
        if item["start_ms"] is not None and item["end_ms"] is not None
    ]
    windows.sort()
    for index in range(1, len(windows)):
        if windows[index][0] < windows[index - 1][1]:
            return True
    return False


def _comparison_warnings(
    detail: DashboardTaskDetail,
    timeline_available: bool,
    overlap_detected: bool,
) -> list[str]:
    warnings: list[str] = []
    if not timeline_available:
        warnings.append("No hay timestamps started_at/completed_at suficientes para demostrar solapamiento real.")
    if detail.task.execution_preset.value == "fast":
        warnings.append("fast se representa como secuencia serial; no se debe interpretar como paralelismo.")
    if detail.task.execution_preset.value == "slow" and timeline_available and not overlap_detected:
        warnings.append("slow fue solicitado, pero las invocaciones registradas no muestran solapamiento.")
    if detail.result is None:
        warnings.append("La tarea aun no tiene resultado terminal; solo se muestra estado persistido.")
    return warnings


def _task_result_view(detail: DashboardTaskDetail) -> dict[str, Any]:
    result = detail.result or {}
    error = detail.error or {}
    assistant_content = result.get("assistant_content") or result.get("result_markdown")
    if assistant_content is None and error:
        assistant_content = error.get("message")
    active = detail.progress.get("active_invocations")
    if not isinstance(active, list):
        active = []
    skipped_proposers = result.get("skipped_proposers")
    if not isinstance(skipped_proposers, list):
        skipped_proposers = detail.progress.get("skipped_proposers")
    if not isinstance(skipped_proposers, list):
        skipped_proposers = []
    warnings = (result.get("consensus") or {}).get("warnings") if isinstance(result.get("consensus"), dict) else []
    if not isinstance(warnings, list):
        warnings = []
    expected_total = detail.progress.get("invocations_total")
    if not isinstance(expected_total, int):
        expected_total = _expected_invocations(detail)
    return {
        "assistant_content": assistant_content,
        "error_code": error.get("code") if isinstance(error, dict) else None,
        "error_message": error.get("message") if isinstance(error, dict) else None,
        "error_stage": error.get("stage") if isinstance(error, dict) else None,
        "error_role": error.get("role") if isinstance(error, dict) else None,
        "error_provider": error.get("provider") if isinstance(error, dict) else None,
        "error_deployment": error.get("deployment") if isinstance(error, dict) else None,
        "error_model": error.get("model") if isinstance(error, dict) else None,
        "active_invocations": active,
        "skipped_proposers": skipped_proposers,
        "warnings": warnings,
        "expected_invocations": expected_total,
    }


def _expected_invocations(detail: DashboardTaskDetail) -> int:
    execution = detail.request.get("execution") if isinstance(detail.request, dict) else {}
    if not isinstance(execution, dict):
        return 1
    if execution.get("strategy") == "mixture_of_agents":
        raw_selection = execution.get("selection")
        selection = raw_selection if isinstance(raw_selection, dict) else {}
        raw_proposers = selection.get("proposers")
        proposers = raw_proposers if isinstance(raw_proposers, list) else []
        judges = int(execution.get("max_judges") or 1)
        return len(proposers) + judges if proposers else int(execution.get("max_proposers") or 0) + judges
    return 1


async def load_dashboard_resources(
    provider,
    scheduler: ResourceScheduler,
    config: BrokerConfig,
) -> DashboardResourcesResponse:
    try:
        snapshot = await provider.resource_snapshot()
        status: Literal["healthy", "unavailable"] = "healthy"
        detail = None
    except ProviderError as error:
        snapshot = {
            "provider": "ollama",
            "used_vram_bytes": 0,
            "reserved_vram_bytes": 0,
            "loaded_models": [],
        }
        status = "unavailable"
        detail = f"{error.code}: snapshot de recursos no disponible"
    return DashboardResourcesResponse(
        checked_at=_utc_now(),
        provider=snapshot["provider"],
        status=status,
        detail=detail,
        vram_budget_bytes=int(config.resources.local_vram_budget_gb * 1024**3),
        vram_safety_margin_bytes=int(config.resources.vram_safety_margin_gb * 1024**3),
        used_vram_bytes=int(snapshot["used_vram_bytes"]),
        reserved_vram_bytes=int(snapshot["reserved_vram_bytes"]),
        max_parallel_invocations=scheduler.max_parallel_invocations(),
        loaded_models=snapshot["loaded_models"],
    )


def _utc_now():
    return datetime.now(timezone.utc)
