from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from app import execution_fingerprint as fingerprint
from app.config import BrokerConfig, local_memory_budget_bytes
from app.providers.base import (
    AgentTurn,
    ModelOutput,
    ProviderError,
    ToolCall,
    _CatalogCache,
    _estimation_text,
    effective_generation,
    enforce_context_limit,
    estimate_required_context,
    provider_error_from_http,
    provider_http_error_message,
    request_with_context_capped_output,
)
from app.schemas import OutputFormat, TaskCreateRequest

logger = logging.getLogger("ai_broker.ollama")

# Los metadatos de /api/show (plantilla de chat, tokenizador, contexto) tienen
# su propia caché, más larga que la del catálogo: son caros —una llamada por
# modelo— y cambian con muchísima menos frecuencia que la lista de modelos. La
# entrada se invalida sola cuando cambia el digest del modelo, así que una
# reinstalación se ve al momento; el TTL solo existe para acabar detectando los
# cambios que NO tocan el digest, como una plantilla reescrita por una
# actualización de Ollama.
METADATA_CACHE_SECONDS = 300.0
# Modelos cuyos metadatos se piden a la vez al refrescar el catálogo. Sin esto
# un catálogo de decenas de modelos encadenaría decenas de peticiones en serie.
METADATA_FETCH_CONCURRENCY = 4

# Metadatos de un modelo del que no se pudo saber nada: ningún componente se
# rellena por inferencia, así que todos quedan vacíos y la huella los declarará
# `no_disponible`.
_EMPTY_METADATA: dict[str, Any] = {
    "context_window": None,
    "capabilities": [],
    "layers": None,
    "chat_template": None,
    "tokenizer": None,
}


def _gib(value: float) -> str:
    """Bytes en GiB para mensajes de error. La cifra la lee una persona que
    está decidiendo si cancelar: en bytes no le dice nada."""
    return f"{value / 1024**3:.1f} GB"


# Presupuesto por debajo del cual un modelo con capacidad "thinking" corre
# riesgo real de gastar todo num_predict razonando y devolver content vacío.
# Ollama activa el razonamiento por defecto en esos modelos.
THINKING_MIN_OUTPUT_TOKENS = 512


def _thinking_disabled(capabilities: Any, max_output_tokens: int) -> bool:
    """True si hay que mandar think=false. Con presupuesto corto el modelo
    agota num_predict en message.thinking y message.content llega vacío; por
    encima del umbral se deja razonar, que es donde aporta calidad."""
    declared = {str(item).lower() for item in (capabilities or [])}
    return "thinking" in declared and max_output_tokens < THINKING_MIN_OUTPUT_TOKENS


@dataclass(frozen=True)
class OffloadPlan:
    """Cómo se reparte un modelo entre GPU y CPU.

    `num_gpu` es el número de capas que van a la GPU, tal como lo entiende
    Ollama en `options.num_gpu`. None significa «no te lo digo, decide tú», que
    es lo que se manda cuando el modelo cabe entero: así una máquina con
    memoria de sobra sigue comportándose exactamente como antes.

    `gpu_bytes` es lo que de verdad ocupa el pool local, y es lo que se reserva
    en el lease. Reservar el modelo entero cuando dos tercios viven en RAM del
    sistema haría que la admisión rechazara trabajo que sí cabe.
    """

    num_gpu: int | None
    gpu_bytes: int
    layers: int | None = None
    total_layers: int | None = None

    @property
    def split(self) -> bool:
        return self.num_gpu is not None


