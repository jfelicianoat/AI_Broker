"""Enriquecimiento del catálogo con metadatos externos de models.dev.

Fuente comunitaria gratuita (una descarga JSON, sin clave): contexto real,
precios de referencia por millón, corte de conocimiento y capacidades
declaradas del modelo canónico. Jerarquía de evidencia del broker: sondeo
real contra el endpoint > declarado por el runtime > catálogo externo >
heurística por nombre — el catálogo rellena huecos, nunca pisa un dato
verificado.

La descarga se cachea en disco: sin red se usa la última copia y el broker
jamás depende de internet para arrancar ni para servir el catálogo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import ModelEnrichmentConfig

logger = logging.getLogger("ai_broker.model_enrichment")

# Proveedor del broker -> proveedor de models.dev cuyos precios son comparables.
# Un casado global (otro proveedor del catálogo) da capacidades y contexto del
# modelo canónico, pero su precio no aplica y no se copia.
_PROVIDER_MAP = {
    "lmstudio": "lmstudio",
    "nvidia": "nvidia",
    "deepseek": "deepseek",
    "ollama": "ollama-cloud",
}
# Proveedores del catálogo con ids más canónicos: ganan los empates del índice global.
_GLOBAL_PRIORITY = ("lmstudio", "nvidia", "deepseek", "ollama-cloud", "openrouter")
_RETRY_AFTER_FAILURE_SECONDS = 900.0
_QUANT_SUFFIX = re.compile(r"[@\-_.](q\d[\w.]*|fp?(16|32)|bf16|gguf|mlx|awq|gptq|i1)$", re.IGNORECASE)


def normalize_model_name(name: Any) -> str:
    """Clave de casado entre el nombre local y el id canónico del catálogo.

    "qwen/qwen3-30b-a3b" (LM Studio), "qwen3-30b-a3b:latest" (Ollama) y
    "Qwen3-30B-A3B-Q4_K_M.gguf" deben converger en "qwen3-30b-a3b".
    """
    value = str(name or "").strip().lower()
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    if value.endswith(":latest"):
        value = value[: -len(":latest")]
    value = value.replace(":", "-")
    while True:
        stripped = _QUANT_SUFFIX.sub("", value)
        if stripped == value:
            break
        value = stripped
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def _optional_number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _model_info(provider_id: str, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    modalities = payload.get("modalities") or {}
    inputs = [str(item).lower() for item in modalities.get("input") or []]
    limit = payload.get("limit") or {}
    cost = payload.get("cost") or {}
    try:
        context_window = int(limit["context"]) if limit.get("context") else None
    except (TypeError, ValueError):
        context_window = None
    return {
        "catalog_provider": provider_id,
        "catalog_id": model_id,
        "vision": "image" in inputs,
        "tools": bool(payload.get("tool_call")),
        "json_mode": bool(payload.get("structured_output")),
        "context_window": context_window,
        "cost_input_per_million": _optional_number(cost.get("input")),
        "cost_output_per_million": _optional_number(cost.get("output")),
        "knowledge_cutoff": payload.get("knowledge"),
        "release_date": payload.get("release_date"),
    }


class ModelEnrichment:
    def __init__(
        self,
        settings: ModelEnrichmentConfig,
        cache_path: str | Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.cache_path = Path(cache_path)
        self._transport = transport
        self._lock = asyncio.Lock()
        self._by_provider: dict[tuple[str, str], dict[str, Any]] = {}
        self._global: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._next_refresh = 0.0

    def reload_settings(self, settings: ModelEnrichmentConfig) -> None:
        if settings.url != self.settings.url:
            self._loaded = False
            self._next_refresh = 0.0
        self.settings = settings

    async def ensure_loaded(self) -> None:
        """Carga o refresca el índice; nunca lanza: sin catálogo no hay
        enriquecimiento, pero el broker sigue sirviendo su catálogo normal."""
        if not self.settings.enabled:
            return
        if self._loaded and time.monotonic() < self._next_refresh:
            return
        async with self._lock:
            if self._loaded and time.monotonic() < self._next_refresh:
                return
            raw = await self._fetch()
            if raw is not None:
                self._write_cache(raw)
                self._next_refresh = time.monotonic() + self.settings.refresh_hours * 3600
            else:
                raw = None if self._loaded else self._read_cache()
                self._next_refresh = time.monotonic() + _RETRY_AFTER_FAILURE_SECONDS
            if raw is not None:
                self._build_index(raw)
                self._loaded = True

    async def _fetch(self) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.get(self.settings.url)
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except Exception as error:
            logger.warning(
                "model_enrichment.fetch_failed",
                extra={"event": "model_enrichment.fetch_failed", "detail": str(error)},
            )
            return None

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, raw: dict[str, Any]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning(
                "model_enrichment.cache_write_failed",
                extra={"event": "model_enrichment.cache_write_failed", "path": str(self.cache_path)},
            )

    def _build_index(self, raw: dict[str, Any]) -> None:
        by_provider: dict[tuple[str, str], dict[str, Any]] = {}
        global_index: dict[str, dict[str, Any]] = {}
        ordered = [pid for pid in _GLOBAL_PRIORITY if pid in raw]
        ordered += sorted(pid for pid in raw if pid not in _GLOBAL_PRIORITY)
        for provider_id in ordered:
            provider_payload = raw.get(provider_id)
            models = (provider_payload or {}).get("models") if isinstance(provider_payload, dict) else None
            if not isinstance(models, dict):
                continue
            for model_id, payload in models.items():
                if not isinstance(payload, dict):
                    continue
                key = normalize_model_name(model_id)
                if not key:
                    continue
                info = _model_info(provider_id, str(model_id), payload)
                by_provider.setdefault((provider_id, key), info)
                global_index.setdefault(key, info)
        self._by_provider = by_provider
        self._global = global_index

    def lookup(self, provider: Any, model: Any) -> tuple[dict[str, Any] | None, bool]:
        """(info, coste_comparable): el precio solo vale si casó el proveedor mapeado."""
        key = normalize_model_name(model)
        if not key:
            return None, False
        mapped = _PROVIDER_MAP.get(str(provider or "").lower())
        if mapped is not None:
            info = self._by_provider.get((mapped, key))
            if info is not None:
                return info, True
        return self._global.get(key), False

    def enrich_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Devuelve una copia enriquecida (o la entrada intacta si no hay casado)."""
        if not self.settings.enabled or not self._loaded:
            return entry
        info, cost_comparable = self.lookup(entry.get("provider"), entry.get("name") or entry.get("model"))
        if info is None:
            return entry
        catalog: dict[str, Any] = {
            "source": "models.dev",
            "catalog_provider": info["catalog_provider"],
            "catalog_id": info["catalog_id"],
            "vision": info["vision"],
            "tools": info["tools"],
            "json_mode": info["json_mode"],
            "knowledge_cutoff": info["knowledge_cutoff"],
            "release_date": info["release_date"],
        }
        if cost_comparable:
            catalog["cost_input_per_million"] = info["cost_input_per_million"]
            catalog["cost_output_per_million"] = info["cost_output_per_million"]
        enriched = dict(entry)
        enriched["catalog"] = catalog
        # El contexto del catálogo solo sustituye a un default sin verificar,
        # nunca a un valor configurado o reportado por el runtime.
        if info["context_window"] and (
            not enriched.get("context_window") or enriched.get("context_window_source") == "default"
        ):
            enriched["context_window"] = info["context_window"]
            enriched["context_window_source"] = "catalog"
        return enriched
