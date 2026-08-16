from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from app import execution_fingerprint as fingerprint
from app.config import DeepSeekConfig
from app.providers.base import (
    AgentTurn,
    CredentialResolver,
    ModelOutput,
    ProviderError,
    ToolCall,
    _CatalogCache,
    _estimation_text,
    effective_generation,
    estimate_tokens_upper_bound,
    provider_error_from_http,
    request_with_context_capped_output,
    rescued_content,
)
from app.providers.base import (
    openai_determinism_fields as _determinism_fields,
)
from app.providers.base import (
    openai_observed_fingerprint as _observed_fingerprint,
)
from app.providers.diagnostics import reasoning_only_error
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
                       "compatibility": "compatible", "compatibility_checked_at": None, "compatibility_error": None,
                       "features": self._known_features(str(item["id"])),
                       # Proveedor remoto: solo la capa A es comprobable desde
                       # aquí. La C (system_fingerprint, modelo devuelto) llega
                       # con cada respuesta y se suma a la huella de la invocación.
                       "execution_fingerprint": fingerprint.build({
                           "provider": fingerprint.component("deepseek", fingerprint.DECLARED),
                           "deployment": fingerprint.component("api", fingerprint.DECLARED),
                           "model": fingerprint.component(str(item["id"]), fingerprint.DECLARED),
                       })}
                      for item in response.json().get("data") or []]
            self._catalog_cache.set(result)
            return result
        except ProviderError:
            raise
        except httpx.HTTPError as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error

    @staticmethod
    def _known_features(model_id: str) -> dict[str, bool]:
        """Capacidades documentadas por la API de DeepSeek: sin visión en ambos
        modelos; JSON estructurado en los dos; function calling solo en chat."""
        if "reasoner" in model_id.lower():
            return {"vision": False, "json_mode": True, "tools": False}
        return {"vision": False, "json_mode": True, "tools": True}

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
                **_determinism_fields(inference_request),
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
            raise provider_error_from_http(error, provider="deepseek", model=model) from error
        choices = payload.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        # deepseek-reasoner entrega su cadena de razonamiento aparte; si la
        # respuesta se queda ahí, se rescata en vez de dar la tarea por perdida.
        content, source = rescued_content(message, enabled=True)
        if content is None:
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                diagnosis = reasoning_only_error(len(reasoning), None)
                raise ProviderError(diagnosis.code, diagnosis.message("deepseek", model))
            finish = (choices[0].get("finish_reason") if choices else None)
            raise ProviderError(
                "INVALID_PROVIDER_RESPONSE",
                f"deepseek/{model}: la respuesta llegó sin contenido "
                f"(finish_reason={finish or 'desconocido'}).",
            )
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cost = (input_tokens * self.config.input_cost_per_million + output_tokens * self.config.output_cost_per_million) / 1_000_000
        return ModelOutput(content, input_tokens, output_tokens, cost,
                           (datetime.now(timezone.utc) - started).total_seconds() * 1000,
                           generation=effective_generation(inference_request),
                           fingerprint_observed=_observed_fingerprint(payload),
                           content_source=source)

    async def chat_tools(
        self,
        request: TaskCreateRequest,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AgentTurn:
        """Una ronda de /chat/completions con tools (formato OpenAI)."""
        started = datetime.now(timezone.utc)
        body: dict[str, Any] = {
            "model": model, "messages": messages,
            "temperature": request.generation.temperature,
            "max_tokens": request.generation.max_output_tokens,
            "stream": False,
            **_determinism_fields(request),
        }
        # Sin tools se omiten ambas claves: `"tools": []` es un 400 en la API
        # de DeepSeek, y el turno de cierre del bucle agéntico llega así.
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            response = await self.client.post("/chat/completions", headers=self._headers(), json=body)
            response.raise_for_status()
            payload = response.json()
        except ProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error
        except httpx.HTTPStatusError as error:
            raise provider_error_from_http(error, provider="deepseek", model=model) from error
        message = ((payload.get("choices") or [{}])[0].get("message")) or {}
        tool_calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append(ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            ))
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        turn_content, turn_source = (
            (message.get("content"), None) if tool_calls
            else rescued_content(message, enabled=True)
        )
        if tool_calls and not isinstance(turn_content, str):
            turn_content = None
        return AgentTurn(
            content=turn_content if turn_content and str(turn_content).strip() else None,
            tool_calls=tuple(tool_calls),
            tokens_input=input_tokens,
            tokens_output=output_tokens,
            cost_usd=(input_tokens * self.config.input_cost_per_million
                      + output_tokens * self.config.output_cost_per_million) / 1_000_000,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            raw_assistant_message=message,
            generation=effective_generation(request),
            fingerprint_observed=_observed_fingerprint(payload),
            content_source=turn_source,
        )