def plan_offload(
    size_bytes: int,
    total_layers: int | None,
    budget_bytes: int,
    *,
    enabled: bool,
    reserve_ratio: float,
) -> OffloadPlan | None:
    """Reparto de capas para un modelo, o None si no hay reparto posible.

    Devolver None significa «este modelo no cabe y tampoco se puede repartir»:
    quien llama lo convierte en el error terminal de siempre. Pasa en dos
    casos, y los dos son honestos:

    - El reparto está desactivado por configuración.
    - No se sabe cuántas capas tiene el modelo (`block_count` no vino en
      /api/show). Sin ese dato, cualquier `num_gpu` sería inventado, y un
      número inventado aquí se paga cargando decenas de GB desde disco para
      morir al final.

    El cálculo reparte los pesos a partes iguales entre capas. Es una
    aproximación —embeddings y capa de salida no son una capa más— y se queda
    corta a propósito: sobreestimar el peso por capa deja menos capas en GPU,
    que falla hacia el lado seguro.
    """
    if size_bytes <= 0:
        # Peso desconocido: se sigue adelante sin reserva ni reparto, que es lo
        # que se venía haciendo. Inventar un tamaño para poder repartir sería
        # justo el error que este módulo evita en todo lo demás.
        return OffloadPlan(num_gpu=None, gpu_bytes=0, total_layers=total_layers)
    if budget_bytes <= 0:
        return None
    if size_bytes <= budget_bytes:
        # Cabe entero: no se toca nada. El cuerpo que llega a Ollama es el
        # mismo de siempre.
        return OffloadPlan(num_gpu=None, gpu_bytes=size_bytes, total_layers=total_layers)
    if not enabled or not total_layers or total_layers <= 0:
        return None
    usable = int(budget_bytes * (1.0 - reserve_ratio))
    bytes_per_layer = size_bytes / total_layers
    layers = int(usable // bytes_per_layer)
    if layers <= 0:
        # Ni una capa cabe: es un modelo demasiado grande para esta máquina, no
        # un caso de reparto. Se trata como terminal en vez de cargarlo entero
        # en CPU, que tardaría horas y nadie ha pedido eso.
        return None
    layers = min(layers, total_layers)
    return OffloadPlan(
        num_gpu=layers,
        gpu_bytes=int(layers * bytes_per_layer),
        layers=layers,
        total_layers=total_layers,
    )


def context_window_for(
    required_tokens: int, model_window: Any, *, minimum_tokens: int
) -> int | None:
    """Contexto a pedirle al runtime, o None si no hay que decir nada.

    La caché KV se dimensiona con el contexto, no con la petición: un modelo que
    declara 262.144 tokens reserva memoria para los 262.144 aunque se le
    pregunte la hora. En una máquina donde los pesos ya van justos, esa reserva
    es la que tumba la carga —y lo hace al final, después de haber colocado las
    capas—, así que decir el tamaño de verdad es la diferencia entre arrancar y
    no arrancar.

    Nunca se pide más de lo que el modelo declara: pasarse es un error del
    runtime, y además no haría falta —si la petición no cupiera, el broker ya
    la habría rechazado antes de llegar aquí.
    """
    try:
        window = int(model_window) if model_window is not None else None
    except (TypeError, ValueError):
        window = None
    wanted = max(int(required_tokens), int(minimum_tokens))
    if window is None:
        # Sin ventana declarada no hay con qué comparar. Se pide lo estimado:
        # es más de lo que la tarea necesita y menos de lo que el runtime
        # reservaría por su cuenta.
        return wanted
    if wanted >= window:
        # Ya se quiere todo lo que hay: callar y dejar el default es idéntico.
        return None
    return wanted


def _generation_options(
    request: TaskCreateRequest, plan: OffloadPlan | None = None, num_ctx: int | None = None
) -> dict[str, Any]:
    """Bloque `options` de /api/chat.

    `seed`, `top_p` y `num_gpu` solo aparecen cuando hay algo que decir: sin
    ellos el cuerpo enviado es EXACTAMENTE el de antes de que existieran, y los
    clientes actuales no notan el cambio. Mandar el valor por defecto del
    proveedor en su lugar sí lo notarían."""
    generation = request.generation
    options: dict[str, Any] = {
        "temperature": generation.temperature,
        "num_predict": generation.max_output_tokens,
    }
    if generation.seed is not None:
        options["seed"] = generation.seed
    if generation.top_p is not None:
        options["top_p"] = generation.top_p
    if plan is not None and plan.num_gpu is not None:
        options["num_gpu"] = plan.num_gpu
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    return options


def _empty_content_error(payload: dict[str, Any], think_disabled: bool) -> ProviderError:
    """El "no devolvió message.content" a secas es un callejón sin salida al
    depurar desde fuera: se añade por qué paró y si el razonamiento se comió
    el presupuesto."""
    message = payload.get("message") or {}
    thinking = message.get("thinking")
    thinking_chars = len(thinking) if isinstance(thinking, str) else 0
    done_reason = str(payload.get("done_reason") or "desconocido")
    detail = f"done_reason={done_reason}, eval_count={int(payload.get('eval_count') or 0)}"
    if thinking_chars:
        detail += f", thinking={thinking_chars} caracteres"
        if done_reason == "length":
            detail += (
                "; el razonamiento agotó max_output_tokens antes de responder"
                f" (think{'=false ya aplicado' if think_disabled else ' activo'})"
            )
    return ProviderError(
        "INVALID_PROVIDER_RESPONSE", f"Ollama no devolvió message.content ({detail})"
    )


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

    @property
    def _unified(self) -> bool:
        return self.config.resources.unified_memory_budget_gb is not None

    def _footprint(self, item: dict[str, Any]) -> int:
        """Lo que un modelo cargado le quita al pool que gobierna la admisión.

        Con memoria unificada hay que mirar el tamaño total: lo que el runtime
        deja fuera de la VRAM sale igualmente del mismo pool, y `size_vram` no
        lo ve. Contarlo por VRAM ahí abajo sería admitir trabajo contra memoria
        que en realidad ya está gastada.
        """
        return int(item.get("size" if self._unified else "size_vram") or 0)

    def _occupied(self, running: list[dict[str, Any]], names: set[str]) -> int:
        occupied = sum(self._footprint(item) for item in running)
        return occupied + sum(size for name, size in self._reserved_sizes.items() if name not in names)

    async def _ensure_capacity(self, model: str, estimated_size: int) -> None:
        running = await self.running()
        budget = local_memory_budget_bytes(self.config)
        # Distinción que antes no se hacía y que decide el destino de la tarea:
        # un modelo más grande que TODO el presupuesto no cabrá por mucho que
        # se espere, así que esperar sería mentirle al usuario. Los demás casos
        # son ocupación temporal y se resuelven cediendo el turno.
        if estimated_size > budget:
            raise ProviderError(
                "VRAM_MODEL_TOO_LARGE",
                self._too_large_message(model, estimated_size, budget),
            )
        running_names = {str(item.get("name") or item.get("model") or "") for item in running}
        occupied = self._occupied(running, running_names)
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
        occupied = self._occupied(refreshed, refreshed_names)
        if occupied + estimated_size > budget:
            # Retryable y con el detalle de quién ocupa: el coordinador lo
            # traduce en un aplazamiento con turno cedido, y el panel puede
            # decir de quién se está esperando en vez de un "espera" a secas.
            holders = sorted(
                {name for name in refreshed_names if name}
                | {name for name in self._reserved_sizes if name not in refreshed_names}
            )
            pool = "memoria unificada" if self._unified else "VRAM"
            error = ProviderError(
                "VRAM_INSUFFICIENT",
                f"No hay {pool} segura para cargar {model}: necesita {_gib(estimated_size)}, "
                f"hay {_gib(occupied)} ocupados de {_gib(budget)}",
                retryable=True,
            )
            error.memory_block = {  # type: ignore[attr-defined]
                "model": model,
                "needed_bytes": estimated_size,
                "occupied_bytes": occupied,
                "budget_bytes": budget,
                "holders": holders,
            }
            raise error

    def _too_large_message(self, model: str, estimated_size: int, budget: int) -> str:
        """El mensaje tiene que dejar claro qué NO es: quien lo lee acaba de ver
        el modelo funcionar en Ollama o LM Studio y buscará culpables (otro
        proceso, otro modelo cargado) que aquí no pintan nada. Este corte solo
        compara el peso del modelo con el presupuesto configurado."""
        resources = self.config.resources
        if resources.unified_memory_budget_gb is not None:
            techo = (
                f"el presupuesto de memoria unificada configurado, {_gib(budget)} "
                f"(unified_memory_budget_gb {resources.unified_memory_budget_gb:g} menos "
                f"{resources.vram_safety_margin_gb:g} de margen)"
            )
            salida = "Usa una cuantización menor o sube unified_memory_budget_gb."
        else:
            techo = (
                f"el presupuesto de VRAM configurado, {_gib(budget)} "
                f"(local_vram_budget_gb {resources.local_vram_budget_gb:g} menos "
                f"{resources.vram_safety_margin_gb:g} de margen)"
            )
            salida = (
                "El broker solo cuenta VRAM y no reparte capas a RAM aunque Ollama sepa "
                "hacerlo, de ahí que el mismo modelo sí corra suelto. Si esta máquina tiene "
                "memoria unificada (APU), declara resources.unified_memory_budget_gb con el "
                "pool completo; si no, usa una cuantización menor o sube local_vram_budget_gb."
            )
        return (
            f"{model} pesa {_gib(estimated_size)} y no cabe en {techo}. No es ocupación ajena: "
            f"el peso del modelo ya supera el presupuesto entero, así que no lo arregla esperar "
            f"ni descargar otros modelos. {salida}"
        )

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
                # Misma vara que la admisión (_footprint): si el panel midiera
                # solo VRAM en una máquina unificada, enseñaría un depósito más
                # vacío que el que de verdad decide si la siguiente tarea entra.
                "size_vram_bytes": self._footprint(item),
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
        # modelo -> (expira_en_monotonic, digest visto, metadatos).
        self._metadata_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}
        self._version_cache = _CatalogCache(METADATA_CACHE_SECONDS)

    async def reload_config(self, config: BrokerConfig) -> None:
        """Aplica la config nueva de verdad: cliente y lifecycle se reconstruyen
        si base_url o timeout cambian; el catálogo cacheado se descarta siempre."""
        self.config = config
        ollama = config.providers.ollama
        self._catalog_cache = _CatalogCache(ollama.catalog_cache_seconds)
        self._metadata_cache = {}
        self._version_cache = _CatalogCache(METADATA_CACHE_SECONDS)
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

    async def delete_model(self, model: str) -> int:
        """Borra un modelo local del disco. Devuelve los bytes que ocupaba.

        Lo hace el propio Ollama (`DELETE /api/delete`), no el broker a mano:
        varios modelos comparten blobs y solo el runtime sabe cuáles quedan sin
        referenciar. Borrar ficheros por nuestra cuenta dejaría el almacén
        inconsistente o se llevaría por delante capas de otro modelo.

        Antes se descarga de memoria: si estuviera cargado, quedaría una copia
        en VRAM sirviendo un modelo que ya no existe en disco. Un fallo al
        descargar no impide el borrado —puede no estar cargado siquiera—, pero
        sí se propaga cualquier fallo del borrado en sí.

        NO comprueba si hay trabajo en curso: eso lo decide quien llama, que es
        el único que sabe si la máquina está ocupada (ver el panel de Modelos).
        """
        catalog = await self.models()
        entry = next((item for item in catalog if item.get("name") == model), None)
        if entry is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo Ollama no encontrado: {model}")
        if entry.get("deployment") != "local":
            # Un modelo de Ollama Cloud no ocupa disco aquí; borrarlo no
            # liberaría nada y sí quitaría el acceso.
            raise ProviderError(
                "MODEL_NOT_LOCAL", f"{model} no es un modelo local: no hay nada que borrar del disco",
            )
        size_bytes = int(entry.get("size_bytes") or 0)
        try:
            await self.lifecycle.unload(model)
        except ProviderError:
            logger.info("ollama.delete_unload_skipped", extra={
                "event": "ollama.delete_unload_skipped", "model": model,
            })
        try:
            response = await self.client.request("DELETE", "/api/delete", json={"model": model})
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ProviderError(
                "MODEL_DELETE_FAILED", provider_http_error_message(error),
                retryable=error.response.status_code >= 500,
            ) from error
        except httpx.HTTPError as error:
            raise ProviderError("MODEL_DELETE_FAILED", str(error), retryable=True) from error
        # El catálogo cacheado sigue nombrando un modelo que ya no existe.
        self._catalog_cache.clear()
        return size_bytes

    async def models(self) -> list[dict[str, Any]]:
        cached = self._catalog_cache.get()
        if cached is not None:
            return cached
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            items = list(response.json().get("models") or [])
        except httpx.HTTPError as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error
        # Los metadatos se piden SIEMPRE, no solo cuando falta el contexto o las
        # capacidades: la plantilla de chat y el tokenizador forman parte de la
        # identidad de la ejecución (app.execution_fingerprint) y dejarlos al
        # azar de si el modelo declaró su contexto haría que la huella cambiara
        # sola. Con la caché de metadatos el coste es una llamada por modelo
        # cada METADATA_CACHE_SECONDS, o antes si cambia su digest.
        runtime_version = await self._runtime_version()
        metadata_by_model = await self._metadata_for(items)
        result = []
        for item in items:
            name = str(item.get("name") or item.get("model") or "")
            details = item.get("details") or {}
            metadata = metadata_by_model.get(name) or _EMPTY_METADATA
            context_window = (
                item.get("context_length")
                or details.get("context_length")
                or metadata["context_window"]
            )
            capabilities = item.get("capabilities") or metadata["capabilities"] or []
            entry = {
                "name": item.get("name") or item.get("model"), "provider": "ollama",
                "deployment": "cloud" if item.get("remote_host") else "local",
                "status": "available", "size_bytes": item.get("size", 0),
                "context_window": context_window, "capabilities": capabilities,
                "context_window_source": "reported",
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
                "layers": metadata.get("layers"),
                "compatibility": "compatible",
                "compatibility_checked_at": None,
                "compatibility_error": None,
                "features": self._declared_features(capabilities),
            }
            entry["execution_fingerprint"] = self._fingerprint(
                entry, item, details, metadata, runtime_version,
            )
            result.append(entry)
        self._catalog_cache.set(result)
        return result

    @staticmethod
    def _fingerprint(
        entry: dict[str, Any],
        item: dict[str, Any],
        details: dict[str, Any],
        metadata: dict[str, Any],
        runtime_version: str | None,
    ) -> dict[str, Any]:
        """Configuración efectiva de un modelo local, componente a componente.

        Todo lo de la capa B sale del runtime, así que va como `verificado`; lo
        que Ollama no devuelva queda `no_disponible` sin sustituto. La capa C no
        aplica: un runtime local no se identifica a sí mismo con un
        `system_fingerprint`, y fingir uno sería inventar."""
        return fingerprint.build({
            "provider": fingerprint.component("ollama", fingerprint.DECLARED),
            "deployment": fingerprint.component(entry.get("deployment"), fingerprint.DECLARED),
            "model": fingerprint.component(entry.get("name"), fingerprint.DECLARED),
            "digest": fingerprint.component(item.get("digest"), fingerprint.VERIFIED),
            "quantization": fingerprint.component(
                details.get("quantization_level"), fingerprint.VERIFIED,
            ),
            "parameter_size": fingerprint.component(
                details.get("parameter_size"), fingerprint.VERIFIED,
            ),
            "family": fingerprint.component(details.get("family"), fingerprint.VERIFIED),
            "tokenizer": fingerprint.component(metadata.get("tokenizer"), fingerprint.VERIFIED),
            "chat_template": fingerprint.component(
                metadata.get("chat_template"), fingerprint.VERIFIED,
            ),
            "runtime": fingerprint.component("ollama", fingerprint.DECLARED),
            "runtime_version": fingerprint.component(runtime_version, fingerprint.VERIFIED),
        })

    async def _runtime_version(self) -> str | None:
        """Versión del runtime (/api/version). Es propiedad del proveedor, no
        de cada modelo, así que se consulta y se cachea una sola vez: una
        actualización de Ollama cambia la huella de TODOS sus modelos a la vez,
        que es justo lo que hay que poder ver."""
        cached = self._version_cache.get()
        if cached is not None:
            return str(cached) or None
        try:
            response = await self.client.get("/api/version")
            response.raise_for_status()
            version = str((response.json() or {}).get("version") or "")
        except (httpx.HTTPError, TypeError, ValueError):
            return None
        self._version_cache.set(version)
        return version or None

    async def _metadata_for(self, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Metadatos de cada modelo del catálogo, con caché por digest.

        Un modelo cuya entrada cacheada sigue vigente y conserva su digest no
        se vuelve a consultar; el resto se piden en paralelo acotado para que
        refrescar un catálogo grande no encadene decenas de peticiones."""
        now = time.monotonic()
        result: dict[str, dict[str, Any]] = {}
        pending: list[tuple[str, str]] = []
        for item in items:
            name = str(item.get("name") or item.get("model") or "")
            if not name:
                continue
            digest = str(item.get("digest") or "")
            cached = self._metadata_cache.get(name)
            if cached is not None and cached[0] > now and cached[1] == digest:
                result[name] = cached[2]
            else:
                pending.append((name, digest))
        if not pending:
            return result
        gate = asyncio.Semaphore(METADATA_FETCH_CONCURRENCY)

        async def fetch(name: str, digest: str) -> None:
            async with gate:
                metadata = await self._model_metadata(name)
            result[name] = metadata
            self._metadata_cache[name] = (now + METADATA_CACHE_SECONDS, digest, metadata)

        await asyncio.gather(*(fetch(name, digest) for name, digest in pending))
        # Los modelos que ya no están en el catálogo dejan de ocupar sitio.
        live = {name for name, _ in pending} | set(result)
        self._metadata_cache = {
            name: value for name, value in self._metadata_cache.items() if name in live
        }
        return result

    def _offload_plan(self, entry: dict[str, Any]) -> OffloadPlan:
        """Reparto GPU/CPU de este modelo, o el error terminal si no lo hay.

        Es el único sitio donde se decide, para que la reserva del lease y el
        `num_gpu` que viaja a Ollama no puedan contar cosas distintas."""
        resources = self.config.resources
        size_bytes = int(entry.get("size_bytes") or 0)
        budget = local_memory_budget_bytes(self.config)
        plan = plan_offload(
            size_bytes,
            entry.get("layers"),
            budget,
            enabled=resources.gpu_layer_offload,
            reserve_ratio=resources.gpu_offload_reserve_ratio,
        )
        if plan is None:
            raise ProviderError(
                "VRAM_MODEL_TOO_LARGE",
                self.lifecycle._too_large_message(str(entry.get("name") or ""), size_bytes, budget),
            )
        if plan.split:
            logger.info(
                "ollama.layer_offload",
                extra={
                    "event": "ollama.layer_offload",
                    "model": entry.get("name"),
                    "layers_gpu": plan.layers,
                    "layers_total": plan.total_layers,
                    "gpu_bytes": plan.gpu_bytes,
                    "detail": (
                        f"{plan.layers}/{plan.total_layers} capas en GPU; el resto en CPU. "
                        "La respuesta será más lenta que con el modelo entero en GPU."
                    ),
                },
            )
        return plan

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
        """Lo que /api/show sabe de un modelo.

        Antes se leían solo `context_length` y `capabilities` y el resto del
        payload se descartaba. También trae `template` —la plantilla con la que
        se formatean los mensajes— y las claves `tokenizer.*` de `model_info`:
        dos piezas que cambian la respuesta del modelo sin tocar sus pesos, así
        que forman parte de su identidad de ejecución. Se guarda un hash de
        ambas, no el texto: interesa saber si cambiaron, y la plantilla de
        algunos modelos ocupa páginas."""
        try:
            response = await self.client.post("/api/show", json={"model": model})
            response.raise_for_status()
            payload = response.json()
            model_info = payload.get("model_info") or {}
            context_window = next(
                (int(value) for key, value in model_info.items() if key.endswith(".context_length")),
                None,
            )
            tokenizer = {
                key: value for key, value in model_info.items() if key.startswith("tokenizer.")
            }
            return {
                "context_window": context_window,
                "capabilities": payload.get("capabilities") or [],
                # Capas del modelo (`*.block_count`). Es lo que permite repartir
                # el modelo entre GPU y CPU con una cifra medida en vez de una
                # inventada (ver plan_offload).
                "layers": next(
                    (int(value) for key, value in model_info.items() if key.endswith(".block_count")),
                    None,
                ),
                "chat_template": fingerprint.digest_of(payload.get("template")),
                "tokenizer": fingerprint.digest_of(
                    json.dumps(tokenizer, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if tokenizer
                    else None
                ),
            }
        except (httpx.HTTPError, TypeError, ValueError):
            return dict(_EMPTY_METADATA)

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
        think_disabled = _thinking_disabled(
            entry.get("capabilities"), inference_request.generation.max_output_tokens
        )
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        plan = self._offload_plan(entry)
        # Contexto a medida de ESTA petición. El mismo número que decide si la
        # tarea cabe (estimate_required_context) sirve para no reservar caché KV
        # de cientos de miles de tokens que nadie va a usar.
        resources = self.config.resources
        num_ctx = (
            context_window_for(
                estimate_required_context(inference_request, _estimation_text(prompt, system)),
                entry.get("context_window"),
                minimum_tokens=resources.gpu_context_minimum_tokens,
            )
            if resources.gpu_context_sizing
            else None
        )
        try:
            async with self.lifecycle.lease(model, plan.gpu_bytes):
                payload_request: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": -1,
                    "options": _generation_options(inference_request, plan, num_ctx),
                }
                if think_disabled:
                    payload_request["think"] = False
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
            raise provider_error_from_http(error, provider="ollama", model=model) from error
        content = (payload.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise _empty_content_error(payload, think_disabled)
        return ModelOutput(
            content=content, tokens_input=int(payload.get("prompt_eval_count") or 0),
            tokens_output=int(payload.get("eval_count") or 0), cost_usd=0.0,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            generation=effective_generation(inference_request),
        )

    async def chat_tools(
        self,
        request: TaskCreateRequest,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AgentTurn:
        """Una ronda de /api/chat con tools (soporte nativo de Ollama). A
        diferencia de OpenAI, Ollama devuelve message.tool_calls con arguments
        ya como objeto y mantiene el modelo cargado (lease de VRAM)."""
        catalog = await self.models()
        entry = next((item for item in catalog if item["name"] == model), None)
        if entry is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo Ollama no disponible: {model}")
        started = datetime.now(timezone.utc)
        plan = self._offload_plan(entry)
        payload_request: dict[str, Any] = {
            "model": model,
            "messages": self._to_ollama_messages(messages),
            "stream": False,
            "keep_alive": -1,
            "options": _generation_options(request, plan),
        }
        # La clave `tools` se omite cuando no hay ninguna, para que el turno de
        # cierre del bucle agéntico sea una generación normal y no quede la
        # plantilla de tools en el contexto invitando a llamarlas.
        if tools:
            payload_request["tools"] = tools
        if _thinking_disabled(entry.get("capabilities"), request.generation.max_output_tokens):
            payload_request["think"] = False
        try:
            async with self.lifecycle.lease(model, plan.gpu_bytes):
                response = await self.client.post("/api/chat", json=payload_request)
                response.raise_for_status()
                payload = response.json()
        except ProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ProviderError("PROVIDER_UNAVAILABLE", str(error), retryable=True) from error
        except httpx.HTTPStatusError as error:
            raise provider_error_from_http(error, provider="ollama", model=model) from error
        message = payload.get("message") or {}
        tool_calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            arguments = function.get("arguments")
            tool_calls.append(ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            ))
        content = message.get("content")
        # Ollama devuelve el mensaje del asistente en su propio formato; se
        # reenvía tal cual porque _to_ollama_messages ya normaliza en la vuelta.
        return AgentTurn(
            content=content if isinstance(content, str) and content.strip() else None,
            tool_calls=tuple(tool_calls),
            tokens_input=int(payload.get("prompt_eval_count") or 0),
            tokens_output=int(payload.get("eval_count") or 0),
            cost_usd=0.0,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            raw_assistant_message={"role": "assistant", **message},
            generation=effective_generation(request),
        )

    @staticmethod
    def _to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """El historial se guarda en formato OpenAI; Ollama usa role=tool con
        el contenido directo (sin tool_call_id) y el asistente con tool_calls."""
        converted: list[dict[str, Any]] = []
        for item in messages:
            if item.get("role") == "tool":
                converted.append({"role": "tool", "content": item.get("content") or ""})
            else:
                converted.append(item)
        return converted

    async def embed(self, request: TaskCreateRequest, model: str, input_text: str) -> ModelOutput:
        catalog = await self.models()
        entry = next((item for item in catalog if item["name"] == model), None)
        if entry is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo Ollama no disponible: {model}")
        if "embedding" not in set(entry.get("capabilities") or []):
            raise ProviderError("MODEL_CAPABILITY_MISMATCH", f"El modelo {model} no declara capacidad embedding")
        enforce_context_limit(request, entry.get("context_window"), input_text)
        started = datetime.now(timezone.utc)
        # /api/embed no acepta `options`, así que aquí el plan solo sirve para
        # reservar lo que de verdad ocupa. Un modelo de embeddings que no cupiera
        # sigue siendo terminal, como siempre.
        plan = self._offload_plan(entry)
        try:
            async with self.lifecycle.lease(model, plan.gpu_bytes):
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
            raise provider_error_from_http(error, provider="ollama", model=model) from error
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
            # /api/embed no admite `options`: una semilla pedida aquí no viaja a
            # ningún sitio y la telemetría lo dice en vez de callarlo.
            generation=effective_generation(request, applies=False),
        )
