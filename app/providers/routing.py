from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.config import BrokerConfig, TaskAffinityConfig, effective_max_parallel_invocations
from app.model_enrichment import ModelEnrichment
from app.model_stats import ModelKey, ModelStats
from app.model_timing import estimate_seconds
from app.prompt_compressor import PromptCompressor
from app.providers.base import (
    ROLE_SYSTEM_PROMPTS,
    AgentTurn,
    ModelOutput,
    ProviderError,
    context_fits_with_capped_output,
    estimate_input_tokens,
    estimate_required_context,
    neutralize_consensus_delimiters,
    role_system_prompt,
)
from app.providers.bootstrap import BootstrapModelProvider
from app.providers.deepseek import DeepSeekProvider
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
from app.task_classifier import classify_task_type

logger = logging.getLogger("ai_broker.routing")

# El conjunto de modelos en VRAM se consulta al runtime y cambia al cargar o
# descargar uno. Con caché corta se evita una consulta por candidato sin que el
# dato llegue a envejecer dentro de una misma decisión.
_LOADED_MODELS_TTL_SECONDS = 2.0


def _entry_key(entry: dict[str, Any], task_type: str) -> ModelKey:
    """Identidad del candidato en la tabla de métricas (app.model_stats)."""
    return (
        str(entry.get("provider") or "").lower(),
        str(entry.get("deployment") or "").lower(),
        str(entry.get("name") or "").lower(),
        task_type,
    )


def task_affinity_patterns(settings: TaskAffinityConfig, task_type: str) -> list[str]:
    """Patrones que apartan modelos en ese tipo de tarea: los de propósito
    único (siempre) más los vetados para ese tipo concreto."""
    return [*settings.exclude_always, *settings.exclude_by_task_type.get(task_type, [])]


def task_affinity_excluded(
    catalog: list[dict[str, Any]], settings: TaskAffinityConfig, task_type: str,
) -> list[dict[str, Any]]:
    """Modelos del catálogo que el filtro de idoneidad apartaría en ese tipo de
    tarea. Fuente única: la usan la selección (`_filter_by_task_affinity`) y el
    panel de Enrutamiento, para que lo que se muestra no pueda divergir de lo
    que se aplica."""
    if not settings.enabled:
        return []
    patterns = task_affinity_patterns(settings, task_type)
    if not patterns:
        return []
    return [entry for entry in catalog if _matches_any(entry, patterns)]


def _matches_any(entry: dict[str, Any], patterns: list[str]) -> bool:
    """Patrones fnmatch contra el nombre del modelo y su catalog_id de
    models.dev: el mismo modelo se llama distinto según el proveedor
    (`qwen3-coder:30b` en Ollama, `qwen/qwen3-coder-30b` en el catálogo)."""
    catalog = entry.get("catalog") or {}
    identities = [
        str(entry.get("name") or "").lower(),
        str(catalog.get("catalog_id") or "").lower(),
    ]
    return any(
        fnmatch(identity, pattern.lower())
        for identity in identities if identity
        for pattern in patterns
    )


