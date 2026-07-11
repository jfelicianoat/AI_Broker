from __future__ import annotations

import asyncio
from typing import Any

from app.config import BrokerConfig, effective_max_parallel_invocations
from app.prompt_compressor import PromptCompressor
from app.providers.base import (
    ROLE_SYSTEM_PROMPTS,
    ModelOutput,
    ProviderError,
    context_fits_with_capped_output,
    estimate_required_context,
    neutralize_consensus_delimiters,
    role_system_prompt,
)
from app.providers.bootstrap import BootstrapModelProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.huggingface import HuggingFaceLocalProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas import (
    ExecutionPreset,
    ExecutionStrategy,
    InferenceKind,
    ModelReference,
    TaskCreateRequest,
    is_local_deployment,
)


class RoutedModelProvider:
    def __init__(self, config: BrokerConfig, *, ollama: OllamaProvider | None = None,
                 deepseek: DeepSeekProvider | None = None,
                 huggingface_local: HuggingFaceLocalProvider | None = None,
                 custom: dict[str, OpenAICompatibleProvider] | None = None) -> None:
        self.config = config
        self.ollama = ollama or OllamaProvider(config)
        self.deepseek = deepseek or DeepSeekProvider(config.providers.deepseek)
        self.huggingface_local = huggingface_local or HuggingFaceLocalProvider(config.providers.huggingface_local)
        self.custom = custom if custom is not None else self._build_custom_providers(config)
        self._parallel_limit = effective_max_parallel_invocations(config)
        self._serial_inference_slot = asyncio.Semaphore(1)
        self._parallel_inference_slot = asyncio.Semaphore(self._parallel_limit)
        self.prompt_compressor = self._build_prompt_compressor(config)
        # Caché de sondas de salud: provider_id -> (expira_en_monotonic, resultado).
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _build_prompt_compressor(config: BrokerConfig) -> PromptCompressor:
        settings = config.prompt_compression
        return PromptCompressor(
            enabled=settings.enabled,
            level=settings.level,
            min_chars=settings.min_chars,
        )

    def _user_prompt(self, request: TaskCreateRequest) -> str:
        """Prompt que viaja al proveedor; el original persiste intacto en la tarea.

        Los embeddings nunca se comprimen: alterar el texto altera el vector.
        """
        if request.inference_kind == InferenceKind.embedding:
            return request.content.prompt
        return self.prompt_compressor.compress_text(request.content.prompt)

    @staticmethod
    def _build_custom_providers(config: BrokerConfig) -> dict[str, OpenAICompatibleProvider]:
        return {
            item.id.lower(): OpenAICompatibleProvider(item)
            for item in config.providers.custom
            if item.enabled
        }

    async def reload_config(self, config: BrokerConfig) -> None:
        """Recarga transaccional: se construye lo nuevo, se intercambia y se
        cierra lo antiguo.

        Antes los custom reemplazados quedaban sin cerrar (fuga de clientes
        HTTP), el semáforo conservaba el límite calculado en el arranque y los
        cambios de base_url/timeout nunca llegaban a los clientes ya creados.
        """
        # Construir primero: si la construcción falla, lo antiguo sigue intacto.
        new_custom = self._build_custom_providers(config)
        old_custom = self.custom

        self.config = config
        await self.ollama.reload_config(config)
        await self.deepseek.reload_config(config.providers.deepseek)
        self.huggingface_local.reload_config(config.providers.huggingface_local)
        self.custom = new_custom
        self.prompt_compressor = self._build_prompt_compressor(config)
        self._rebuild_inference_slots(config)
        # El conjunto de proveedores puede haber cambiado: las sondas cacheadas
        # dejan de ser representativas.
        self._health_cache.clear()

        # Con lo nuevo ya en su sitio, cerrar los clientes reemplazados.
        for provider in old_custom.values():
            await provider.close()

    def _rebuild_inference_slots(self, config: BrokerConfig) -> None:
        limit = effective_max_parallel_invocations(config)
        if limit == self._parallel_limit:
            return
        # Las inferencias en vuelo liberan el semáforo antiguo al terminar;
        # las adquisiciones nuevas ya entran con el límite recién configurado.
        self._parallel_limit = limit
        self._parallel_inference_slot = asyncio.Semaphore(limit)

    async def close(self) -> None:
        await self.ollama.close()
        await self.deepseek.close()
        await self.huggingface_local.close()
        for provider in self.custom.values():
            await provider.close()

    async def models(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        sources: list[Any] = []
        if self.config.providers.ollama.enabled:
            sources.append(self.ollama)
        if self.config.providers.deepseek.enabled:
            sources.append(self.deepseek)
        if self.config.providers.huggingface_local.enabled:
            sources.append(self.huggingface_local)
        sources.extend(self.custom.values())
        for source in sources:
            try:
                result.extend(await source.models())
            except ProviderError:
                pass
        return result

    async def health(self) -> dict[str, dict[str, Any]]:
        """Sondas de salud concurrentes, con deadline corto y caché por proveedor.

        Antes las sondas eran secuenciales y sin límite propio: un proveedor
        colgado podía bloquear /health durante minutos (timeout de inferencia).
        La caché usa los intervalos de config.health: las dependencias locales
        se revalidan a menudo y los proveedores externos con mucha menos
        frecuencia (cada sonda externa es una llamada a un tercero).
        """
        sources: list[tuple[str, Any, bool]] = []
        if self.config.providers.ollama.enabled:
            sources.append(("ollama", self.ollama, False))
        if self.config.providers.deepseek.enabled:
            sources.append(("deepseek", self.deepseek, True))
        if self.config.providers.huggingface_local.enabled:
            sources.append(("huggingface_local", self.huggingface_local, False))
        for provider_id, provider in self.custom.items():
            item = next(
                (c for c in self.config.providers.custom if c.id.lower() == provider_id),
                None,
            )
            external = item is None or not is_local_deployment(item.deployment)
            sources.append((provider_id, provider, external))

        now = asyncio.get_running_loop().time()
        checks: dict[str, dict[str, Any]] = {}
        pending: list[tuple[str, Any, bool]] = []
        for provider_id, provider, external in sources:
            cached = self._health_cache.get(provider_id)
            if cached is not None and cached[0] > now:
                checks[provider_id] = cached[1]
            else:
                pending.append((provider_id, provider, external))
        if pending:
            results = await asyncio.gather(
                *(self._probe_health(provider) for _, provider, _ in pending)
            )
            health_config = self.config.health
            for (provider_id, _, external), result in zip(pending, results, strict=True):
                ttl = (
                    health_config.external_providers_interval_seconds
                    if external
                    else health_config.local_dependencies_interval_seconds
                )
                self._health_cache[provider_id] = (now + ttl, result)
                checks[provider_id] = result
        return checks

    async def _probe_health(self, provider: Any) -> dict[str, Any]:
        timeout = self.config.health.probe_timeout_seconds
        try:
            return await asyncio.wait_for(self._provider_health(provider), timeout)
        except asyncio.TimeoutError:
            return {
                "status": "unavailable",
                "detail": f"la sonda de salud superó el deadline de {timeout:g}s",
                "latency_ms": timeout * 1000,
            }

    async def resource_snapshot(self) -> dict[str, Any]:
        if not self.config.providers.ollama.enabled:
            return {
                "provider": "ollama",
                "used_vram_bytes": 0,
                "reserved_vram_bytes": 0,
                "loaded_models": [],
            }
        return await self.ollama.lifecycle.resource_snapshot()

    @staticmethod
    async def _provider_health(provider: Any) -> dict[str, Any]:
        provider_health = getattr(provider, "health", None)
        if callable(provider_health):
            return await provider_health()
        started = asyncio.get_running_loop().time()
        try:
            models = await provider.models()
            return {
                "status": "healthy" if models else "degraded",
                "detail": f"{len(models)} modelos disponibles",
                "latency_ms": (asyncio.get_running_loop().time() - started) * 1000,
            }
        except ProviderError as error:
            return {
                "status": "unavailable",
                "detail": f"{error.code}: proveedor no disponible",
                "latency_ms": (asyncio.get_running_loop().time() - started) * 1000,
            }

    async def select(self, request: TaskCreateRequest, count: int, roles: list[str]) -> list[ModelReference]:
        allowed = {item.lower() for item in request.model_requirements.allowed_providers}
        catalog = [item for item in await self.models() if item["provider"].lower() in allowed]
        if not request.model_requirements.cloud_allowed:
            # Fail-closed: sin cloud_allowed solo entran deployments locales;
            # "api" o valores desconocidos se tratan como externos.
            catalog = [item for item in catalog if is_local_deployment(item.get("deployment"))]
        catalog = [item for item in catalog if item.get("compatibility") != "incompatible"]
        required_capability = "embedding" if request.inference_kind == InferenceKind.embedding else "completion"
        capability_catalog = [
            item for item in catalog
            if required_capability in set(item.get("capabilities") or (["completion"] if required_capability == "completion" else []))
        ]
        required_context = estimate_required_context(request)
        context_catalog = [
            item for item in capability_catalog
            if context_fits_with_capped_output(request, item.get("context_window"))
        ]
        target = request.model_requirements.target_model
        if target is not None:
            def matches_target(item: dict[str, Any]) -> bool:
                return (
                    item["provider"].lower() == target.provider.lower()
                    and str(item.get("deployment") or "").lower() == target.deployment.lower()
                    and item["name"] == target.model
                )

            target_available = [item for item in catalog if matches_target(item)]
            target_capable = [item for item in capability_catalog if matches_target(item)]
            target_items = [item for item in context_catalog if matches_target(item)]
            if target_items:
                chosen = target_items[0]
                return [
                    ModelReference(
                        provider=chosen["provider"],
                        deployment=chosen["deployment"],
                        model=chosen["name"],
                        role=roles[index],
                    )
                    for index in range(count)
                ]
            if not request.model_requirements.fallback_allowed:
                identity = f"{target.provider}/{target.deployment}/{target.model}"
                if target_capable and target_capable[0].get("context_window") is None:
                    raise ProviderError("CONTEXT_WINDOW_UNKNOWN", f"El modelo exacto {identity} no declara su contexto")
                if target_capable:
                    window = target_capable[0].get("context_window")
                    raise ProviderError(
                        "CONTEXT_LIMIT_EXCEEDED",
                        f"El modelo exacto {identity} admite {window} tokens y la inferencia requiere {required_context}",
                    )
                if target_available:
                    raise ProviderError(
                        "MODEL_CAPABILITY_MISMATCH",
                        f"El modelo exacto {identity} no declara capacidad {required_capability}",
                    )
                raise ProviderError("MODEL_UNAVAILABLE", f"Modelo exacto no disponible: {identity}")

        preferred = request.model_requirements.preferred_model or (target.model if target is not None else None)
        if preferred:
            preferred_available = [item for item in catalog if item["name"] == preferred]
            preferred_capable = [item for item in capability_catalog if item["name"] == preferred]
            preferred_items = [item for item in context_catalog if item["name"] == preferred]
            if preferred_items:
                context_catalog = preferred_items + [item for item in context_catalog if item["name"] != preferred]
            elif preferred_capable and preferred_capable[0].get("context_window") is None \
                    and not request.model_requirements.fallback_allowed:
                raise ProviderError("CONTEXT_WINDOW_UNKNOWN", "El modelo preferido no declara su contexto")
            elif preferred_capable and not request.model_requirements.fallback_allowed:
                window = preferred_capable[0].get("context_window")
                raise ProviderError(
                    "CONTEXT_LIMIT_EXCEEDED",
                    f"El modelo preferido admite {window} tokens y la inferencia requiere {required_context}",
                )
            elif preferred_available and not request.model_requirements.fallback_allowed:
                raise ProviderError(
                    "MODEL_CAPABILITY_MISMATCH",
                    f"El modelo preferido no declara capacidad {required_capability}",
                )
            elif not request.model_requirements.fallback_allowed:
                raise ProviderError("MODEL_UNAVAILABLE", f"Modelo preferido no disponible: {preferred}")
        if not context_catalog and any(item.get("context_window") is None for item in capability_catalog):
            raise ProviderError("CONTEXT_WINDOW_UNKNOWN", "Ningún modelo permitido declara contexto utilizable")
        if not context_catalog and capability_catalog:
            raise ProviderError("CONTEXT_LIMIT_EXCEEDED", "Ningún modelo permitido admite el contexto requerido")
        if not context_catalog and catalog:
            raise ProviderError(
                "MODEL_CAPABILITY_MISMATCH",
                f"Ningún modelo permitido declara capacidad {required_capability}",
            )
        if not context_catalog:
            raise ProviderError("MODEL_UNAVAILABLE", "No hay modelos permitidos disponibles", retryable=True)
        return [ModelReference(provider=context_catalog[i % len(context_catalog)]["provider"],
                               deployment=context_catalog[i % len(context_catalog)]["deployment"],
                               model=context_catalog[i % len(context_catalog)]["name"], role=roles[i]) for i in range(count)]

    async def propose(self, request: TaskCreateRequest, model: ModelReference, ordinal: int) -> ModelOutput:
        system = None
        if request.execution.strategy == ExecutionStrategy.mixture_of_agents:
            system = role_system_prompt(model.role) or ROLE_SYSTEM_PROMPTS["proposer"]
        return await self._generate(request, model, self._user_prompt(request), system=system)

    async def synthesize(self, request: TaskCreateRequest, model: ModelReference, proposals: list[ModelOutput]) -> ModelOutput:
        candidates = "\n\n".join(
            f"<candidate_{i+1}>\n{neutralize_consensus_delimiters(o.content or '')}\n</candidate_{i+1}>"
            for i, o in enumerate(proposals)
        )
        prompt = (
            f"<original_request>\n{neutralize_consensus_delimiters(self._user_prompt(request))}\n</original_request>\n\n"
            f"<candidates>\n{candidates}\n</candidates>"
        )
        return await self._generate(request, model, prompt, system=ROLE_SYSTEM_PROMPTS["arbiter"])

    @staticmethod
    def _resolve_catalog_entry(
        catalog: list[dict[str, Any]],
        model: ModelReference,
        label: str,
    ) -> dict[str, Any]:
        """Busca el modelo exacto (nombre + deployment) o falla con el código adecuado."""
        entry = next(
            (
                item for item in catalog
                if item["name"] == model.model
                and str(item.get("deployment") or "").lower() == model.deployment.lower()
            ),
            None,
        )
        if entry is None:
            same_name = any(item["name"] == model.model for item in catalog)
            code = "MODEL_DEPLOYMENT_MISMATCH" if same_name else "MODEL_UNAVAILABLE"
            raise ProviderError(code, f"Modelo {label} no disponible: {model.deployment}/{model.model}")
        return entry

    @staticmethod
    def _reject_incompatible(entry: dict[str, Any], identity: str, default_detail: str) -> None:
        if entry.get("compatibility") == "incompatible":
            detail = entry.get("compatibility_error") or default_detail
            raise ProviderError(
                "MODEL_COMPATIBILITY_MISMATCH",
                f"Modelo {identity} marcado como no compatible: {detail}",
            )

    async def _generate(
        self,
        request: TaskCreateRequest,
        model: ModelReference,
        prompt: str,
        system: str | None = None,
    ) -> ModelOutput:
        allow_parallel = (
            request.execution.strategy == ExecutionStrategy.mixture_of_agents
            and request.execution.preset == ExecutionPreset.slow
        )
        inference_slot = self._parallel_inference_slot if allow_parallel else self._serial_inference_slot
        async with inference_slot:
            allowed = {item.lower() for item in request.model_requirements.allowed_providers}
            provider_name = model.provider.lower()
            embedding = request.inference_kind == InferenceKind.embedding
            if provider_name not in allowed:
                raise ProviderError("PROVIDER_NOT_ALLOWED", f"Proveedor no permitido: {model.provider}")
            if provider_name == "ollama":
                if not self.config.providers.ollama.enabled:
                    raise ProviderError("PROVIDER_UNAVAILABLE", "Ollama está deshabilitado")
                entry = self._resolve_catalog_entry(await self.ollama.models(), model, "Ollama")
                if not is_local_deployment(entry.get("deployment")) and not request.model_requirements.cloud_allowed:
                    raise ProviderError("CLOUD_NOT_ALLOWED", f"El modelo {model.model} requiere cloud")
                if embedding:
                    return await self.ollama.embed(request, model.model, prompt)
                return await self.ollama.generate(request, model.model, prompt, system=system)
            if provider_name == "deepseek":
                if embedding:
                    raise ProviderError("PROVIDER_CAPABILITY_MISMATCH", "DeepSeek no admite embeddings en este adapter")
                if not self.config.providers.deepseek.enabled:
                    raise ProviderError("PROVIDER_UNAVAILABLE", "DeepSeek está deshabilitado")
                if not request.model_requirements.cloud_allowed:
                    raise ProviderError("CLOUD_NOT_ALLOWED", "DeepSeek requiere cloud_allowed=true")
                self._resolve_catalog_entry(await self.deepseek.models(), model, "DeepSeek")
                return await self.deepseek.generate(request, model.model, prompt, system=system)
            if provider_name == "huggingface_local":
                if embedding:
                    raise ProviderError(
                        "PROVIDER_CAPABILITY_MISMATCH",
                        "HuggingFaceLocalProvider no admite embeddings en este adapter",
                    )
                if not self.config.providers.huggingface_local.enabled:
                    raise ProviderError("PROVIDER_UNAVAILABLE", "Hugging Face local esta deshabilitado")
                entry = self._resolve_catalog_entry(
                    await self.huggingface_local.models(), model, "Hugging Face local"
                )
                self._reject_incompatible(
                    entry,
                    f"huggingface_local/{model.model}",
                    "No compatible con HuggingFaceLocalProvider",
                )
                return await self.huggingface_local.generate(request, model.model, prompt, system=system)
            if provider_name in self.custom:
                if not request.model_requirements.cloud_allowed and not is_local_deployment(model.deployment):
                    raise ProviderError("CLOUD_NOT_ALLOWED", f"{model.provider} requiere cloud_allowed=true")
                provider = self.custom[provider_name]
                entry = self._resolve_catalog_entry(await provider.models(), model, model.provider)
                self._reject_incompatible(
                    entry,
                    f"{model.provider}/{model.model}",
                    "No compatible con /chat/completions",
                )
                if embedding:
                    return await provider.embed(request, model.model, prompt)
                return await provider.generate(request, model.model, prompt, system=system)
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Proveedor no soportado: {model.provider}")


def build_provider(config: BrokerConfig):
    return BootstrapModelProvider() if config.processing.provider_mode == "bootstrap" else RoutedModelProvider(config)
