from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
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
    HuggingFaceLocalConfig,
    HuggingFaceLocalModelConfig,
    OpenAICompatibleModelConfig,
    OpenAICompatibleProviderConfig,
    save_config,
)
from app.coordinator import ConsensusCoordinator
from app.dashboard import DashboardQueryRepository
from app.dashboard_filters import register_filters
from app.providers import OpenAICompatibleProvider, ProviderError
from app.repository import IdempotencyConflict, QueueFull, TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import (
    DashboardInvocationItem,
    DashboardResourcesResponse,
    DashboardTaskDetail,
    HealthResponse,
    ModelReference,
    TaskCreateRequest,
    TaskStatus,
    is_local_deployment,
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
    router = APIRouter()
    login_throttle = LoginThrottle()

    def _login_redirect_or_none(request: Request) -> RedirectResponse | None:
        """Guard de las páginas HTML del panel: muestran prompts y resultados
        completos, así que con token configurado exigen la misma sesión que
        las mutaciones. Devuelve la redirección al login (el cliente es un
        navegador) o None si el acceso es válido. Los fragmentos HTMX llaman
        a verify_admin_access directamente y responden 403: solo se cargan
        desde una página completa ya autenticada."""
        try:
            verify_admin_access(request, config)
        except HTTPException:
            return RedirectResponse("/dashboard/login", status_code=303)
        return None

    async def resources() -> DashboardResourcesResponse:
        return await load_dashboard_resources(provider, scheduler, config)

    async def models() -> tuple[list[dict[str, Any]], str | None]:
        try:
            return await provider.models(), None
        except ProviderError as error:
            return [], f"{error.code}: catalogo no disponible"

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request, config_saved: bool = False):
        redirect = _login_redirect_or_none(request)
        if redirect:
            return redirect
        context = {
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

    @router.get("/dashboard/prompt-tester", response_class=HTMLResponse)
    async def prompt_tester(request: Request):
        redirect = _login_redirect_or_none(request)
        if redirect:
            return redirect
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

    @router.get("/dashboard/models", response_class=HTMLResponse)
    async def model_dashboard(
        request: Request,
        model_probe: str | None = None,
        model_name: str | None = None,
        model_error: str | None = None,
    ):
        redirect = _login_redirect_or_none(request)
        if redirect:
            return redirect
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

    @router.get("/dashboard/comparison", response_class=HTMLResponse)
    async def comparison(request: Request, task_id: str | None = None):
        redirect = _login_redirect_or_none(request)
        if redirect:
            return redirect
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

    @router.get("/dashboard/tasks/{task_id}", response_class=HTMLResponse)
    async def task_view(request: Request, task_id: str):
        redirect = _login_redirect_or_none(request)
        if redirect:
            return redirect
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

    @router.get("/dashboard/fragments/summary", response_class=HTMLResponse)
    async def summary_fragment(request: Request):
        verify_admin_access(request, config)
        return templates.TemplateResponse(
            request=request,
            name="fragments/summary.html",
            context={"summary": queries.summary(window_hours=24)},
        )

    @router.get("/dashboard/fragments/queue", response_class=HTMLResponse)
    async def queue_fragment(request: Request):
        verify_admin_access(request, config)
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

    @router.get("/dashboard/fragments/active", response_class=HTMLResponse)
    async def active_fragment(request: Request):
        verify_admin_access(request, config)
        return templates.TemplateResponse(
            request=request,
            name="fragments/active.html",
            context={"active": queries.active_task_detail()},
        )

    @router.get("/dashboard/fragments/health", response_class=HTMLResponse)
    async def health_fragment(request: Request):
        verify_admin_access(request, config)
        return templates.TemplateResponse(
            request=request,
            name="fragments/health.html",
            context={"health": await health_loader()},
        )

    @router.get("/dashboard/fragments/resources", response_class=HTMLResponse)
    async def resources_fragment(request: Request):
        verify_admin_access(request, config)
        return templates.TemplateResponse(
            request=request,
            name="fragments/resources.html",
            context={"resources": await resources()},
        )

    @router.get("/dashboard/fragments/history", response_class=HTMLResponse)
    async def history_fragment(request: Request):
        verify_admin_access(request, config)
        return templates.TemplateResponse(
            request=request,
            name="fragments/history.html",
            context={"history": queries.list_terminal_tasks(page_size=20)},
        )

    @router.get("/dashboard/fragments/config", response_class=HTMLResponse)
    async def config_fragment(request: Request):
        verify_admin_access(request, config)
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

    @router.post("/dashboard/actions/config", response_class=HTMLResponse)
    async def update_config(request: Request):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        verify_admin_access(request, config)
        errors: list[str] = []
        try:
            updated = _build_dashboard_config(config, form)
            if form.get("config_action") == "validate":
                context = {
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

    @router.post("/dashboard/actions/providers/{provider_id}/probe", response_class=HTMLResponse)
    async def probe_provider_models(request: Request, provider_id: str):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        verify_admin_access(request, config)
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

    @router.get("/dashboard/actions/providers/{provider_id}/probe/progress")
    async def probe_provider_progress(request: Request, provider_id: str, progress_id: str) -> dict[str, Any]:
        verify_admin_access(request, config)
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

    @router.post("/dashboard/actions/models/probe", response_class=HTMLResponse)
    async def probe_single_model(request: Request):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        verify_admin_access(request, config)
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

    @router.post("/dashboard/actions/prompt-tester", response_class=HTMLResponse)
    async def submit_prompt_tester(request: Request):
        form = await _read_urlencoded_form(request)
        _verify_dashboard_mutation(request, form)
        verify_admin_access(request, config)
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

    @router.get("/dashboard/login", response_class=HTMLResponse)
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

    @router.post("/dashboard/actions/login", response_class=HTMLResponse)
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

    @router.post("/dashboard/actions/tasks/{task_id}/cancel", status_code=204)
    async def cancel_task(request: Request, task_id: str) -> Response:
        _verify_dashboard_mutation(request)
        verify_admin_access(request, config)
        try:
            repository.request_cancel(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error
        return Response(status_code=204, headers={"HX-Trigger": "dashboard-refresh"})

    @router.post("/dashboard/actions/queue/{task_id}/{direction}", status_code=204)
    async def move_task(request: Request, task_id: str, direction: str) -> Response:
        _verify_dashboard_mutation(request)
        verify_admin_access(request, config)
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

    return router


class PromptTesterError(ValueError):
    pass


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


def _prompt_tester_defaults() -> dict[str, str]:
    return {
        "input_mode": "prompt",
        "prompt": "",
        "strategy": "single",
        "preset": "fast",
        "scheduling": "adaptive",
        "temperature": "0.3",
        "max_output_tokens": "4000",
        "output_format": "markdown",
        "json_schema": "",
        "data_classification": "internal",
        "cloud_allowed": "",
        "fallback_allowed": "",
        "timeout_seconds": "600",
        "max_cost_usd": "",
        "priority": "100",
        "single_model": "",
        "arbiter_model": "",
        "proposer_model_1": "",
        "proposer_role_1": "generalist",
        "proposer_model_2": "",
        "proposer_role_2": "specialist",
        "proposer_model_3": "",
        "proposer_role_3": "skeptic",
        "proposer_model_4": "",
        "proposer_role_4": "analyst",
        "proposer_model_5": "",
        "proposer_role_5": "reviewer",
    }


def _config_review_items(current: BrokerConfig, updated: BrokerConfig) -> list[dict[str, str]]:
    current_data = current.model_dump(mode="json")
    updated_data = updated.model_dump(mode="json")
    checks = [
        ("processing.task_timeout_seconds", "Timeout global por tarea"),
        ("processing.queue_max_size", "Tamano maximo de cola"),
        ("processing.max_parallel_invocations", "Max. invocaciones paralelas slow"),
        ("resources.local_vram_budget_gb", "Presupuesto VRAM local"),
        ("resources.vram_safety_margin_gb", "Margen seguridad VRAM"),
        ("resources.max_loaded_local_models", "Max. modelos locales cargados"),
        ("resources.allow_execution_waves", "Permitir waves"),
        ("prompt_compression.enabled", "Compresion de prompts activa"),
        ("prompt_compression.level", "Nivel de compresion de prompts"),
        ("prompt_compression.min_chars", "Minimo de caracteres para comprimir"),
        ("providers.huggingface_local.enabled", "Hugging Face local activo"),
        ("providers.huggingface_local.models_dir", "Directorio HF local"),
        ("providers.huggingface_local.timeout_seconds", "Timeout HF local"),
        ("providers.huggingface_local.default_context_window", "Contexto HF local"),
        ("providers.huggingface_local.default_device", "Device HF local"),
        ("providers.huggingface_local.default_dtype", "Dtype HF local"),
        ("providers.huggingface_local.trust_remote_code", "HF trust_remote_code"),
    ]
    changes: list[dict[str, str]] = []
    for path, label in checks:
        before = _nested_value(current_data, path)
        after = _nested_value(updated_data, path)
        if before != after:
            changes.append({"label": label, "before": _display_config_value(before), "after": _display_config_value(after)})

    current_hf_models = _nested_value(current_data, "providers.huggingface_local.models") or []
    updated_hf_models = _nested_value(updated_data, "providers.huggingface_local.models") or []
    if _model_names(current_hf_models) != _model_names(updated_hf_models):
        changes.append({
            "label": "Modelos HF local",
            "before": _display_model_list(current_hf_models),
            "after": _display_model_list(updated_hf_models),
        })

    current_custom = current_data.get("providers", {}).get("custom", [])
    updated_custom = updated_data.get("providers", {}).get("custom", [])
    for index in range(max(len(current_custom), len(updated_custom))):
        before_provider = current_custom[index] if index < len(current_custom) else {}
        after_provider = updated_custom[index] if index < len(updated_custom) else {}
        prefix = after_provider.get("id") or before_provider.get("id") or f"Proveedor {index + 1}"
        provider_checks = [
            ("enabled", "activo"),
            ("id", "id"),
            ("display_name", "nombre visible"),
            ("base_url", "base URL"),
            ("api_key_env", "variable API key"),
            ("deployment", "deployment"),
            ("auto_start", "autoarranque"),
            ("timeout_seconds", "timeout"),
            ("default_context_window", "contexto"),
            ("probe_max_output_tokens", "tokens probe"),
            ("probe_delay_seconds", "pausa probe"),
            ("probe_max_models", "max. modelos por analisis"),
            ("sync_models", "sincronizar catalogo"),
            ("probe_skip_compatible", "omitir verdes"),
            ("probe_skip_checked", "omitir analizados"),
        ]
        for key, label in provider_checks:
            before = before_provider.get(key)
            after = after_provider.get(key)
            if before != after:
                changes.append({
                    "label": f"{prefix}: {label}",
                    "before": _display_config_value(before),
                    "after": _display_config_value(after),
                })
        if _model_names(before_provider.get("models") or []) != _model_names(after_provider.get("models") or []):
            changes.append({
                "label": f"{prefix}: modelos",
                "before": _display_model_list(before_provider.get("models") or []),
                "after": _display_model_list(after_provider.get("models") or []),
            })
    return changes


def _nested_value(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _display_config_value(value: Any) -> str:
    if value is None or value == "":
        return "N/D"
    if isinstance(value, bool):
        return "si" if value else "no"
    return str(value)


def _model_names(models: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("name") or item.get("path") or "modelo") for item in models]


def _display_model_list(models: list[dict[str, Any]]) -> str:
    names = _model_names(models)
    if not names:
        return "0 modelos"
    preview = ", ".join(names[:3])
    suffix = f" +{len(names) - 3}" if len(names) > 3 else ""
    return f"{len(names)} modelos: {preview}{suffix}"


def _build_dashboard_config(current: BrokerConfig, form: dict[str, str]) -> BrokerConfig:
    payload = current.model_dump(mode="json")
    processing = dict(payload["processing"])
    resources = dict(payload["resources"])
    processing["task_timeout_seconds"] = _int_range_field(
        form, "task_timeout_seconds", minimum=30, maximum=86400
    )
    processing["queue_max_size"] = _int_range_field(
        form, "queue_max_size", minimum=1, maximum=100000
    )
    processing["max_parallel_invocations"] = _auto_or_int_field(
        form, "max_parallel_invocations", minimum=1, maximum=64
    )
    resources["local_vram_budget_gb"] = _float_range_field(
        form, "local_vram_budget_gb", minimum=1.0, maximum=1024.0
    )
    resources["vram_safety_margin_gb"] = _float_range_field(
        form, "vram_safety_margin_gb", minimum=0.0, maximum=512.0
    )
    resources["max_loaded_local_models"] = _auto_or_int_field(
        form, "max_loaded_local_models", minimum=1, maximum=64
    )
    resources["allow_execution_waves"] = _checked(form, "allow_execution_waves")
    if resources["vram_safety_margin_gb"] >= resources["local_vram_budget_gb"]:
        raise PromptTesterError("El margen de VRAM debe ser menor que el presupuesto total de VRAM.")
    level = form.get("prompt_compression_level", "medium").strip().lower() or "medium"
    if level not in {"light", "medium", "aggressive"}:
        raise PromptTesterError("prompt_compression_level debe ser light, medium o aggressive.")
    payload["prompt_compression"] = {
        "enabled": _checked(form, "prompt_compression_enabled"),
        "level": level,
        "min_chars": _int_range_field(form, "prompt_compression_min_chars", minimum=0, maximum=100000),
    }
    payload["processing"] = processing
    payload["resources"] = resources
    payload["providers"]["huggingface_local"] = _parse_huggingface_local_provider(current, form)
    payload["providers"]["custom"] = _parse_custom_providers(current, form)
    return BrokerConfig.model_validate(payload)


def _apply_config_update(target: BrokerConfig, updated: BrokerConfig) -> None:
    target.processing = updated.processing
    target.prompt_compression = updated.prompt_compression
    target.resources = updated.resources
    target.providers = updated.providers



def _parse_custom_providers(current: BrokerConfig, form: dict[str, str]) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for index in range(1, 4):
        provider_id = form.get(f"custom_provider_{index}_id", "").strip()
        base_url = form.get(f"custom_provider_{index}_base_url", "").strip()
        models_text = form.get(f"custom_provider_{index}_models", "").strip()
        enabled = _checked(form, f"custom_provider_{index}_enabled")
        if not provider_id and not base_url and not models_text:
            continue
        if not provider_id:
            raise PromptTesterError(f"Proveedor custom {index}: indica un id.")
        if not base_url:
            raise PromptTesterError(f"Proveedor custom {provider_id}: indica base_url.")
        previous = _find_custom_provider(current, provider_id)
        previous_models = {item.name: item for item in previous.models} if previous is not None else {}
        models = _parse_custom_provider_models(provider_id, models_text, previous_models)
        sync_models = _checked(form, f"custom_provider_{index}_sync_models")
        if enabled and not sync_models and not models:
            raise PromptTesterError(
                f"Proveedor custom {provider_id}: anade al menos un modelo o activa sincronizar catalogo."
            )
        providers.append({
            "id": provider_id,
            "enabled": enabled,
            "adapter": "openai_compatible",
            "display_name": form.get(f"custom_provider_{index}_display_name", "").strip() or None,
            "base_url": base_url.rstrip("/"),
            "timeout_seconds": _float_field(form, f"custom_provider_{index}_timeout_seconds", 300.0),
            "api_key_env": form.get(f"custom_provider_{index}_api_key_env", "").strip() or None,
            "keyring_service": "ai-broker",
            "keyring_username": form.get(f"custom_provider_{index}_keyring_username", "").strip() or None,
            "deployment": form.get(f"custom_provider_{index}_deployment", "cloud") or "cloud",
            "auto_start": _checked(form, f"custom_provider_{index}_auto_start"),
            "sync_models": sync_models,
            "default_context_window": _int_field(form, f"custom_provider_{index}_default_context_window", 128000),
            "probe_max_output_tokens": _int_field(form, f"custom_provider_{index}_probe_max_output_tokens", 1),
            "probe_delay_seconds": _float_field(form, f"custom_provider_{index}_probe_delay_seconds", 0.25),
            "probe_max_models": _int_field(form, f"custom_provider_{index}_probe_max_models", 50),
            "probe_skip_compatible": _checked(form, f"custom_provider_{index}_probe_skip_compatible"),
            "probe_skip_checked": _checked(form, f"custom_provider_{index}_probe_skip_checked"),
            "input_cost_per_million": _float_field(form, f"custom_provider_{index}_input_cost_per_million", 0.0),
            "output_cost_per_million": _float_field(form, f"custom_provider_{index}_output_cost_per_million", 0.0),
            "models": [item.model_dump(mode="json") for item in models],
        })
    return providers


def _parse_huggingface_local_provider(current: BrokerConfig, form: dict[str, str]) -> dict[str, Any]:
    current_hf = getattr(current.providers, "huggingface_local", HuggingFaceLocalConfig())
    models_text = form.get("hf_local_models", "").strip()
    models = _parse_huggingface_local_models(models_text)
    enabled = _checked(form, "hf_local_enabled")
    if enabled and not models:
        raise PromptTesterError("Hugging Face local: anade al menos un modelo.")
    return {
        "enabled": enabled,
        "models_dir": form.get("hf_local_models_dir", "").strip() or current_hf.models_dir,
        "timeout_seconds": _float_field(form, "hf_local_timeout_seconds", 300.0),
        "default_context_window": _int_field(form, "hf_local_default_context_window", 32768),
        "default_device": form.get("hf_local_default_device", "").strip() or "auto",
        "default_dtype": form.get("hf_local_default_dtype", "").strip() or None,
        "trust_remote_code": _checked(form, "hf_local_trust_remote_code"),
        "models": [item.model_dump(mode="json") for item in models],
    }


def _parse_huggingface_local_models(models_text: str) -> list[HuggingFaceLocalModelConfig]:
    if not models_text:
        return []
    models: list[HuggingFaceLocalModelConfig] = []
    for line_number, raw_line in enumerate(models_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        try:
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValueError("missing name or path")
            models.append(HuggingFaceLocalModelConfig(
                name=parts[0],
                path=parts[1],
                context_window=int(parts[2]) if len(parts) > 2 and parts[2] else 32768,
                device=parts[3] if len(parts) > 3 and parts[3] else None,
                dtype=parts[4] if len(parts) > 4 and parts[4] else None,
                capabilities=["completion"],
                compatibility="compatible",
            ))
        except (ValueError, ValidationError) as error:
            raise PromptTesterError(
                f"Hugging Face local: modelo invalido en linea {line_number}. "
                "Usa nombre|ruta|contexto|device|dtype."
            ) from error
    return models


def _parse_custom_provider_models(
    provider_id: str,
    models_text: str,
    previous_models: dict[str, OpenAICompatibleModelConfig] | None = None,
) -> list[OpenAICompatibleModelConfig]:
    if not models_text:
        return []
    previous_models = previous_models or {}
    models: list[OpenAICompatibleModelConfig] = []
    for line_number, raw_line in enumerate(models_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        try:
            previous = previous_models.get(parts[0])
            models.append(OpenAICompatibleModelConfig(
                name=parts[0],
                context_window=int(parts[1]) if len(parts) > 1 and parts[1] else 128000,
                input_cost_per_million=float(parts[2]) if len(parts) > 2 and parts[2] else 0.0,
                output_cost_per_million=float(parts[3]) if len(parts) > 3 and parts[3] else 0.0,
                compatibility=previous.compatibility if previous is not None else "unknown",
                compatibility_checked_at=previous.compatibility_checked_at if previous is not None else None,
                compatibility_error=previous.compatibility_error if previous is not None else None,
            ))
        except (ValueError, ValidationError) as error:
            raise PromptTesterError(
                f"Proveedor custom {provider_id}: modelo invalido en linea {line_number}. "
                "Usa nombre|contexto|coste_input_millon|coste_output_millon."
            ) from error
    return models


def _find_custom_provider(
    config: BrokerConfig,
    provider_id: str,
) -> OpenAICompatibleProviderConfig | None:
    return next(
        (item for item in config.providers.custom if item.id.lower() == provider_id.lower()),
        None,
    )


def _apply_probe_results(
    config: BrokerConfig,
    provider_id: str,
    results: list[dict[str, Any]],
    catalog: list[dict[str, Any]] | None = None,
) -> None:
    provider_config = _find_custom_provider(config, provider_id)
    if provider_config is None:
        raise PromptTesterError(f"Proveedor custom no encontrado: {provider_id}")
    existing = {item.name: item for item in provider_config.models}
    updated_by_name: dict[str, OpenAICompatibleModelConfig] = dict(existing)
    for entry in catalog or []:
        name = str(entry.get("name") or entry.get("id") or "")
        if not name:
            continue
        previous = updated_by_name.get(name)
        inferred_capabilities = list(entry.get("capabilities") or [])
        if previous is not None:
            updated_by_name[name] = OpenAICompatibleModelConfig(
                name=name,
                context_window=previous.context_window,
                input_cost_per_million=previous.input_cost_per_million,
                output_cost_per_million=previous.output_cost_per_million,
                capabilities=inferred_capabilities or list(previous.capabilities),
                compatibility=previous.compatibility,
                compatibility_checked_at=previous.compatibility_checked_at,
                compatibility_error=previous.compatibility_error,
            )
            continue
        updated_by_name[name] = OpenAICompatibleModelConfig(
            name=name,
            context_window=int(entry.get("context_window") or provider_config.default_context_window),
            input_cost_per_million=provider_config.input_cost_per_million,
            output_cost_per_million=provider_config.output_cost_per_million,
            capabilities=inferred_capabilities or ["completion"],
            compatibility=str(entry.get("compatibility") or "unknown"),
            compatibility_checked_at=entry.get("compatibility_checked_at"),
            compatibility_error=entry.get("compatibility_error"),
        )
    for result in results:
        name = str(result["name"])
        previous = updated_by_name.get(name)
        updated_by_name[name] = OpenAICompatibleModelConfig(
            name=name,
            context_window=previous.context_window if previous is not None else provider_config.default_context_window,
            input_cost_per_million=(
                previous.input_cost_per_million if previous is not None else provider_config.input_cost_per_million
            ),
            output_cost_per_million=(
                previous.output_cost_per_million if previous is not None else provider_config.output_cost_per_million
            ),
            capabilities=list(previous.capabilities) if previous is not None else ["completion"],
            compatibility=str(result.get("compatibility") or "unknown"),
            compatibility_checked_at=result.get("compatibility_checked_at"),
            compatibility_error=result.get("compatibility_error"),
        )
    provider_config.models = list(updated_by_name.values())


def _build_prompt_tester_request(form: dict[str, str]) -> TaskCreateRequest:
    prompt = form.get("prompt", "")
    if not prompt.strip():
        raise PromptTesterError("El prompt no puede estar vacio.")
    input_mode = form.get("input_mode", "prompt")
    if input_mode == "json":
        try:
            json.loads(prompt)
        except json.JSONDecodeError as error:
            raise PromptTesterError(
                f"JSON de entrada invalido: linea {error.lineno}, columna {error.colno}."
            ) from error
    elif input_mode != "prompt":
        raise PromptTesterError("Modo de entrada no soportado.")

    output_format = form.get("output_format", "markdown")
    output: dict[str, Any] = {"format": output_format, "language": "es"}
    json_schema_text = form.get("json_schema", "").strip()
    if output_format == "json":
        if not json_schema_text:
            raise PromptTesterError("El formato de salida JSON requiere JSON Schema.")
        try:
            output["json_schema"] = json.loads(json_schema_text)
        except json.JSONDecodeError as error:
            raise PromptTesterError(
                f"JSON Schema invalido: linea {error.lineno}, columna {error.colno}."
            ) from error

    strategy = form.get("strategy", "single")
    cloud_allowed = _checked(form, "cloud_allowed")
    fallback_allowed = _checked(form, "fallback_allowed")
    if strategy == "single":
        target = _parse_model_reference(form.get("single_model", ""))
        _ensure_cloud_allowed([target], cloud_allowed)
        execution = {
            "strategy": "single",
            "preset": "fast",
            "scheduling": "sequential",
            "timeout_seconds": _int_field(form, "timeout_seconds", 600),
        }
        model_requirements = {
            "preferred_model": target.model,
            "target_model": target.model_dump(mode="json"),
            "fallback_allowed": fallback_allowed,
            "cloud_allowed": cloud_allowed,
            "allowed_providers": [target.provider],
            "max_cost_usd": _optional_float(form, "max_cost_usd"),
        }
    elif strategy == "mixture_of_agents":
        preset = form.get("preset", "fast")
        if preset not in {"fast", "slow"}:
            raise PromptTesterError("El probador solo admite mixture_of_agents/fast o slow.")
        proposers = _parse_proposers(form)
        arbiter = _parse_model_reference(form.get("arbiter_model", ""))
        selected_models = proposers + [arbiter]
        _ensure_cloud_allowed(selected_models, cloud_allowed)
        execution = {
            "strategy": "mixture_of_agents",
            "preset": preset,
            "scheduling": "sequential" if preset == "fast" else form.get("scheduling", "adaptive"),
            "max_proposers": len(proposers),
            "max_judges": 1,
            "max_rounds": 1,
            "timeout_seconds": _int_field(form, "timeout_seconds", 600),
            "selection": {
                "mode": "manual",
                "allow_substitution": False,
                "proposer_count": len(proposers),
                "proposers": [item.model_dump(mode="json") for item in proposers],
                "arbiter": arbiter.model_dump(mode="json"),
            },
        }
        model_requirements = {
            "fallback_allowed": fallback_allowed,
            "cloud_allowed": cloud_allowed,
            "allowed_providers": sorted({item.provider for item in selected_models}),
            "max_cost_usd": _optional_float(form, "max_cost_usd"),
        }
    else:
        raise PromptTesterError("Estrategia no soportada.")

    return TaskCreateRequest.model_validate({
        "idempotency_key": f"prompt-tester:{uuid4().hex}",
        "request_id": f"prompt-tester-{uuid4().hex[:12]}",
        "content": {
            "prompt": prompt,
            "metadata": {
                "origin": "prompt_tester",
                "input_mode": input_mode,
            },
        },
        "output": output,
        "generation": {
            "temperature": _float_field(form, "temperature", 0.3),
            "max_output_tokens": _int_field(form, "max_output_tokens", 4000),
        },
        "model_requirements": model_requirements,
        "execution": execution,
        "risk": {
            "data_classification": form.get("data_classification", "internal"),
            "human_review_required": False,
        },
        "priority": _int_field(form, "priority", 100),
    })


def _prompt_tester_impact(payload: TaskCreateRequest) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    execution = data.get("execution") or {}
    model_requirements = data.get("model_requirements") or {}
    generation = data.get("generation") or {}
    strategy = execution.get("strategy", "single")
    preset = execution.get("preset", "fast")
    if strategy == "mixture_of_agents":
        selection = execution.get("selection") if isinstance(execution.get("selection"), dict) else {}
        proposers = selection.get("proposers") if isinstance(selection.get("proposers"), list) else []
        expected_invocations = len(proposers) + int(execution.get("max_judges") or 1)
        selected_models = proposers + ([selection.get("arbiter")] if selection.get("arbiter") else [])
    else:
        expected_invocations = 1
        selected_models = [model_requirements.get("target_model")]
    selected_models = [item for item in selected_models if isinstance(item, dict)]
    cloud_models = [
        f"{item.get('provider')}/{item.get('deployment')}/{item.get('model')}"
        for item in selected_models
        if str(item.get("deployment") or "").lower() == "cloud"
    ]
    return {
        "strategy": f"{strategy}/{preset}",
        "expected_invocations": expected_invocations,
        "scheduling": execution.get("scheduling", "sequential"),
        "timeout_seconds": execution.get("timeout_seconds"),
        "max_output_tokens": generation.get("max_output_tokens"),
        "cloud_allowed": bool(model_requirements.get("cloud_allowed")),
        "fallback_allowed": bool(model_requirements.get("fallback_allowed")),
        "cloud_models": cloud_models,
    }


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
        "message": _model_probe_notice(compatibility, model_name, None)["message"],
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


def _parse_proposers(form: dict[str, str]) -> list[ModelReference]:
    proposers: list[ModelReference] = []
    for index in range(1, 6):
        raw = form.get(f"proposer_model_{index}", "")
        if not raw:
            continue
        role = form.get(f"proposer_role_{index}", "").strip() or f"proposer_{index}"
        proposers.append(_parse_model_reference(raw).model_copy(update={"role": role}))
    if not proposers:
        raise PromptTesterError("Selecciona al menos un proponente.")
    return proposers


def _parse_model_reference(raw: str) -> ModelReference:
    if not raw:
        raise PromptTesterError("Selecciona un modelo del catalogo.")
    try:
        payload = json.loads(raw)
        return ModelReference(
            provider=payload["provider"],
            deployment=payload["deployment"],
            model=payload["model"],
        )
    except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
        raise PromptTesterError("Referencia de modelo invalida.") from error


def _ensure_cloud_allowed(models: list[ModelReference], cloud_allowed: bool) -> None:
    if cloud_allowed:
        return
    blocked = [
        f"{item.provider}/{item.deployment}/{item.model}"
        for item in models
        if not is_local_deployment(item.deployment)
    ]
    if blocked:
        raise PromptTesterError(
            "Marca Permitir cloud o selecciona solo modelos locales: " + ", ".join(blocked)
        )


def _checked(form: dict[str, str], key: str) -> bool:
    return form.get(key) in {"1", "true", "on", "yes"}


def _int_field(form: dict[str, str], key: str, default: int) -> int:
    raw = form.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise PromptTesterError(f"{key} debe ser un numero entero.") from error


def _int_range_field(form: dict[str, str], key: str, *, minimum: int, maximum: int) -> int:
    value = _int_field(form, key, minimum)
    if value < minimum or value > maximum:
        raise PromptTesterError(f"{key} debe estar entre {minimum} y {maximum}.")
    return value


def _float_field(form: dict[str, str], key: str, default: float) -> float:
    raw = form.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise PromptTesterError(f"{key} debe ser numerico.") from error


def _float_range_field(form: dict[str, str], key: str, *, minimum: float, maximum: float) -> float:
    value = _float_field(form, key, minimum)
    if value < minimum or value > maximum:
        raise PromptTesterError(f"{key} debe estar entre {minimum:g} y {maximum:g}.")
    return value


def _auto_or_int_field(form: dict[str, str], key: str, *, minimum: int, maximum: int) -> int | str:
    raw = form.get(key, "").strip().lower()
    if not raw or raw == "auto":
        return "auto"
    try:
        value = int(raw)
    except ValueError as error:
        raise PromptTesterError(f"{key} debe ser 'auto' o un numero entero.") from error
    if value < minimum or value > maximum:
        raise PromptTesterError(f"{key} debe ser 'auto' o estar entre {minimum} y {maximum}.")
    return value


def _optional_float(form: dict[str, str], key: str) -> float | None:
    raw = form.get(key, "").strip()
    if not raw:
        return None
    return _float_field(form, key, 0.0)


def _validation_messages(error: ValidationError) -> list[str]:
    messages = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        messages.append(f"{location}: {item.get('msg')}")
    return messages


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
        selection = execution.get("selection") if isinstance(execution.get("selection"), dict) else {}
        proposers = selection.get("proposers") if isinstance(selection.get("proposers"), list) else []
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
        status = "healthy"
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
