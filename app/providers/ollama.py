from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import BrokerConfig
from app.providers.base import (
    ModelOutput,
    ProviderError,
    _CatalogCache,
    _estimation_text,
    enforce_context_limit,
    provider_http_error_message,
    request_with_context_capped_output,
)
from app.schemas import OutputFormat, TaskCreateRequest


class OllamaLifecycleManager:
    def __init__(self, client: httpx.AsyncClient, config: BrokerConfig) -> None:
        self.client = client
        self.config = config
        self._lock = asyncio.Lock()
        self._leases: dict[str, int] = {}
        self._reserved_sizes: dict[str, int] = {}

    async def running(self) -> list[dict[str, Any]]:
        try:
            response = await self.client.get("/api/ps")
            response.raise_for_status()
            payload = response.json()
            return list(payload.get("models") or [])
        except httpx.HTTPError as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error

    @asynccontextmanager
    async def lease(self, model: str, estimated_size: int = 0):
        async with self._lock:
            await self._ensure_capacity(model, estimated_size)
            self._leases[model] = self._leases.get(model, 0) + 1
            self._reserved_sizes[model] = max(self._reserved_sizes.get(model, 0), estimated_size)
        try:
            yield
        finally:
            async with self._lock:
                remaining = self._leases.get(model, 1) - 1
                if remaining > 0:
                    self._leases[model] = remaining
                else:
                    self._leases.pop(model, None)
                    try:
                        if self.config.processing.unload_after_task:
                            await self.unload(model)
                    finally:
                        self._reserved_sizes.pop(model, None)

    async def _ensure_capacity(self, model: str, estimated_size: int) -> None:
        running = await self.running()
        budget = int(
            (self.config.resources.local_vram_budget_gb - self.config.resources.vram_safety_margin_gb)
            * 1024**3
        )
        running_names = {str(item.get("name") or item.get("model") or "") for item in running}
        occupied = sum(int(item.get("size_vram") or 0) for item in running)
        occupied += sum(size for name, size in self._reserved_sizes.items() if name not in running_names)
        if any(item.get("name") == model for item in running):
            return
        if occupied + estimated_size <= budget:
            return
        for item in running:
            name = str(item.get("name") or item.get("model") or "")
            if name and name not in self._leases:
                await self.unload(name)
        refreshed = await self.running()
        refreshed_names = {str(item.get("name") or item.get("model") or "") for item in refreshed}
        occupied = sum(int(item.get("size_vram") or 0) for item in refreshed)
        occupied += sum(size for name, size in self._reserved_sizes.items() if name not in refreshed_names)
        if occupied + estimated_size > budget:
            raise ProviderError("VRAM_INSUFFICIENT", f"No hay VRAM segura para cargar {model}")

    async def unload(self, model: str) -> None:
        try:
            response = await self.client.post("/api/generate", json={"model": model, "keep_alive": 0})
            response.raise_for_status()
            deadline = asyncio.get_running_loop().time() + self.config.providers.ollama.unload_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if not any(item.get("name") == model for item in await self.running()):
                    return
                await asyncio.sleep(0.1)
            raise ProviderError("MODEL_UNLOAD_FAILED", f"Ollama no descargó {model}")
        except httpx.HTTPError as error:
            raise ProviderError("MODEL_UNLOAD_FAILED", str(error), retryable=True) from error

    async def resource_snapshot(self) -> dict[str, Any]:
        running = await self.running()
        async with self._lock:
            leases = dict(self._leases)
            reservations = dict(self._reserved_sizes)
        loaded = [
            {
                "model": str(item.get("name") or item.get("model") or "unknown"),
                "size_vram_bytes": int(item.get("size_vram") or 0),
                "context_length": int(item["context_length"]) if item.get("context_length") is not None else None,
                "lease_count": leases.get(str(item.get("name") or item.get("model") or ""), 0),
            }
            for item in running
        ]
        return {
            "provider": "ollama",
            "used_vram_bytes": sum(item["size_vram_bytes"] for item in loaded),
            "reserved_vram_bytes": sum(reservations.values()),
            "loaded_models": loaded,
        }


