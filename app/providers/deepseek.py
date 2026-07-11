from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import DeepSeekConfig
from app.providers.base import (
    CredentialResolver,
    ModelOutput,
    ProviderError,
    _CatalogCache,
    _estimation_text,
    estimate_tokens_upper_bound,
    provider_http_error_message,
    request_with_context_capped_output,
)
from app.schemas import OutputFormat, TaskCreateRequest


class DeepSeekProvider:
    def __init__(self, config: DeepSeekConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_seconds, transport=transport)
        # Ajustes materializados en el cliente HTTP: la recarga los compara con
        # la config nueva porque reasignar .config no cambia un cliente ya creado.
        self._client_settings = (config.base_url, config.timeout_seconds)
        self._catalog_cache = _CatalogCache(config.catalog_cache_seconds)

    async def reload_config(self, config: DeepSeekConfig) -> None:
        """Aplica la config nueva de verdad: si base_url o timeout cambian se
        construye un cliente nuevo y se cierra el anterior."""
        self.config = config
        self._catalog_cache = _CatalogCache(config.catalog_cache_seconds)
        settings = (config.base_url, config.timeout_seconds)
        if settings != self._client_settings:
            old_client = self.client
            self.client = httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_seconds)
            self._client_settings = settings
            await old_client.aclose()

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        key = CredentialResolver.get(self.config)
        if not key:
            raise ProviderError("CREDENTIALS_UNAVAILABLE", "Falta credencial DeepSeek")
        return {"Authorization": f"Bearer {key}"}

    async def models(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []
        cached = self._catalog_cache.get()
        if cached is not None:
            return cached
        try:
            response = await self.client.get("/models", headers=self._headers())
            response.raise_for_status()
            result = [{"name": item["id"], "provider": "deepseek", "deployment": "api", "status": "online",
                       "context_window": self.config.context_window, "capabilities": ["completion"],
                       "context_window_source": "configured",
                       "compatibility": "compatible", "compatibility_checked_at": None, "compatibility_error": None}
                      for item in response.json().get("data") or []]
            self._catalog_cache.set(result)
            return result
        except ProviderError:
            raise
        except httpx.HTTPError as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error

    async def generate(
        self, request: TaskCreateRequest, model: str, prompt: str, system: str | None = None
    ) -> ModelOutput:
        estimation_text = _estimation_text(prompt, system)
        inference_request = request_with_context_capped_output(request, self.config.context_window, estimation_text)
        if request.model_requirements.max_cost_usd is not None:
            estimated_input = estimate_tokens_upper_bound(estimation_text)
            estimated_cost = (
                estimated_input * self.config.input_cost_per_million
                + inference_request.generation.max_output_tokens * self.config.output_cost_per_million
            ) / 1_000_000
            if estimated_cost > request.model_requirements.max_cost_usd:
                raise ProviderError(
                    "BUDGET_EXCEEDED",
                    f"El coste máximo estimado ({estimated_cost:.6f} USD) supera el presupuesto",
                )
        started = datetime.now(timezone.utc)
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        try:
            request_payload: dict[str, Any] = {
                "model": model, "messages": messages,
                "temperature": inference_request.generation.temperature,
                "max_tokens": inference_request.generation.max_output_tokens,
                "stream": False,
            }
            if inference_request.output.format == OutputFormat.json:
                request_payload["response_format"] = {"type": "json_object"}
            response = await self.client.post("/chat/completions", headers=self._headers(), json=request_payload)
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
        choices = payload.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") if choices else None)
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("INVALID_PROVIDER_RESPONSE", "DeepSeek no devolvió contenido")
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cost = (input_tokens * self.config.input_cost_per_million + output_tokens * self.config.output_cost_per_million) / 1_000_000
        return ModelOutput(content, input_tokens, output_tokens, cost,
                           (datetime.now(timezone.utc) - started).total_seconds() * 1000)
