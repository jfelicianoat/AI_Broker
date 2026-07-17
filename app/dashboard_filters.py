"""Filtros Jinja2 del panel operativo, separados de las rutas."""

from __future__ import annotations

import json
from typing import Any

STATUS_LABELS = {
    "queued": "En cola",
    "routing": "Enrutando",
    "resource_planning": "Planificando",
    "generating": "Generando",
    "proposing": "Proponiendo",
    "synthesizing": "Sintetizando",
    "completed": "Completada",
    "failed": "Fallida",
    "cancelled": "Cancelada",
}

COMPATIBILITY_LABELS = {
    "compatible": "[OK]",
    "incompatible": "[NO OPERATIVO]",
    "error": "[ERROR TEMP]",
    "unknown": "[PENDIENTE]",
}

COMPATIBILITY_TEXTS = {
    "compatible": "Operativo",
    "incompatible": "No operativo",
    "error": "Error temporal",
    "unknown": "Pendiente de analizar",
}

FEATURE_LABELS = {
    "vision": "visión",
    "json_mode": "JSON",
    "tools": "tools",
}

# Claves del catálogo externo comparables con las del sondeo.
CATALOG_FEATURE_KEYS = ("vision", "json_mode", "tools")


def _compatibility(value: Any) -> str:
    return str((value or {}).get("compatibility") or "unknown")


def gb(value: Any) -> str:
    return f"{float(value or 0) / 1024**3:.1f} GB"


def short_time(value: Any) -> str:
    return value.astimezone().strftime("%H:%M:%S") if value else "—"


def short_date(value: Any) -> str:
    return value.astimezone().strftime("%d/%m %H:%M") if value else "—"


def ms(value: Any) -> str:
    return f"{float(value):.0f} ms" if value is not None else "N/D"


def model_value(value: Any) -> str:
    return json.dumps(
        {"provider": value["provider"], "deployment": value["deployment"], "model": value["name"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def model_compatibility_label(value: Any) -> str:
    return COMPATIBILITY_LABELS.get(_compatibility(value), "[PENDIENTE]")


def model_compatibility_text(value: Any) -> str:
    return COMPATIBILITY_TEXTS.get(_compatibility(value), "Pendiente de analizar")


def model_compatibility_class(value: Any) -> str:
    compatibility = _compatibility(value)
    if compatibility == "compatible":
        return "model-compatible"
    if compatibility == "incompatible":
        return "model-incompatible"
    if compatibility == "error":
        return "model-error"
    return "model-unknown"


def model_features_text(value: Any) -> str:
    """Capacidades de un modelo: primero las verificadas (sondeo/runtime) y,
    para claves sin verificar, lo que declara el catálogo externo con su marca."""
    features = (value or {}).get("features") or {}
    catalog = (value or {}).get("catalog") or {}
    verified = [FEATURE_LABELS.get(key, key) for key, ok in features.items() if ok]
    from_catalog = [
        FEATURE_LABELS.get(key, key)
        for key in CATALOG_FEATURE_KEYS
        if key not in features and catalog.get(key)
    ]
    parts = []
    if verified:
        parts.append(" · ".join(verified))
    if from_catalog:
        parts.append("catálogo: " + " · ".join(from_catalog))
    if parts:
        return " · ".join(parts)
    if features or catalog:
        return "solo texto"
    return "sin sondear"


def model_effective_caps(value: Any) -> list[str]:
    """Capacidades para filtrar: verificadas true + declaradas por el catálogo
    en claves sin verificar. Un negativo verificado excluye aunque el catálogo
    afirme lo contrario."""
    features = (value or {}).get("features") or {}
    catalog = (value or {}).get("catalog") or {}
    caps = [key for key, ok in features.items() if ok]
    caps += [key for key in CATALOG_FEATURE_KEYS if key not in features and catalog.get(key)]
    return caps


def status_label(value: Any) -> str:
    key = getattr(value, "value", value)
    return STATUS_LABELS.get(key, str(key))


def register_filters(env: Any) -> None:
    env.filters["gb"] = gb
    env.filters["short_time"] = short_time
    env.filters["short_date"] = short_date
    env.filters["ms"] = ms
    env.filters["model_value"] = model_value
    env.filters["model_compatibility_label"] = model_compatibility_label
    env.filters["model_compatibility_text"] = model_compatibility_text
    env.filters["model_compatibility_class"] = model_compatibility_class
    env.filters["model_features_text"] = model_features_text
    env.filters["model_effective_caps"] = model_effective_caps
    env.filters["status_label"] = status_label