class RoutedModelProvider:
    def __init__(self, config: BrokerConfig, *, ollama: OllamaProvider | None = None,
                 deepseek: DeepSeekProvider | None = None,
                 custom: dict[str, OpenAICompatibleProvider] | None = None,
                 stats_loader: Callable[[], dict[ModelKey, ModelStats]] | None = None) -> None:
        self.config = config
        self.ollama = ollama or OllamaProvider(config)
        self.deepseek = deepseek or DeepSeekProvider(config.providers.deepseek)
        self.custom = custom if custom is not None else self._build_custom_providers(config)
        # Evidencia operativa para la selección adaptativa (app.model_stats);
        # sin loader el router mantiene el orden del catálogo.
        self._stats_loader = stats_loader
        self._parallel_limit = effective_max_parallel_invocations(config)
        self._serial_inference_slot = asyncio.Semaphore(1)
        self._parallel_inference_slot = asyncio.Semaphore(self._parallel_limit)
        # Carril propio de las medidas en segundo plano (sondeo en sombra):
        # nunca compite por los slots que sirven respuestas, y de uno en uno
        # porque solo se mide a un aspirante a la vez.
        self._background_inference_slot = asyncio.Semaphore(1)
        self.prompt_compressor = self._build_prompt_compressor(config)
        self.model_enrichment = ModelEnrichment(
            config.model_enrichment,
            cache_path=Path(config.persistence.database).parent / "models_dev_catalog.json",
        )
        # Caché de sondas de salud: provider_id -> (expira_en_monotonic, resultado).
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Modelos en VRAM: (expira_en_monotonic, nombres) o None si no se pudo
        # consultar. Alimenta la estimación de tiempo (caliente vs frío).
        self._loaded_models_cache: tuple[float, frozenset[str] | None] | None = None

    @staticmethod
    def _build_prompt_compressor(config: BrokerConfig) -> PromptCompressor:
        settings = config.prompt_compression
        return PromptCompressor(
            enabled=settings.enabled,
            level=settings.level,
            min_chars=settings.min_chars,
        )

    def user_prompt(self, request: TaskCreateRequest) -> str:
        """Prompt que viaja al proveedor; el original persiste intacto en la tarea.

        La tarea puede traer un override (`prompt_compression`): "off" envía el
        texto tal cual y un nivel concreto sustituye al de la configuración
        global. Los embeddings nunca se comprimen: alterar el texto altera el vector.
        """
        if request.inference_kind == InferenceKind.embedding:
            return request.content.prompt
        override = request.prompt_compression
        if override == "off":
            return request.content.prompt
        if override is not None:
            return PromptCompressor(
                enabled=True,
                level=override,
                min_chars=self.config.prompt_compression.min_chars,
            ).compress_text(request.content.prompt)
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
        self.custom = new_custom
        self.prompt_compressor = self._build_prompt_compressor(config)
        self.model_enrichment.reload_settings(config.model_enrichment)
        self._rebuild_inference_slots(config)
        # El conjunto de proveedores puede haber cambiado: las sondas cacheadas
        # dejan de ser representativas.
        self._health_cache.clear()
        self._loaded_models_cache = None

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
        for provider in self.custom.values():
            await provider.close()

    async def models(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        sources: list[Any] = []
        if self.config.providers.ollama.enabled:
            sources.append(self.ollama)
        if self.config.providers.deepseek.enabled:
            sources.append(self.deepseek)
        sources.extend(self.custom.values())
        for source in sources:
            try:
                result.extend(await source.models())
            except ProviderError:
                pass
        await self.model_enrichment.ensure_loaded()
        return [self.model_enrichment.enrich_entry(item) for item in result]

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

    def model_stats_snapshot(self) -> dict[ModelKey, ModelStats]:
        """Evidencia operativa actual. La usa el sondeo en sombra para saber a
        quién le falta medida; la selección la consume por la vía interna."""
        return self._load_stats()

    def _load_stats(self) -> dict[ModelKey, ModelStats]:
        """Evidencia operativa, o vacío si no hay loader o falla la lectura.
        La selección nunca debe caerse por un problema leyendo métricas."""
        if self._stats_loader is None:
            return {}
        try:
            return self._stats_loader() or {}
        except Exception:
            logger.warning("routing.stats_unavailable", exc_info=True)
            return {}

    @staticmethod
    def _attempts(
        stats: dict[ModelKey, ModelStats], entry: dict[str, Any], task_type: str,
    ) -> int:
        """Invocaciones registradas del modelo EN ESE tipo de tarea, incluidas
        las que aún no llegan a min_invocations: son las que dicen cuánta
        evidencia falta por reunir."""
        stat = stats.get(_entry_key(entry, task_type))
        return stat.attempts if stat is not None else 0

    def _rank_candidates(
        self,
        catalog: list[dict[str, Any]],
        request: TaskCreateRequest,
        loaded_models: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Ordena los candidatos por TIEMPO ESPERADO hasta una respuesta buena.

        Sustituye al score multiobjetivo sin unidades que había antes. El
        criterio es el del usuario final: cuanto menos espera, mejor. La
        estimación (app.model_timing) parte de la latencia medida, elige la
        media en caliente o en frío según el estado actual de la VRAM, y la
        divide por la tasa de éxito, porque un modelo que falla obliga a
        reintentar y ese reintento también es espera.

        Los modelos SIN evidencia suficiente no se puntúan: no se les inventa
        un tiempo, así que van detrás de todos los medidos. No quedan
        condenados, porque la exploración y el sondeo en sombra son los que
        les consiguen las medidas sin hacer esperar a nadie. Entre ellos, el
        desempate lo decide _evidence_rank: primero el que ya está a medias,
        luego los no probados, después los medidos por debajo del umbral.

        En arranque en frío absoluto (nadie tiene historial) todos empatan sin
        estimación y el orden lo pone _evidence_rank, no el catálogo: atarlo al
        orden del proveedor daba siempre la petición al último modelo que se
        hubiera descargado en Ollama.

        Las métricas se filtran por el tipo de tarea de `request`
        (app.task_classifier): un modelo bueno en prosa no hereda ese
        historial al puntuar una tarea de código, ni viceversa. Cada
        (modelo, tipo de tarea) acumula evidencia de forma independiente.
        """
        routing = self.config.routing
        if not routing.adaptive_selection or len(catalog) < 2:
            return catalog
        # Sin estadísticas el ranking es neutro, pero la exploración sí debe
        # correr: si aquí se devolviera el catálogo tal cual, el arranque en
        # frío (nadie tiene historial) nunca generaría el historial que
        # necesita para dejar de serlo.
        stats = self._load_stats()
        task_type = classify_task_type(request)
        # Nivel 0: la carga real de esta petición, calculada sin coste (es el
        # mismo recuento determinista que decide si cabe en el contexto). Sin
        # esto, el ranking compararía modelos por lo que tardaron con la
        # petición media, no con la que hay delante.
        request_tokens = estimate_input_tokens(request)
        estimates = {
            id(entry): estimate_seconds(
                stats.get(_entry_key(entry, task_type)),
                loaded=self._loaded_state(entry, loaded_models),
                min_invocations=routing.min_invocations,
                request_tokens=request_tokens,
            )
            for entry in catalog
        }

        def sort_key(indexed: tuple[int, dict[str, Any]]) -> tuple:
            index, entry = indexed
            estimate = estimates[id(entry)]
            attempts = self._attempts(stats, entry, task_type)
            if estimate is not None:
                # Bucket 0: hay medida. Ordena por segundos, de menos a más.
                return (0, round(estimate.seconds, 3), 0, 0, index)
            return (1, 0.0, *self._evidence_rank(attempts, routing.min_invocations), index)

        def describe(entry: dict[str, Any]) -> str:
            estimate = estimates[id(entry)]
            seconds = "sin medir" if estimate is None else f"{estimate.seconds:.1f}s"
            return f"{entry['provider']}/{entry['name']}: {seconds}"

        ranked = [entry for _, entry in sorted(enumerate(catalog), key=sort_key)]
        if ranked != catalog:
            logger.debug(
                "routing.time_ranking",
                extra={
                    "event": "routing.time_ranking",
                    "task_type": task_type,
                    "ranking": [describe(entry) for entry in ranked[:5]],
                },
            )
        return self._apply_exploration(ranked, routing, stats, task_type)

    @staticmethod
    def _loaded_state(
        entry: dict[str, Any], loaded_models: frozenset[str] | None,
    ) -> bool | None:
        """¿Está este modelo cargado ahora mismo? None cuando la pregunta no
        aplica (cloud no carga nada) o no se pudo responder."""
        if loaded_models is None or not is_local_deployment(entry.get("deployment")):
            return None
        return str(entry.get("name") or "") in loaded_models

    async def loaded_local_models(self) -> frozenset[str] | None:
        """Modelos actualmente en VRAM, con caché corta.

        Es estado real del sistema consultado en el instante de decidir, no un
        prior: un modelo local ya cargado responde en segundos y el mismo
        modelo en frío paga primero la carga desde disco. La caché evita una
        consulta al runtime por cada candidato de cada selección; el TTL es
        corto porque el dato caduca en cuanto entra o sale un modelo."""
        now = time.monotonic()
        cached = self._loaded_models_cache
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            snapshot = await self.resource_snapshot()
        except Exception:
            # La selección no puede caerse porque el runtime no conteste: sin
            # el dato se estima con la media global, que sigue siendo medida.
            logger.debug("routing.loaded_models_unavailable", exc_info=True)
            self._loaded_models_cache = (now + _LOADED_MODELS_TTL_SECONDS, None)
            return None
        names = frozenset(
            str(item.get("model"))
            for item in snapshot.get("loaded_models") or []
            if item.get("model")
        )
        self._loaded_models_cache = (now + _LOADED_MODELS_TTL_SECONDS, names)
        return names

    @staticmethod
    def _evidence_rank(attempts: int, min_invocations: int) -> tuple[int, int]:
        """Desempate entre candidatos indistinguibles por score, orientado a
        reunir evidencia cuanto antes.

        Rotar uno a uno por todo el catálogo repartiría una invocación a cada
        modelo y ninguno alcanzaría min_invocations en cientos de peticiones.
        Por eso primero se remata al que ya tiene historial a medias (bucket 0,
        el más avanzado antes), después se estrena a los no probados (bucket 1)
        y por último van los que ya tienen evidencia suficiente (bucket 2,
        el menos usado primero)."""
        if attempts <= 0:
            return (1, 0)
        if attempts < min_invocations:
            return (0, -attempts)
        return (2, attempts)

    def _apply_exploration(
        self,
        ranked: list[dict[str, Any]],
        routing: Any,
        stats: dict[ModelKey, ModelStats],
        task_type: str,
    ) -> list[dict[str, Any]]:
        """Epsilon-greedy dirigido: con probabilidad exploration_rate, asciende
        otro candidato por delante del primero. Sin esto, en cuanto un modelo
        gana la comparación de score queda fijado para siempre: solo él sigue
        recibiendo invocaciones, así que ningún otro candidato acumula
        evidencia para disputarle el puesto (rich-get-richer).

        La exploración no es uniforme sobre todo el catálogo: se sortea entre
        los candidatos a los que aún les falta evidencia en este tipo de tarea,
        y dentro de esos se remata primero al que ya está a medias. Con ~80
        modelos elegibles, un sorteo uniforme le da a cada alternativa concreta
        un 0.2% por petición: las exploraciones se dispersarían en decenas de
        modelos con una o dos invocaciones sueltas, ninguna de ellas suficiente
        para que su score deje de ser neutro. Rematando de uno en uno, cada
        modelo pasa a competir de verdad tras min_invocations exploraciones.
        Si ya todos están medidos se cae al sorteo uniforme, que es lo que
        evita que el campeón se eternice."""
        if routing.exploration_rate <= 0 or len(ranked) < 2:
            return ranked
        if random.random() >= routing.exploration_rate:
            return ranked
        attempts_by_index = {
            index: self._attempts(stats, ranked[index], task_type)
            for index in range(1, len(ranked))
        }
        under_measured = [
            index for index, attempts in attempts_by_index.items()
            if attempts < routing.min_invocations
        ]
        started = [index for index in under_measured if attempts_by_index[index] > 0]
        if started:
            most_advanced = max(attempts_by_index[index] for index in started)
            under_measured = [index for index in started if attempts_by_index[index] == most_advanced]
        index = random.choice(under_measured) if under_measured else random.randrange(1, len(ranked))
        explored = list(ranked)
        explored[0], explored[index] = explored[index], explored[0]
        logger.info(
            "routing.exploration",
            extra={
                "event": "routing.exploration",
                "task_type": task_type,
                "directed": bool(under_measured),
                "explored": f"{explored[0]['provider']}/{explored[0]['name']}",
                "displaced": f"{ranked[0]['provider']}/{ranked[0]['name']}",
            },
        )
        return explored

    def _filter_by_task_affinity(
        self, catalog: list[dict[str, Any]], request: TaskCreateRequest,
    ) -> list[dict[str, Any]]:
        """Descarta los modelos que *pueden* atender la petición pero no
        *deberían* (config.task_affinity): un modelo de código redactando
        prosa, o un guardarraíl/OCR metido en la rotación general.

        Best-effort por diseño: si el filtro dejara la selección vacía se
        ignora. Ninguna preferencia debe convertir en irresoluble una tarea
        que el broker podía atender."""
        settings = self.config.task_affinity
        if not settings.enabled or not catalog:
            return catalog
        task_type = classify_task_type(request)
        excluded = task_affinity_excluded(catalog, settings, task_type)
        if not excluded:
            return catalog
        discarded = {id(entry) for entry in excluded}
        kept = [entry for entry in catalog if id(entry) not in discarded]
        if not kept:
            logger.info(
                "routing.affinity_ignored",
                extra={
                    "event": "routing.affinity_ignored",
                    "task_type": task_type,
                    "detail": "el filtro de idoneidad dejaría la selección sin candidatos",
                },
            )
            return catalog
        if len(kept) != len(catalog):
            logger.debug(
                "routing.affinity_filtered",
                extra={
                    "event": "routing.affinity_filtered",
                    "task_type": task_type,
                    "discarded": len(catalog) - len(kept),
                    "remaining": len(kept),
                },
            )
        return kept

    async def eligible_catalog(
        self, request: TaskCreateRequest,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Los tres conjuntos de elegibilidad de una petición, en orden de
        restricción creciente: (permitido, capaz, cabe en contexto).

        Extraído de `select` para que el sondeo en sombra (app.shadow_probe)
        elija aspirantes con EXACTAMENTE los mismos filtros —sobre todo la
        frontera de datos—. Duplicar esta cadena en otro sitio sería la forma
        más fácil de abrir un agujero de privacidad sin darse cuenta.
        """
        declared_providers = request.model_requirements.allowed_providers
        catalog = list(await self.models())
        if declared_providers is not None:
            # None = sin restricción: entra todo el catálogo configurado y la
            # frontera la pone la clasificación de datos, no una lista fija.
            allowed = {item.lower() for item in declared_providers}
            catalog = [item for item in catalog if item["provider"].lower() in allowed]
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
        context_catalog = [
            item for item in capability_catalog
            if context_fits_with_capped_output(request, item.get("context_window"))
        ]
        return catalog, capability_catalog, context_catalog

    async def select(self, request: TaskCreateRequest, count: int, roles: list[str]) -> list[ModelReference]:
        catalog, capability_catalog, context_catalog = await self.eligible_catalog(request)
        required_capability = "embedding" if request.inference_kind == InferenceKind.embedding else "completion"
        required_context = estimate_required_context(request)
        # Idoneidad + selección adaptativa. `context_catalog` sigue siendo el
        # conjunto ELEGIBLE (lo que el broker puede atender) y decide los
        # errores de más abajo; `ranked_catalog` es el conjunto PREFERIBLE ya
        # ordenado, del que sale la elección por defecto. target_model y
        # preferred_model tienen prioridad absoluta sobre ambos.
        ranked_catalog = self._rank_candidates(
            self._filter_by_task_affinity(context_catalog, request),
            request,
            await self.loaded_local_models(),
        )
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
                # Una preferencia explícita se respeta aunque el filtro de
                # idoneidad hubiera descartado ese modelo para este tipo de
                # tarea: la petición manda sobre la política.
                promoted = [item for item in ranked_catalog if item["name"] == preferred]
                promoted += [item for item in preferred_items if item not in promoted]
                ranked_catalog = promoted + [item for item in ranked_catalog if item["name"] != preferred]
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
        return [ModelReference(provider=ranked_catalog[i % len(ranked_catalog)]["provider"],
                               deployment=ranked_catalog[i % len(ranked_catalog)]["deployment"],
                               model=ranked_catalog[i % len(ranked_catalog)]["name"], role=roles[i]) for i in range(count)]

    async def propose(self, request: TaskCreateRequest, model: ModelReference, ordinal: int) -> ModelOutput:
        system = None
        if request.execution.strategy == ExecutionStrategy.mixture_of_agents:
            system = role_system_prompt(model.role) or ROLE_SYSTEM_PROMPTS["proposer"]
        return await self._generate(request, model, self.user_prompt(request), system=system)

    async def measure(self, request: TaskCreateRequest, model: ModelReference) -> ModelOutput:
        """Invocación cuyo único producto es la medida: nadie espera su salida.

        La usa el sondeo en sombra (app.shadow_probe) para dar evidencia a un
        modelo que aún no la tiene, sin que ningún usuario pague la espera. Se
        ejecuta por un carril aparte del que sirve respuestas."""
        return await self._generate(request, model, self.user_prompt(request), background=True)

    def _resolve_agent_provider(self, model: ModelReference) -> Any:
        """Proveedor con soporte de chat+tools para el modelo agéntico."""
        provider_name = model.provider.lower()
        if provider_name in self.custom:
            provider: Any = self.custom[provider_name]
        elif provider_name == "ollama":
            provider = self.ollama
        elif provider_name == "deepseek":
            provider = self.deepseek
        else:
            provider = None
        if provider is None or not hasattr(provider, "chat_tools"):
            raise ProviderError(
                "MODEL_CAPABILITY_MISMATCH",
                f"El proveedor {model.provider} no soporta ejecución agéntica con tools",
            )
        return provider

    async def ensure_agent_capable(self, model: ModelReference) -> None:
        """Falla pronto (antes de arrancar el loop) si el modelo no puede ser
        un agente: proveedor sin chat+tools, o tools verificado como no
        soportado por el sondeo/runtime. Un 'sin verificar' se deja pasar: no
        hay evidencia de que falle y el propio loop lo descubriría."""
        self._resolve_agent_provider(model)
        entry = next(
            (
                item for item in await self.models()
                if item.get("name") == model.model
                and str(item.get("provider") or "").lower() == model.provider.lower()
            ),
            None,
        )
        if entry is None:
            return
        features = entry.get("features") or {}
        catalog = entry.get("catalog") or {}
        tools_support = features.get("tools")
        if tools_support is None and isinstance(catalog.get("tools"), bool):
            tools_support = catalog.get("tools")
        if tools_support is False:
            raise ProviderError(
                "MODEL_CAPABILITY_MISMATCH",
                f"El modelo {model.provider}/{model.model} no soporta tools (function calling); "
                "elige otro modelo o usa la estrategia single.",
            )

    async def agent_turn(
        self,
        request: TaskCreateRequest,
        model: ModelReference,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        allow_parallel: bool = False,
    ) -> AgentTurn:
        """Una ronda de chat con tools para el loop agéntico. Cuenta como una
        invocación LLM: usa el semáforo paralelo cuando el llamante lo permite
        (proponentes de un mixture slow), serial en el resto de casos."""
        provider = self._resolve_agent_provider(model)
        slot = self._parallel_inference_slot if allow_parallel else self._serial_inference_slot
        async with slot:
            return await provider.chat_tools(request, model.model, messages, tools)

    async def synthesize(self, request: TaskCreateRequest, model: ModelReference, proposals: list[ModelOutput]) -> ModelOutput:
        candidates = "\n\n".join(
            f"<candidate_{i+1}>\n{neutralize_consensus_delimiters(o.content or '')}\n</candidate_{i+1}>"
            for i, o in enumerate(proposals)
        )
        prompt = (
            f"<original_request>\n{neutralize_consensus_delimiters(self.user_prompt(request))}\n</original_request>\n\n"
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
        *,
        background: bool = False,
    ) -> ModelOutput:
        allow_parallel = (
            request.execution.strategy == ExecutionStrategy.mixture_of_agents
            and request.execution.preset == ExecutionPreset.slow
        )
        if background:
            # Una medida en segundo plano NO puede ocupar el slot que sirve
            # respuestas: si lo hiciera, la siguiente tarea de la cola
            # esperaría a que terminase el sondeo, que es justo la espera que
            # todo este trabajo pretende eliminar. Tiene su propio carril de
            # uno en uno.
            inference_slot = self._background_inference_slot
        else:
            inference_slot = self._parallel_inference_slot if allow_parallel else self._serial_inference_slot
        async with inference_slot:
            declared = request.model_requirements.allowed_providers
            allowed = {item.lower() for item in declared} if declared is not None else None
            provider_name = model.provider.lower()
            embedding = request.inference_kind == InferenceKind.embedding
            if allowed is not None and provider_name not in allowed:
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


def build_provider(
    config: BrokerConfig,
    stats_loader: Callable[[], dict[ModelKey, ModelStats]] | None = None,
):
    if config.processing.provider_mode == "bootstrap":
        return BootstrapModelProvider()
    return RoutedModelProvider(config, stats_loader=stats_loader)
