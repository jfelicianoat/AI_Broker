from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BrokerConfig
from app.dashboard import DashboardQueryRepository
from app.providers import ProviderError
from app.repository import TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import DashboardResourcesResponse, HealthResponse, TaskStatus


TEMPLATES_ROOT = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_ROOT)
templates.env.filters["gb"] = lambda value: f"{float(value or 0) / 1024**3:.1f} GB"
templates.env.filters["short_time"] = lambda value: value.astimezone().strftime("%H:%M:%S") if value else "—"
templates.env.filters["short_date"] = lambda value: value.astimezone().strftime("%d/%m %H:%M") if value else "—"
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
    provider,
    scheduler: ResourceScheduler,
    config: BrokerConfig,
    health_loader: Callable[[], Awaitable[HealthResponse]],
) -> APIRouter:
    router = APIRouter()

    async def resources() -> DashboardResourcesResponse:
        return await load_dashboard_resources(provider, scheduler, config)

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
