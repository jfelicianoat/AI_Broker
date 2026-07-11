"""Estado agregado de dependencias: sqlite, disco, dispatcher y proveedores.

Los checks se cachean por intervalo (config.health); las sondas de
proveedores llevan su propia caché y deadline en RoutedModelProvider.health.
"""
from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.config import BrokerConfig
from app.db import Database
from app.schemas import HealthDependency, HealthResponse

# Caché mutable por app: dependencia -> (expira_en_monotonic, resultado).
HealthCache = dict[str, tuple[float, HealthDependency]]


def _cached_dependency(
    cache: HealthCache,
    key: str,
    now: float,
    ttl: float,
    probe: Callable[[], HealthDependency],
) -> HealthDependency:
    """Reutiliza el último resultado dentro del TTL: los orquestadores sondean
    /health/ready en bucle y no tiene sentido repetir cada comprobación."""
    entry = cache.get(key)
    if entry is not None and entry[0] > now:
        return entry[1]
    dependency = probe()
    cache[key] = (now + ttl, dependency)
    return dependency


def _check_sqlite(db: Database) -> HealthDependency:
    checked_at = datetime.now(timezone.utc)
    try:
        start = datetime.now(timezone.utc)
        db.query_one("SELECT 1")
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        return HealthDependency(
            status="healthy",
            checked_at=checked_at,
            detail="SQLite reachable",
            latency_ms=latency_ms,
        )
    except Exception as exc:  # pragma: no cover - defensive readiness path
        return HealthDependency(status="unavailable", checked_at=checked_at, detail=str(exc))


def _check_disk(database: Path, alert_gb: int) -> HealthDependency:
    """El broker escribe BD, WAL, logs y artefactos en este volumen: quedarse
    sin espacio corrompe la cola, así que por debajo del umbral se degrada."""
    checked_at = datetime.now(timezone.utc)
    try:
        free_gb = shutil.disk_usage(database.resolve().parent).free / 1024**3
    except OSError as exc:
        return HealthDependency(
            status="unavailable",
            checked_at=checked_at,
            detail=f"No se pudo medir el espacio libre: {exc}",
        )
    if free_gb < alert_gb:
        return HealthDependency(
            status="degraded",
            checked_at=checked_at,
            detail=f"{free_gb:.1f} GB libres, por debajo de la alerta de {alert_gb} GB",
        )
    return HealthDependency(
        status="healthy",
        checked_at=checked_at,
        detail=f"{free_gb:.1f} GB libres (alerta a partir de {alert_gb} GB)",
    )


async def health_response(
    db: Database,
    provider,
    dispatcher_state: str | None,
    config: BrokerConfig,
    cache: HealthCache,
) -> HealthResponse:
    """Estado agregado de dependencias con caché por intervalo (config.health).

    sqlite y disco se revalidan según su TTL; las sondas de proveedores llevan
    su propia caché y deadline (RoutedModelProvider.health). El dispatcher se
    evalúa siempre en fresco: es una comprobación en memoria y decide la
    readiness.
    """
    checked_at = datetime.now(timezone.utc)
    health_config = config.health
    now = time.monotonic()

    sqlite = _cached_dependency(
        cache, "sqlite", now, health_config.sqlite_interval_seconds, lambda: _check_sqlite(db)
    )
    dependencies = {"sqlite": sqlite}
    dependencies["disk"] = _cached_dependency(
        cache,
        "disk",
        now,
        health_config.local_dependencies_interval_seconds,
        lambda: _check_disk(Path(config.persistence.database), health_config.disk_free_alert_gb),
    )
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
    status: Literal["healthy", "degraded", "unavailable"]
    status = "unavailable" if sqlite.status == "unavailable" else "healthy"
    states = {dependency.status for dependency in dependencies.values()}
    if "unavailable" in states and sqlite.status != "unavailable":
        status = "degraded"
    elif "degraded" in states and status == "healthy":
        status = "degraded"
    return HealthResponse(status=status, checked_at=checked_at, dependencies=dependencies)
