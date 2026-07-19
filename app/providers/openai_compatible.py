from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import OpenAICompatibleProviderConfig
from app.providers.base import (
    PROBE_HARD_MAX_MODELS,
    AgentTurn,
    CredentialResolver,
    ModelOutput,
    ProviderError,
    ToolCall,
    _CatalogCache,
    _estimation_text,
    classify_probe_http_error,
    enforce_context_limit,
    estimate_tokens_upper_bound,
    infer_openai_compatible_capabilities,
    provider_http_error_message,
    request_with_context_capped_output,
)
from app.schemas import OutputFormat, TaskCreateRequest

# Capacidades sondeables contra /chat/completions con 1 token de salida.
PROBE_FEATURES: tuple[str, ...] = ("vision", "json_mode", "tools")

# PNG de 1x1 pixel: suficiente para saber si el modelo acepta imagenes.
_PROBE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: OpenAICompatibleProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
        )
        # Solo cachea los nombres devueltos por /models: los campos derivados de la
        # configuración (compatibilidad, costes) se reconstruyen en cada llamada.
        self._names_cache = _CatalogCache(config.catalog_cache_seconds)

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key_env:
            return {}
        key = CredentialResolver.get(self.config)
        if not key:
            label = self.config.display_name or self.config.id
            raise ProviderError("CREDENTIALS_UNAVAILABLE", f"Falta credencial para {label}: {self.config.api_key_env}")
        return {"Authorization": f"Bearer {key}"}

    async def models(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []
        configured = {item.name: item for item in self.config.models}
        names = list(configured)
        if self.config.sync_models:
            cached_names = self._names_cache.get()
            if cached_names is not None:
                names = list(cached_names)
            else:
                try:
                    response = await self.client.get("/models", headers=self._headers())
                    response.raise_for_status()
                    names = [
                        str(item["id"])
                        for item in response.json().get("data") or []
                        if isinstance(item, dict) and item.get("id")
                    ]
                except ProviderError:
                    raise
                except httpx.HTTPError as error:
                    raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error
                self._names_cache.set(list(names))
        return [self._catalog_entry(name, configured.get(name)) for name in names]

    def _catalog_entry(self, name: str, model_config: Any | None = None) -> dict[str, Any]:
        capabilities = (
            list(model_config.capabilities)
            if model_config is not None
            else infer_openai_compatible_capabilities(name)
        )
        return {
            "name": name,
            "provider": self.config.id,
            "deployment": self.config.deployment,
            "status": "online",
            "context_window": (
                model_config.context_window if model_config is not None else self.config.default_context_window
            ),
            # "default" = heredado de default_context_window sin verificar: puede no
            # corresponder al contexto real del modelo y producir errores 4xx tardíos.
            "context_window_source": "configured" if model_config is not None else "default",
            "capabilities": capabilities,
            "family": self.config.display_name or self.config.id,
            "compatibility": (
                model_config.compatibility if model_config is not None else "unknown"
            ),
            "compatibility_checked_at": (
                model_config.compatibility_checked_at if model_config is not None else None
            ),
            "compatibility_error": (
                model_config.compatibility_error if model_config is not None else None
            ),
            "features": dict(model_config.features) if model_config is not None else {},
            "features_checked_at": (
                model_config.features_checked_at if model_config is not None else None
            ),
        }

    def _model_config(self, model: str) -> Any | None:
        return next((item for item in self.config.models if item.name == model), None)

    def _costs(self, model: str) -> tuple[float, float]:
        model_config = self._model_config(model)
        if model_config is not None:
            return model_config.input_cost_per_million, model_config.output_cost_per_million
        return self.config.input_cost_per_million, self.config.output_cost_per_million

    async def probe_chat_compatibility(self, model: str) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        try:
            response = await self.client.post(
                "/chat/completions",
                headers=self._headers(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": self.config.probe_max_output_tokens,
                    "temperature": 0.1,
                    "stream": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") or []
            compatible = bool(choices)
            return {
                "name": model,
                "compatibility": "compatible" if compatible else "incompatible",
                "compatibility_checked_at": started.isoformat(),
                "compatibility_error": None if compatible else "Respuesta sin choices",
            }
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429:
                raise ProviderError(
                    "RATE_LIMITED",
                    provider_http_error_message(error),
                    retryable=True,
                ) from error
            compatibility, detail = classify_probe_http_error(error)
            return {
                "name": model,
                "compatibility": compatibility,
                "compatibility_checked_at": started.isoformat(),
                "compatibility_error": detail,
            }
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            return {
                "name": model,
                "compatibility": "error",
                "compatibility_checked_at": started.isoformat(),
                "compatibility_error": str(error),
            }

    def _feature_probe_payload(self, model: str, feature: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self.config.probe_max_output_tokens,
            "temperature": 0.1,
            "stream": False,
        }
        if feature == "vision":
            payload["messages"] = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "ping"},
                    {"type": "image_url", "image_url": {"url": _PROBE_PIXEL_PNG}},
                ],
            }]
        elif feature == "json_mode":
            payload["messages"] = [{"role": "user", "content": "Devuelve un objeto JSON vacio."}]
            payload["response_format"] = {"type": "json_object"}
        elif feature == "tools":
            payload["messages"] = [{"role": "user", "content": "ping"}]
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Sonda de function calling",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
        else:
            raise ValueError(f"Capacidad no sondeable: {feature}")
        return payload

    async def probe_feature(self, model: str, feature: str) -> bool | None:
        """True/False = verificado contra el endpoint; None = no concluyente
        (fallo temporal del proveedor: no se persiste como negativo)."""
        try:
            response = await self.client.post(
                "/chat/completions",
                headers=self._headers(),
                json=self._feature_probe_payload(model, feature),
            )
            response.raise_for_status()
            return bool(response.json().get("choices"))
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 429:
                raise ProviderError(
                    "RATE_LIMITED",
                    provider_http_error_message(error),
                    retryable=True,
                ) from error
            if status in (401, 403, 408) or status >= 500:
                return None
            return False
        except (httpx.TimeoutException, httpx.NetworkError):
            return None

    async def probe_model_features(self, model: str) -> dict[str, bool]:
        features: dict[str, bool] = {}
        for feature in PROBE_FEATURES:
            if self.config.probe_delay_seconds:
                await asyncio.sleep(self.config.probe_delay_seconds)
            try:
                outcome = await self.probe_feature(model, feature)
            except ProviderError as error:
                if error.code == "RATE_LIMITED":
                    # Lo ya verificado no se pierde: el llamante decide si
                    # persiste el parcial y corta la tanda.
                    error.details["partial_features"] = dict(features)
                raise
            if outcome is not None:
                features[feature] = outcome
        return features

    async def probe_embedding_compatibility(self, model: str) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        try:
            response = await self.client.post(
                "/embeddings",
                headers=self._headers(),
                json={"model": model, "input": "ping"},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or []
            vector = data[0].get("embedding") if data and isinstance(data[0], dict) else None
            compatible = isinstance(vector, list) and bool(vector)
            return {
                "name": model,
                "compatibility": "compatible" if compatible else "unknown",
                "compatibility_checked_at": started.isoformat(),
                "compatibility_error": None if compatible else "Respuesta de embeddings sin vector",
            }
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429:
                raise ProviderError(
                    "RATE_LIMITED",
                    provider_http_error_message(error),
                    retryable=True,
                ) from error
            compatibility, detail = classify_probe_http_error(error)
            return {
                "name": model,
                "compatibility": compatibility,
                "compatibility_checked_at": started.isoformat(),
                "compatibility_error": detail,
            }
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            return {
                "name": model,
                "compatibility": "error",
                "compatibility_checked_at": started.isoformat(),
                "compatibility_error": str(error),
            }

    async def probe_all_models(
        self,
        *,
        max_models: int | None = None,
        skip_compatible: bool | None = None,
        skip_checked: bool | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        catalog = await self.models()
        if skip_compatible is None:
            skip_compatible = self.config.probe_skip_compatible
        if skip_checked is None:
            skip_checked = self.config.probe_skip_checked
        requested_limit = max_models or self.config.probe_max_models
        limit = min(requested_limit, PROBE_HARD_MAX_MODELS)
        candidates = []
        for item in catalog:
            compatibility = str(item.get("compatibility") or "unknown")
            # Los errores temporales se reintentan siempre: "ya analizado" no
            # aplica a un sondeo que falló por causas ajenas al modelo.
            if skip_checked and item.get("compatibility_checked_at") and compatibility != "error":
                continue
            if skip_compatible and compatibility == "compatible":
                continue
            candidates.append(item)
            if len(candidates) >= limit:
                break
        results: list[dict[str, Any]] = []
        if progress_callback is not None:
            await progress_callback({
                "phase": "running",
                "completed": 0,
                "total": len(candidates),
                "current_model": None,
                "last_result": None,
            })
        for item in candidates:
            name = str(item["name"])
            capabilities = {str(capability).lower() for capability in item.get("capabilities") or []}
            if progress_callback is not None:
                await progress_callback({
                    "phase": "running",
                    "completed": len(results),
                    "total": len(candidates),
                    "current_model": name,
                    "last_result": None,
                })
            rate_limited = False
            if "completion" in capabilities:
                try:
                    result = await self.probe_chat_compatibility(name)
                except ProviderError as error:
                    if error.code == "RATE_LIMITED":
                        break
                    raise
                if result["compatibility"] == "compatible" and self.config.probe_features:
                    if progress_callback is not None:
                        await progress_callback({
                            "phase": "running",
                            "completed": len(results),
                            "total": len(candidates),
                            "current_model": f"{name} (capacidades)",
                            "last_result": None,
                        })
                    try:
                        result["features"] = await self.probe_model_features(name)
                        result["features_checked_at"] = datetime.now(timezone.utc).isoformat()
                    except ProviderError as error:
                        if error.code != "RATE_LIMITED":
                            raise
                        partial = error.details.get("partial_features") or {}
                        if partial:
                            result["features"] = partial
                            result["features_checked_at"] = datetime.now(timezone.utc).isoformat()
                        rate_limited = True
                results.append(result)
            elif "embedding" in capabilities:
                try:
                    result = await self.probe_embedding_compatibility(name)
                    results.append(result)
                except ProviderError as error:
                    if error.code == "RATE_LIMITED":
                        break
                    raise
            else:
                result = {
                    "name": name,
                    "compatibility": "unknown",
                    "compatibility_checked_at": datetime.now(timezone.utc).isoformat(),
                    "compatibility_error": "Capacidad no-chat catalogada; endpoint de ejecución aún no soportado.",
                }
                results.append(result)
            if progress_callback is not None:
                await progress_callback({
                    "phase": "running",
                    "completed": len(results),
                    "total": len(candidates),
                    "current_model": name,
                    "last_result": result,
                })
            if rate_limited:
                break
            if self.config.probe_delay_seconds:
                await asyncio.sleep(self.config.probe_delay_seconds)
        if progress_callback is not None:
            await progress_callback({
                "phase": "completed",
                "completed": len(results),
                "total": len(candidates),
                "current_model": None,
                "last_result": results[-1] if results else None,
            })
        return results

    async def embed(self, request: TaskCreateRequest, model: str, input_text: str) -> ModelOutput:
        catalog = await self.models()
        entry = next((item for item in catalog if item["name"] == model), None)
        if entry is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo {self.config.id} no disponible: {model}")
        if "embedding" not in {str(capability).lower() for capability in entry.get("capabilities") or []}:
            raise ProviderError("MODEL_CAPABILITY_MISMATCH", f"El modelo {model} no declara capacidad embedding")
        enforce_context_limit(request, entry.get("context_window"), input_text)
        input_cost, _ = self._costs(model)
        if request.model_requirements.max_cost_usd is not None:
            estimated_input = estimate_tokens_upper_bound(input_text)
            estimated_cost = estimated_input * input_cost / 1_000_000
            if estimated_cost > request.model_requirements.max_cost_usd:
                raise ProviderError(
                    "BUDGET_EXCEEDED",
                    f"El coste maximo estimado ({estimated_cost:.6f} USD) supera el presupuesto",
                )
        started = datetime.now(timezone.utc)
        try:
            response = await self.client.post(
                "/embeddings",
                headers=self._headers(),
                json={"model": model, "input": input_text},
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
        data = payload.get("data") or []
        vector = data[0].get("embedding") if data and isinstance(data[0], dict) else None
        if not isinstance(vector, list) or not vector or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in vector
        ):
            raise ProviderError("INVALID_PROVIDER_RESPONSE", f"{self.config.id} no devolvio un embedding numerico")
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        cost = input_tokens * input_cost / 1_000_000
        return ModelOutput(
            content=None,
            tokens_input=input_tokens,
            tokens_output=0,
            cost_usd=cost,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            embedding=tuple(float(value) for value in vector),
        )

    async def chat_tools(
        self,
        request: TaskCreateRequest,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AgentTurn:
        """Una ronda de /chat/completions con tools. Devuelve la respuesta del
        modelo (contenido final o tool_calls) sin ejecutar nada: el coordinador
        ejecuta las skills y decide si continúa el loop."""
        input_cost, output_cost = self._costs(model)
        started = datetime.now(timezone.utc)
        payload_body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.generation.temperature,
            "max_tokens": request.generation.max_output_tokens,
            "stream": False,
            "tools": tools,
            "tool_choice": "auto",
        }
        try:
            response = await self.client.post("/chat/completions", headers=self._headers(), json=payload_body)
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
        message = (choices[0].get("message") if choices else None) or {}
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
        content = message.get("content")
        return AgentTurn(
            content=content if isinstance(content, str) else None,
            tool_calls=tuple(tool_calls),
            tokens_input=input_tokens,
            tokens_output=output_tokens,
            cost_usd=(input_tokens * input_cost + output_tokens * output_cost) / 1_000_000,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            raw_assistant_message=message,
        )

    async def generate(
        self, request: TaskCreateRequest, model: str, prompt: str, system: str | None = None
    ) -> ModelOutput:
        catalog = await self.models()
        entry = next((item for item in catalog if item["name"] == model), None)
        if entry is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo {self.config.id} no disponible: {model}")
        if "completion" not in set(entry.get("capabilities") or []):
            raise ProviderError("MODEL_CAPABILITY_MISMATCH", f"El modelo {model} no declara capacidad completion")
        estimation_text = _estimation_text(prompt, system)
        inference_request = request_with_context_capped_output(request, entry.get("context_window"), estimation_text)
        input_cost, output_cost = self._costs(model)
        if request.model_requirements.max_cost_usd is not None:
            estimated_input = estimate_tokens_upper_bound(estimation_text)
            estimated_cost = (
                estimated_input * input_cost
                + inference_request.generation.max_output_tokens * output_cost
            ) / 1_000_000
            if estimated_cost > request.model_requirements.max_cost_usd:
                raise ProviderError(
                    "BUDGET_EXCEEDED",
                    f"El coste maximo estimado ({estimated_cost:.6f} USD) supera el presupuesto",
                )
        started = datetime.now(timezone.utc)
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        try:
            request_payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
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
            raise ProviderError("INVALID_PROVIDER_RESPONSE", f"{self.config.id} no devolvio contenido")
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cost = (input_tokens * input_cost + output_tokens * output_cost) / 1_000_000
        return ModelOutput(
            content,
            input_tokens,
            output_tokens,
            cost,
            (datetime.now(timezone.utc) - started).total_seconds() * 1000,
        )
