"""Provider interno `larm`: sirve variantes gestionadas por LARM con lease.

No es una ruta alternativa para las aplicaciones: ellas siguen hablando con el
broker, y el broker solo llega aquí con un target exacto
`{provider: "larm", deployment: "local", model: <variant_id>}`. Cada invocación
adquiere su lease, habla el protocolo que declara el endpoint, renueva mientras
dura y libera al terminar, pase lo que pase (app.larm).

La admisión de memoria y contexto es de LARM: el catálogo no publica ventana
de contexto y el broker no la inventa. La petición viaja con sus
`resource_requirements` y LARM decide; su negativa vuelve con el código que
el broker ya sabe tratar (VRAM_INSUFFICIENT → waiting_for_memory, etc.).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import httpx

from app import execution_fingerprint as fingerprint
from app import invocation_context
from app.config import BrokerConfig, OpenAICompatibleModelConfig, OpenAICompatibleProviderConfig
from app.larm import (
    CAPABILITY_MAP,
    PROVIDER_ID,
    SUPPORTED_PROTOCOLS,
    UUID7_PATTERN,
    ActiveLease,
    LarmError,
    LarmLeaseJournal,
    LeaseResourceRequirements,
    LocalRuntimeResolver,
    effective_backend,
    fingerprint_parts,
    lease_idempotency_key,
)
from app.providers.base import ModelOutput, ProviderError, estimate_input_tokens, estimate_required_context
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas import InferenceKind, TaskCreateRequest

logger = logging.getLogger("ai_broker.providers.larm")

# El contexto lo admite LARM, no el broker. El adapter OpenAI recorta la
# salida para que quepa en la ventana que se le diga; con una ventana que no
# limita, la petición viaja tal cual la pidió el cliente y quien decide si
# cabe es la admisión de LARM con los resource_requirements.
_UNBOUNDED_CONTEXT = 1_000_000_000
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_MEMORY_CODES = {"RESOURCE_UNAVAILABLE", "LOAD_OOM"}
_MISMATCH_CODES = {
    "INCOMPATIBLE", "UNSUPPORTED_CAPABILITY", "RUNTIME_INCOMPATIBLE", "CONTRACT_UNSUPPORTED",
    "BACKEND_MISMATCH", "PROTOCOL_UNSUPPORTED", "CAPABILITY_UNAVAILABLE",
}
_BOUNDARY_CODES = {"ENDPOINT_NOT_LOCAL"}
_UNAVAILABLE_MODEL_CODES = {
    "NOT_FOUND", "MODEL_NOT_FOUND", "MODEL_OFFLINE", "MODEL_CORRUPT", "RUNTIME_NOT_INSTALLED",
    "LICENSE_REQUIRED", "APPROVAL_REQUIRED", "INTEGRITY_FAILED", "UNKNOWN_SIZE",
}
_AUTH_CODES = {"AUTH_REQUIRED", "FORBIDDEN", "CREDENTIALS_MISSING", "AUTH_BACKEND_UNAVAILABLE"}
_CONTRACT_CODES = {"VALIDATION_FAILED", "IDEMPOTENCY_CONFLICT", "INVALID_RESPONSE", "INVALID_STATE_TRANSITION"}
_TRANSIENT_CODES = {
    "TIMEOUT", "UPSTREAM_UNAVAILABLE", "ADAPTER_CRASHED", "RUNTIME_CRASH", "BACKEND_UNAVAILABLE",
    "LEASE_ACQUIRE_TIMEOUT", "PROTOCOL_ERROR",
}


def provider_error(error: LarmError, variant_id: str) -> ProviderError:
    """Traduce un error LARM al vocabulario del broker (contrato LARM 06).

    El error original viaja siempre en `details.larm_error`: la traducción
    ayuda a decidir qué hacer, no sustituye a la evidencia."""
    details = {"provider": PROVIDER_ID, "larm_error": error.original()}
    identity = f"larm/local/{variant_id}"
    if error.code in _MEMORY_CODES:
        if error.retryable:
            # No cabe AHORA: la tarea espera memoria conservando su turno.
            result = ProviderError(
                "VRAM_INSUFFICIENT",
                f"LARM no puede admitir {identity} ahora: {error}",
                retryable=True, details=details,
            )
            result.memory_block = {  # type: ignore[attr-defined]
                "reason": "larm_admission",
                "model": variant_id,
                "larm_reason": error.reason,
                "evidence_refs": error.evidence_refs[:20],
                "message": str(error),
            }
            return result
        return ProviderError(
            "VRAM_MODEL_TOO_LARGE",
            f"{identity} no cabe en el presupuesto de memoria de LARM: {error}",
            details=details,
        )
    if error.code in _BOUNDARY_CODES:
        return ProviderError("CLOUD_NOT_ALLOWED", f"{identity}: {error}", details=details)
    if error.code in _MISMATCH_CODES:
        return ProviderError("MODEL_CAPABILITY_MISMATCH", f"{identity}: {error}", details=details)
    if error.code in _UNAVAILABLE_MODEL_CODES:
        return ProviderError(
            "MODEL_UNAVAILABLE", f"{identity} no disponible en LARM: {error}",
            retryable=error.retryable, details=details,
        )
    if error.code in _AUTH_CODES:
        return ProviderError(
            "CREDENTIALS_UNAVAILABLE", f"LARM rechazó la credencial del broker: {error}",
            retryable=error.code == "AUTH_BACKEND_UNAVAILABLE", details=details,
        )
    if error.code in _CONTRACT_CODES:
        return ProviderError("INVALID_PROVIDER_RESPONSE", f"LARM: {error}", details=details)
    return ProviderError(
        "PROVIDER_UNAVAILABLE", f"LARM: {error}",
        retryable=error.code in _TRANSIENT_CODES or error.retryable, details=details,
    )


class _LeaseEndpointAdapter(OpenAICompatibleProvider):
    """Adapter OpenAI contra el endpoint de una lease, con el token de esa
    lease y ninguno más: nunca consulta variables ni keyring."""

    def __init__(self, config: OpenAICompatibleProviderConfig, token: str | None,
                 transport: httpx.AsyncBaseTransport | None) -> None:
        super().__init__(config, transport=transport)
        self._lease_token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._lease_token}"} if self._lease_token else {}


class LarmProvider:
    def __init__(
        self,
        config: BrokerConfig,
        *,
        resolver: LocalRuntimeResolver | None = None,
        journal: LarmLeaseJournal | None = None,
        endpoint_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.resolver = resolver or LocalRuntimeResolver(config.larm, journal=journal)
        self._endpoint_transport = endpoint_transport

    @property
    def enabled(self) -> bool:
        return self.config.larm.enabled

    async def close(self) -> None:
        await self.resolver.close()

    async def reload_config(self, config: BrokerConfig) -> LarmProvider:
        """Un cambio del bloque `larm` reconstruye el resolver; uno ajeno no."""
        if config.larm == self.config.larm:
            self.config = config
            return self
        replacement = LarmProvider(
            config, journal=self.resolver.journal, endpoint_transport=self._endpoint_transport,
        )
        await self.close()
        return replacement

    def capabilities(self) -> dict[str, Any]:
        if not self.enabled:
            return {"local_runtime_resolver": False, "larm_contract_version": None}
        return self.resolver.get_capabilities()

    async def models(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        catalog = await self.resolver.refresh_catalog()
        if catalog is None:
            return []
        return [
            self._catalog_entry(item)
            for item in catalog["items"]
            if isinstance(item, dict) and UUID7_PATTERN.match(str(item.get("variant_id") or ""))
        ]

    @staticmethod
    def _speaks_supported_protocol(item: dict[str, Any]) -> bool:
        return any(
            isinstance(profile, dict) and str(profile.get("protocol") or "").lower() in SUPPORTED_PROTOCOLS
            for profile in item.get("protocol_profiles") or []
        )

    def _catalog_entry(self, item: dict[str, Any]) -> dict[str, Any]:
        variant_id = str(item["variant_id"])
        larm_capabilities = [str(cap) for cap in item.get("capabilities") or []]
        capabilities: list[str] = []
        if self._speaks_supported_protocol(item):
            capabilities = sorted({CAPABILITY_MAP[cap] for cap in larm_capabilities if cap in CAPABILITY_MAP})
        mapped, extension = fingerprint_parts(item.get("fingerprint"), variant_id=variant_id, source="catalog")
        return {
            "name": variant_id,
            "provider": PROVIDER_ID,
            "deployment": "local",
            "status": "online",
            # Sin dato no hay ventana: la admisión de contexto la hace LARM.
            "context_window": None,
            "context_window_source": "larm_admission",
            "context_admission": PROVIDER_ID,
            "capabilities": capabilities,
            "larm_capabilities": larm_capabilities,
            "family": "larm",
            "compatibility": "unknown",
            "features": {"vision": True} if "vision" in capabilities else {},
            "execution_fingerprint": fingerprint.build(
                {
                    "provider": fingerprint.component(PROVIDER_ID, fingerprint.DECLARED),
                    "deployment": fingerprint.component("local", fingerprint.DECLARED),
                    "model": fingerprint.component(variant_id, fingerprint.DECLARED),
                    **mapped,
                },
                extension,
            ),
        }

    async def health(self) -> dict[str, Any]:
        started = asyncio.get_running_loop().time()
        catalog = await self.resolver.refresh_catalog(force=True)
        latency_ms = (asyncio.get_running_loop().time() - started) * 1000
        error = self.resolver.last_error
        if catalog is None or error is not None:
            # La salud es la del último intento: un catálogo conservado tras un
            # parpadeo sigue sirviendo, pero LARM no está respondiendo ahora.
            return {
                "status": "unavailable",
                "detail": f"{error.code if error else 'UNKNOWN'}: LARM no publica catálogo",
                "latency_ms": latency_ms,
            }
        count = len(catalog["items"])
        return {
            "status": "healthy" if count else "degraded",
            "detail": f"{count} variantes BROKER_ELIGIBLE (contrato {catalog['contract_version']})",
            "latency_ms": latency_ms,
        }

    async def generate(
        self, request: TaskCreateRequest, model: str, prompt: str, system: str | None = None,
    ) -> ModelOutput:
        return await self._serve(request, model, prompt, system)

    async def embed(self, request: TaskCreateRequest, model: str, input_text: str) -> ModelOutput:
        return await self._serve(request, model, input_text, None)

    def _requirements(self, request: TaskCreateRequest, prompt: str) -> LeaseResourceRequirements:
        if request.inference_kind == InferenceKind.embedding:
            return LeaseResourceRequirements(
                capability="embedding.create",
                context_tokens=estimate_input_tokens(request, prompt),
                max_output_tokens=None,
            )
        capability = "vision.understand" if request.inline_images else "text.generate"
        return LeaseResourceRequirements(
            capability=capability,
            context_tokens=estimate_required_context(request, prompt),
            max_output_tokens=request.generation.max_output_tokens,
        )

    async def _resolve(self, variant_id: str, capability: str) -> dict[str, Any]:
        if not self.enabled:
            raise ProviderError("PROVIDER_UNAVAILABLE", "El puente LARM está deshabilitado (larm.enabled=false)")
        if not UUID7_PATTERN.match(variant_id):
            # Un alias o un nombre no es identidad en LARM: se resuelve antes
            # de pedir, nunca aquí por aproximación.
            raise ProviderError(
                "MODEL_UNAVAILABLE",
                f"larm solo admite variant_id UUIDv7 como modelo exacto; {variant_id!r} no lo es",
            )
        entry = await self.resolver.resolve_model(variant_id)
        if entry is None:
            if self.resolver.last_error is not None:
                raise provider_error(self.resolver.last_error, variant_id)
            raise ProviderError("MODEL_UNAVAILABLE", f"LARM no publica la variante {variant_id} como BROKER_ELIGIBLE")
        if capability not in {str(cap) for cap in entry.get("capabilities") or []}:
            raise ProviderError(
                "MODEL_CAPABILITY_MISMATCH",
                f"La variante {variant_id} no anuncia la capacidad {capability} en LARM",
            )
        if not self._speaks_supported_protocol(entry):
            raise ProviderError(
                "MODEL_CAPABILITY_MISMATCH",
                f"La variante {variant_id} no declara ningún protocolo que el broker hable "
                f"({', '.join(sorted(SUPPORTED_PROTOCOLS))})",
            )
        return entry

    @staticmethod
    def _check_effective(entry: dict[str, Any], lease: ActiveLease, fallback_allowed: bool) -> None:
        """Lo servido tiene que ser lo pedido: backend y huella exactos.

        Un backend distinto del anunciado (GPU pedida, CPU servida) solo se
        acepta si LARM lo permitía en el catálogo Y el cliente no prohibió el
        fallback. No es un detalle: cambia latencia y, a veces, resultados."""
        if lease.protocol.lower() not in SUPPORTED_PROTOCOLS:
            raise LarmError("PROTOCOL_UNSUPPORTED", f"El endpoint de la lease habla {lease.protocol!r}")
        host = (urlparse(lease.base_url).hostname or "").lower()
        if host not in _LOOPBACK_HOSTS:
            # El broker trata `larm` como local en la frontera de datos; un
            # endpoint fuera de esta máquina rompería esa promesa. Se rechaza
            # antes de enviar el prompt, sea cual sea la clasificación.
            raise LarmError(
                "ENDPOINT_NOT_LOCAL",
                f"LARM entregó un endpoint fuera de loopback ({host or 'sin host'}); "
                "el broker no envía datos fuera de esta máquina por el puente LARM",
            )
        constraints = entry.get("constraints") or {}
        backend_fallback = bool(constraints.get("allow_backend_fallback")) and fallback_allowed
        if backend_fallback:
            return
        expected_backend = effective_backend(entry.get("fingerprint"))
        served_backend = effective_backend(lease.execution_fingerprint)
        if expected_backend is not None and served_backend is not None and expected_backend != served_backend:
            raise LarmError(
                "BACKEND_MISMATCH",
                f"LARM sirvió con backend {served_backend} y el catálogo anunciaba {expected_backend}",
            )
        expected_hash = constraints.get("execution_fingerprint")
        served_hash = lease.execution_fingerprint.get("hash")
        if expected_hash and served_hash and expected_hash != served_hash:
            raise LarmError(
                "BACKEND_MISMATCH",
                "La huella de ejecución de la lease no es la exigida por el catálogo",
            )

    def _endpoint_adapter(self, base_url: str, token: str | None, variant_id: str, capability: str) -> _LeaseEndpointAdapter:
        broker_capability = "embedding" if capability == "embedding.create" else "completion"
        endpoint_config = OpenAICompatibleProviderConfig(
            id=PROVIDER_ID,
            enabled=True,
            base_url=base_url,
            timeout_seconds=self.config.larm.inference_timeout_seconds,
            api_key_env=None,
            deployment="local",
            sync_models=False,
            probe_features=False,
            models=[
                OpenAICompatibleModelConfig(
                    name=variant_id,
                    context_window=_UNBOUNDED_CONTEXT,
                    capabilities=[broker_capability] + (["vision"] if capability == "vision.understand" else []),
                ),
            ],
        )
        return _LeaseEndpointAdapter(endpoint_config, token, self._endpoint_transport)

    async def _serve(
        self, request: TaskCreateRequest, variant_id: str, prompt: str, system: str | None,
    ) -> ModelOutput:
        requirements = self._requirements(request, prompt)
        entry = await self._resolve(variant_id, requirements.capability)
        context = invocation_context.current()
        key = lease_idempotency_key(
            context.task_id if context else None, context.invocation_id if context else None,
        )
        fallback_allowed = request.model_requirements.fallback_allowed
        try:
            async with self.resolver.leased(
                entry, requirements,
                idempotency_key=key,
                task_id=context.task_id if context else None,
                invocation_id=context.invocation_id if context else None,
                fallback_allowed=fallback_allowed,
            ) as lease:
                self._check_effective(entry, lease, fallback_allowed)
                base_url, token = await self.resolver.get_endpoint(lease)
                output = await self._infer_under_lease(
                    lease, self._endpoint_adapter(base_url, token, variant_id, requirements.capability),
                    request, variant_id, prompt, system,
                )
        except LarmError as error:
            raise provider_error(error, variant_id) from error
        except TimeoutError as error:
            raise provider_error(
                LarmError("TIMEOUT", "El canal de credenciales de LARM no respondió a tiempo", retryable=True),
                variant_id,
            ) from error
        mapped, extension = fingerprint_parts(lease.execution_fingerprint, variant_id=variant_id, source="lease")
        observed = dict(output.fingerprint_observed or {})
        observed.update(mapped)
        logger.info("larm.served", extra={"event": "larm.served", **lease.summary()})
        return replace(output, fingerprint_observed=observed or None, fingerprint_extensions=extension)

    @staticmethod
    async def _infer_under_lease(
        lease: ActiveLease,
        adapter: _LeaseEndpointAdapter,
        request: TaskCreateRequest,
        variant_id: str,
        prompt: str,
        system: str | None,
    ) -> ModelOutput:
        """La inferencia corre mientras la lease siga viva; si se pierde, se corta."""
        if request.inference_kind == InferenceKind.embedding:
            call = adapter.embed(request, variant_id, prompt)
        else:
            call = adapter.generate(request, variant_id, prompt, system=system)
        infer = asyncio.ensure_future(call)
        lost = asyncio.ensure_future(lease.lost.wait())
        try:
            done, _ = await asyncio.wait({infer, lost}, return_when=asyncio.FIRST_COMPLETED)
            if infer in done:
                return infer.result()
            raise ProviderError(
                "PROVIDER_UNAVAILABLE",
                f"Se perdió la lease LARM {lease.lease_id} durante la inferencia: {lease.lost_reason}",
                retryable=True,
                details={"provider": PROVIDER_ID, "larm_error": {"code": "LEASE_LOST", "reason": lease.lost_reason}},
            )
        finally:
            for pending in (infer, lost):
                if not pending.done():
                    pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(infer, lost, return_exceptions=True)
            await adapter.close()
