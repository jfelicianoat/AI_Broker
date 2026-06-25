from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import BrokerConfig
from app.coordinator import ConsensusCoordinator
from app.dashboard import DashboardQueryRepository
from app.providers import ProviderError
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
)


TEMPLATES_ROOT = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_ROOT)
templates.env.filters["gb"] = lambda value: f"{float(value or 0) / 1024**3:.1f} GB"
templates.env.filters["short_time"] = lambda value: value.astimezone().strftime("%H:%M:%S") if value else "—"
templates.env.filters["short_date"] = lambda value: value.astimezone().strftime("%d/%m %H:%M") if value else "—"
templates.env.filters["ms"] = lambda value: f"{float(value):.0f} ms" if value is not None else "N/D"
templates.env.filters["model_value"] = lambda value: json.dumps({
    "provider": value["provider"],
    "deployment": value["deployment"],
    "model": value["name"],
}, ensure_ascii=False, separators=(",", ":"))
templates.env.filters["status_label"] = lambda value: {
    "queued": "En cola",
    "routing": "Enrutando",
    "resource_planning": "Planificando",
    "generating": "Generando",
    "proposing": "Proponiendo",
    "synthesizing": "Sintetizando",
    "completed": "Completada",
    "failed": "Fallida",
    "cancelled": "Cancelada",
}.get(getattr(value, "value", value), str(getattr(value, "value", value)))


def create_dashboard_router(
    *,
    queries: DashboardQueryRepository,
    repository: TaskRepository,
    coordinator: ConsensusCoordinator,
    provider,
    scheduler: ResourceScheduler,
    config: BrokerConfig,
    health_loader: Callable[[], Awaitable[HealthResponse]],
) -> APIRouter:
    router = APIRouter()

    async def resources() -> DashboardResourcesResponse:
        return await load_dashboard_resources(provider, scheduler, config)

    async def models() -> tuple[list[dict[str, Any]], str | None]:
        try:
            return await provider.models(), None
        except ProviderError as error:
            return [], f"{error.code}: catalogo no disponible"

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        context = {
            "request": request,
            "summary": queries.summary(window_hours=24),
            "queue": queries.list_tasks(page=1, page_size=50, status=TaskStatus.queued, origin=None),
            "active": queries.active_task_detail(),
            "health": await health_loader(),
            "resources": await resources(),
        }
        return templates.TemplateResponse(request=request, name="dashboard.html", context=context)

    @router.get("/dashboard/prompt-tester", response_class=HTMLResponse)
    async def prompt_tester(request: Request):
        catalog, catalog_error = await models()
        return templates.TemplateResponse(
            request=request,
            name="prompt_tester.html",
            context={
                "models": catalog,
                "catalog_error": catalog_error,
                "form": _prompt_tester_defaults(),
                "errors": [],
                "request_preview": None,
                "accepted": None,
            },
        )

    @router.get("/dashboard/comparison", response_class=HTMLResponse)
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
        return templates.TemplateResponse(
            request=request,
            name="comparison.html",
            context={
                "tasks": tasks,
                "selected": selected,
                "comparison": comparison_view,
            },
        )

    @router.get("/dashboard/fragments/summary", response_class=HTMLResponse)
    async def summary_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/summary.html",
            context={"summary": queries.summary(window_hours=24)},
        )

    @router.get("/dashboard/fragments/queue", response_class=HTMLResponse)
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

    @router.get("/dashboard/fragments/active", response_class=HTMLResponse)
    async def active_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/active.html",
            context={"active": queries.active_task_detail()},
        )

    @router.get("/dashboard/fragments/health", response_class=HTMLResponse)
    async def health_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/health.html",
            context={"health": await health_loader()},
        )

    @router.get("/dashboard/fragments/resources", response_class=HTMLResponse)
    async def resources_fragment(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="fragments/resources.html",
            context={"resources": await resources()},
        )

    @router.post("/dashboard/actions/prompt-tester", response_class=HTMLResponse)
    async def submit_prompt_tester(request: Request):
        form = await _read_urlencoded_form(request)
        action = form.get("action", "validate")
        errors: list[str] = []
        accepted = None
        request_preview = None
        try:
            payload = _build_prompt_tester_request(form)
            request_preview = payload.model_dump(mode="json")
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
        return templates.TemplateResponse(
            request=request,
            name="prompt_tester.html",
            context={
                "models": catalog,
                "catalog_error": catalog_error,
                "form": {**_prompt_tester_defaults(), **form},
                "errors": errors,
                "request_preview": request_preview,
                "accepted": accepted,
            },
        )

    @router.post("/dashboard/actions/tasks/{task_id}/cancel", status_code=204)
    async def cancel_task(task_id: str) -> Response:
        try:
            repository.request_cancel(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND") from error
        return Response(status_code=204, headers={"HX-Trigger": "dashboard-refresh"})

    @router.post("/dashboard/actions/queue/{task_id}/{direction}", status_code=204)
    async def move_task(task_id: str, direction: str) -> Response:
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


def _float_field(form: dict[str, str], key: str, default: float) -> float:
    raw = form.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise PromptTesterError(f"{key} debe ser numerico.") from error


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