class OllamaProvider:
    def __init__(self, config: BrokerConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        ollama = config.providers.ollama
        self.client = httpx.AsyncClient(base_url=ollama.base_url, timeout=ollama.timeout_seconds, transport=transport)
        # Ajustes materializados en el cliente: la recarga compara contra la
        # config nueva (la config compartida puede mutarse in place, así que
        # self.config no sirve como referencia del estado anterior).
        self._client_settings = (ollama.base_url, ollama.timeout_seconds)
        self.lifecycle = OllamaLifecycleManager(self.client, config)
        self._catalog_cache = _CatalogCache(ollama.catalog_cache_seconds)

    async def reload_config(self, config: BrokerConfig) -> None:
        """Aplica la config nueva de verdad: cliente y lifecycle se reconstruyen
        si base_url o timeout cambian; el catálogo cacheado se descarta siempre."""
        self.config = config
        ollama = config.providers.ollama
        self._catalog_cache = _CatalogCache(ollama.catalog_cache_seconds)
        settings = (ollama.base_url, ollama.timeout_seconds)
        if settings != self._client_settings:
            old_client = self.client
            self.client = httpx.AsyncClient(base_url=ollama.base_url, timeout=ollama.timeout_seconds)
            # El lifecycle va ligado al cliente; solo se recrea con él para no
            # perder los leases de modelos cargados en recargas sin cambios.
            self.lifecycle = OllamaLifecycleManager(self.client, config)
            self._client_settings = settings
            await old_client.aclose()
        else:
            self.lifecycle.config = config

    async def close(self) -> None:
        await self.client.aclose()

    async def models(self) -> list[dict[str, Any]]:
        cached = self._catalog_cache.get()
        if cached is not None:
            return cached
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            result = []
            for item in response.json().get("models") or []:
                details = item.get("details") or {}
                context_window = item.get("context_length") or details.get("context_length")
                capabilities = item.get("capabilities") or []
                if context_window is None or not capabilities:
                    metadata = await self._model_metadata(str(item.get("name") or item.get("model") or ""))
                    context_window = context_window or metadata["context_window"]
                    capabilities = capabilities or metadata["capabilities"]
                result.append({
                    "name": item.get("name") or item.get("model"), "provider": "ollama",
                    "deployment": "cloud" if item.get("remote_host") else "local",
                    "status": "available", "size_bytes": item.get("size", 0),
                    "context_window": context_window, "capabilities": capabilities,
                    "context_window_source": "reported",
                    "family": details.get("family"),
                    "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
                    "compatibility": "compatible",
                    "compatibility_checked_at": None,
                    "compatibility_error": None,
                    "features": self._declared_features(capabilities),
                })
            self._catalog_cache.set(result)
            return result
        except httpx.HTTPError as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error

    @staticmethod
    def _declared_features(capabilities: list[Any]) -> dict[str, bool]:
        """Features en el formato del sondeo, derivadas de las capacidades que
        declara el propio runtime de Ollama en /api/show. La lista es
        exhaustiva para el runtime: la ausencia de "vision"/"tools" significa
        no soportado, no "sin comprobar". json_mode no se declara y queda fuera.
        """
        declared = {str(item).lower() for item in capabilities}
        if "completion" not in declared:
            return {}
        return {"vision": "vision" in declared, "tools": "tools" in declared}

    async def _model_metadata(self, model: str) -> dict[str, Any]:
        try:
            response = await self.client.post("/api/show", json={"model": model})
            response.raise_for_status()
            payload = response.json()
            model_info = payload.get("model_info") or {}
            context_window = next(
                (int(value) for key, value in model_info.items() if key.endswith(".context_length")),
                None,
            )
            return {"context_window": context_window, "capabilities": payload.get("capabilities") or []}
        except (httpx.HTTPError, TypeError, ValueError):
            return {"context_window": None, "capabilities": []}

    async def generate(
        self, request: TaskCreateRequest, model: str, prompt: str, system: str | None = None
    ) -> ModelOutput:
        catalog = await self.models()
        entry = next((item for item in catalog if item["name"] == model), None)
        if entry is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo Ollama no disponible: {model}")
        inference_request = request_with_context_capped_output(
            request, entry.get("context_window"), _estimation_text(prompt, system)
        )
        started = datetime.now(timezone.utc)
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        try:
            async with self.lifecycle.lease(model, int(entry.get("size_bytes") or 0)):
                payload_request: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": -1,
                    "options": {
                        "temperature": inference_request.generation.temperature,
                        "num_predict": inference_request.generation.max_output_tokens,
                    },
                }
                if inference_request.output.format == OutputFormat.json:
                    payload_request["format"] = inference_request.output.json_schema
                response = await self.client.post("/api/chat", json=payload_request)
                response.raise_for_status()
                payload = response.json()
        except ProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error
        except httpx.HTTPStatusError as error:
            raise ProviderError(
                "MODEL_ERROR",
                provider_http_error_message(error),
                retryable=error.response.status_code >= 500,
            ) from error
        content = (payload.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("INVALID_PROVIDER_RESPONSE", "Ollama no devolvió message.content")
        return ModelOutput(
            content=content, tokens_input=int(payload.get("prompt_eval_count") or 0),
            tokens_output=int(payload.get("eval_count") or 0), cost_usd=0.0,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
        )

    async def embed(self, request: TaskCreateRequest, model: str, input_text: str) -> ModelOutput:
        catalog = await self.models()
        entry = next((item for item in catalog if item["name"] == model), None)
        if entry is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo Ollama no disponible: {model}")
        if "embedding" not in set(entry.get("capabilities") or []):
            raise ProviderError("MODEL_CAPABILITY_MISMATCH", f"El modelo {model} no declara capacidad embedding")
        enforce_context_limit(request, entry.get("context_window"), input_text)
        started = datetime.now(timezone.utc)
        try:
            async with self.lifecycle.lease(model, int(entry.get("size_bytes") or 0)):
                response = await self.client.post(
                    "/api/embed",
                    json={"model": model, "input": input_text, "truncate": False, "keep_alive": -1},
                )
                response.raise_for_status()
                payload = response.json()
        except ProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error
        except httpx.HTTPStatusError as error:
            raise ProviderError(
                "MODEL_ERROR",
                provider_http_error_message(error),
                retryable=error.response.status_code >= 500,
            ) from error
        embeddings = payload.get("embeddings") or []
        vector = embeddings[0] if len(embeddings) == 1 else None
        if not isinstance(vector, list) or not vector or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in vector
        ):
            raise ProviderError("INVALID_PROVIDER_RESPONSE", "Ollama no devolvió un embedding numérico único")
        return ModelOutput(
            content=None,
            tokens_input=int(payload.get("prompt_eval_count") or 0),
            tokens_output=0,
            cost_usd=0.0,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            embedding=tuple(float(value) for value in vector),
        )
