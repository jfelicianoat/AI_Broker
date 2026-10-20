"""LocalRuntimeResolver: el puente del broker hacia LARM.

LARM (Local AI Runtime Manager) administra la capacidad local del PC IA: qué
variantes de modelo existen, con qué runtime y backend se pueden servir y si
hay memoria para cargarlas. El broker sigue siendo el gateway de inferencia y
quien decide el routing de negocio (spec LARM §62); LARM solo hace la
admisión técnica. Este módulo materializa el contrato Broker–LARM 1.0
(contratos LARM 04 y 06, operaciones P100/P101 de api-operations.json):

- catálogo: GET /api/v1/broker/catalog, solo variantes BROKER_ELIGIBLE;
- lease: POST /api/v1/leases (201 ACTIVE, o 202 + GET /jobs/{id}), renovación
  según `renew_after_seconds` con If-Match + Idempotency-Key, y liberación
  idempotente al terminar pase lo que pase;
- credencial de endpoint: solo por el named pipe local de LARM, en memoria;
- diario `larm_leases`: cada lease adquirida queda escrita ANTES de usarla,
  para liberarla al arrancar si el broker murió con ella en el aire.

Lo que este módulo NO hace: llamar al modelo (lo hace el adapter del
protocolo declarado, ver app.providers.larm) ni decidir qué modelo usar.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from app.config import LarmConfig
from app.db import LARM_LEASES_DDL, Database, dumps_json, loads_json

logger = logging.getLogger("ai_broker.larm")

LARM_CONTRACT_VERSION = "1.0"
# Clave versionada de la extensión de huella (contrato LARM 05: "extras
# técnicos en extensión versionada, no inyectarlos en esquema Broker actual").
FINGERPRINT_EXTENSION_KEY = "larm.v1"
PROVIDER_ID = "larm"
UUID7_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# Idempotency-Key de LARM: 1..240 caracteres (contrato 07).
_IDEMPOTENCY_KEY_MAX = 240
_TERMINAL_JOB_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
# Mensaje máximo del canal de credenciales (contrato 04).
_PIPE_MESSAGE_MAX_BYTES = 4096
# original_error se recorta a lo que admite el contrato 07.
_ORIGINAL_ERROR_MAX_CHARS = 4096

# Capacidades LARM → capacidades del catálogo del broker. Solo las que la vía
# /tasks del contrato 2.11 sabe servir: chat y embeddings. El resto
# (imagen, audio, decision.evaluate…) llega con la extensión de
# capability-tasks, no se degrada a chat para fingir cobertura.
CAPABILITY_MAP = {
    "text.generate": "completion",
    "embedding.create": "embedding",
    "vision.understand": "vision",
}
# Protocolos que el broker sabe hablar contra el endpoint de una lease.
SUPPORTED_PROTOCOLS = {"openai"}

# Componentes de la huella LARM que tienen equivalente exacto en la huella
# del broker (app.execution_fingerprint). El resto viaja en la extensión.
_COMPONENT_MAP = {
    "artifact_bundle_hash": "digest",
    "tokenizer_hash": "tokenizer",
    "chat_template_hash": "chat_template",
    "runtime_id": "runtime",
    "runtime_version": "runtime_version",
}
_STATUS_MAP = {"VERIFIED": "verificado", "DECLARED": "declarado", "UNAVAILABLE": "no_disponible"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LarmError(Exception):
    """Fallo de una llamada a LARM, con el error original conservado.

    `code` es el código LARM (contrato 07) o uno de transporte del cliente
    (`UPSTREAM_UNAVAILABLE`, `INVALID_RESPONSE`, `CREDENTIALS_MISSING`).
    `original` es lo que se publica hacia el cliente del broker: sin rutas ni
    secretos, recortado a 4096 caracteres."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        correlation_id: str | None = None,
        reason: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.correlation_id = correlation_id
        self.reason = reason
        self.evidence_refs = list(evidence_refs or [])

    def original(self) -> dict[str, Any]:
        return {
            "system": "larm",
            "code": self.code,
            "message": str(self)[:_ORIGINAL_ERROR_MAX_CHARS],
            "http_status": self.status,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
            "reason": self.reason,
            "evidence_refs": self.evidence_refs[:20],
        }


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _require_keys(payload: Any, keys: tuple[str, ...], what: str) -> dict[str, Any]:
    """Núcleo obligatorio de una respuesta externa: se toleran campos de más,
    no campos de menos (contrato 00)."""
    if not isinstance(payload, dict):
        raise LarmError("INVALID_RESPONSE", f"LARM devolvió {what} que no es un objeto JSON")
    missing = [key for key in keys if key not in payload]
    if missing:
        raise LarmError("INVALID_RESPONSE", f"LARM devolvió {what} sin {', '.join(missing)}")
    return payload


_LEASE_RESPONSE_KEYS = (
    "id", "owner_id", "instance_id", "generation", "endpoint_id", "endpoint",
    "execution_fingerprint", "expires_at", "renew_after_seconds",
)
_RUNTIME_LEASE_KEYS = (
    "id", "row_version", "owner_id", "request_id", "generation", "state", "expires_at",
)


class LarmClient:
    """Cliente HTTP de la API administrativa de LARM para el principal broker.

    El token se cachea en memoria y se vuelve a leer ante un 401: así una
    rotación de la credencial no exige reiniciar el broker, y un token que
    sigue siendo el mismo no provoca un bucle de reintentos."""

    def __init__(self, config: LarmConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(config.timeout_seconds, connect=config.connect_timeout_seconds),
            transport=transport,
        )
        self._token: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    def _resolve_token(self, *, refresh: bool = False) -> str:
        # Import diferido: app.providers importa este módulo al montar el
        # provider `larm`, y la dependencia inversa cerraría un ciclo.
        from app.providers.base import CredentialResolver

        if self._token is None or refresh:
            self._token = CredentialResolver.get(self.config)
        if not self._token:
            raise LarmError(
                "CREDENTIALS_MISSING",
                "No hay credencial del broker para LARM: ni la variable "
                f"{self.config.api_key_env or '(sin variable)'} ni la entrada "
                f"{self.config.keyring_service}/{self.config.keyring_username} del "
                "Credential Manager",
            )
        return self._token

    def bearer_token(self) -> str:
        return self._resolve_token()

    async def request(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token = self._resolve_token()
        for attempt in range(2):
            request_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
            try:
                response = await self.client.request(
                    method, path, json=json_body, headers=request_headers, params=params,
                )
            except httpx.TimeoutException as error:
                raise LarmError(
                    "TIMEOUT", f"LARM no respondió a {method} {path}: {type(error).__name__}",
                    retryable=True,
                ) from error
            except httpx.HTTPError as error:
                raise LarmError(
                    "UPSTREAM_UNAVAILABLE",
                    f"LARM no está disponible en {self.config.endpoint}: {type(error).__name__}",
                    retryable=True,
                ) from error
            if response.status_code == 401 and attempt == 0:
                # Rotación del token: se relee una vez. Si sigue siendo el
                # mismo, no hay nada que reintentar.
                previous = token
                token = self._resolve_token(refresh=True)
                if token != previous:
                    continue
            if response.status_code in expected:
                return response
            raise self._error_from(response, method, path)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _error_from(response: httpx.Response, method: str, path: str) -> LarmError:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("code"), str):
            details = _as_dict(body.get("details"))
            return LarmError(
                body["code"],
                str(body.get("message") or body["code"]),
                status=status,
                retryable=bool(body.get("retryable", False)),
                correlation_id=body.get("correlation_id"),
                reason=details.get("reason"),
                evidence_refs=[str(item) for item in details.get("evidence_refs") or []],
            )
        excerpt = " ".join((response.text or "").split())[:200]
        return LarmError(
            "UPSTREAM_UNAVAILABLE" if status >= 500 else "INVALID_RESPONSE",
            f"LARM respondió {status} a {method} {path} sin error contractual: {excerpt or '(vacío)'}",
            status=status,
            retryable=status >= 500,
        )

    async def catalog(self) -> dict[str, Any]:
        response = await self.request("GET", "/api/v1/broker/catalog", expected={200})
        payload = _require_keys(_json(response), ("contract_version", "items", "snapshot_at"), "un catálogo")
        if payload["contract_version"] != LARM_CONTRACT_VERSION:
            raise LarmError(
                "CONTRACT_UNSUPPORTED",
                f"LARM habla el contrato {payload['contract_version']!r}; "
                f"este broker solo conoce {LARM_CONTRACT_VERSION!r}",
            )
        if not isinstance(payload["items"], list):
            raise LarmError("INVALID_RESPONSE", "El catálogo de LARM no trae una lista de items")
        return payload

    async def launch_profiles(self) -> list[dict[str, Any]]:
        response = await self.request("GET", "/api/v1/launch-profiles", expected={200})
        payload = _require_keys(_json(response), ("items",), "una lista de perfiles")
        return [item for item in payload["items"] if isinstance(item, dict)]

    async def create_lease(self, body: dict[str, Any], idempotency_key: str) -> tuple[int, dict[str, Any]]:
        response = await self.request(
            "POST", "/api/v1/leases", expected={200, 201, 202},
            json_body=body, headers={"Idempotency-Key": idempotency_key},
        )
        return response.status_code, _json(response)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        response = await self.request("GET", f"/api/v1/jobs/{job_id}", expected={200})
        return _require_keys(_json(response), ("job_id", "state", "result", "error"), "un job")

    async def get_lease(self, lease_id: str) -> dict[str, Any]:
        response = await self.request("GET", f"/api/v1/leases/{lease_id}", expected={200})
        return _require_keys(_json(response), _RUNTIME_LEASE_KEYS, "una lease")

    async def list_leases(self) -> list[dict[str, Any]]:
        response = await self.request("GET", "/api/v1/leases", expected={200})
        payload = _require_keys(_json(response), ("items",), "una lista de leases")
        return [item for item in payload["items"] if isinstance(item, dict)]

    async def renew_lease(
        self, lease_id: str, *, row_version: int, generation: int, ttl_seconds: int, idempotency_key: str,
    ) -> dict[str, Any]:
        response = await self.request(
            "POST", f"/api/v1/leases/{lease_id}/renew", expected={200, 201},
            json_body={"generation": generation, "ttl_seconds": ttl_seconds},
            headers={"If-Match": str(row_version), "Idempotency-Key": idempotency_key},
        )
        return _require_keys(_json(response), _RUNTIME_LEASE_KEYS, "una lease renovada")

    async def release_lease(self, lease_id: str, *, row_version: int) -> None:
        await self.request(
            "DELETE", f"/api/v1/leases/{lease_id}", expected={204},
            headers={"If-Match": str(row_version)},
        )


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise LarmError(
            "INVALID_RESPONSE",
            f"LARM respondió {response.status_code} sin JSON en {response.url.path}",
        ) from error


# --- Canal de credenciales de endpoint (contrato LARM 04) --------------------


def current_user_sid() -> str | None:
    """SID textual del usuario del proceso, o None fuera de Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Firmas explícitas: sin ellas ctypes pasa los HANDLE como int de 32 bits
    # y el pseudo-handle del proceso (-1) llega truncado en 64 bits.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    token_query = 0x0008
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        token_user = 1
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, token_user, buffer, size, ctypes.byref(size)):
            return None
        # TOKEN_USER empieza por SID_AND_ATTRIBUTES, cuyo primer campo es el PSID.
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def default_credentials_pipe(sid: str | None) -> str | None:
    if not sid:
        return None
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\LARM-{digest}-credentials"


def _read_line(handle: Any) -> bytes:
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = handle.read(1)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > _PIPE_MESSAGE_MAX_BYTES:
            raise LarmError("INVALID_RESPONSE", "El canal de credenciales devolvió un mensaje de más de 4 KiB")
    return bytes(data)


def fetch_endpoint_token_blocking(
    pipe_path: str,
    *,
    lease_id: str,
    generation: int,
    credential_reference_id: str,
    bearer_token: str,
) -> dict[str, Any]:
    """Pide el token de endpoint de una lease por el pipe local de LARM.

    Handshake del contrato 04: el servidor abre con {nonce, expires_at}; la
    petición devuelve ese nonce de un solo uso y el Bearer del principal. El
    token resultante vive solo en memoria del broker: no se registra, no se
    persiste y no viaja en ningún error."""
    request_id = str(uuid4())
    try:
        handle = open(pipe_path, "r+b", buffering=0)  # noqa: SIM115 - pipe con ciclo explícito
    except OSError as error:
        raise LarmError(
            "UPSTREAM_UNAVAILABLE",
            f"No se pudo abrir el canal de credenciales de LARM: {type(error).__name__}",
            retryable=True,
        ) from error
    with handle:
        hello = _decode_pipe_message(_read_line(handle), "saludo")
        nonce = hello.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise LarmError("INVALID_RESPONSE", "El canal de credenciales no envió nonce")
        message = json.dumps({
            "request_id": request_id,
            "lease_id": lease_id,
            "generation": generation,
            "credential_reference_id": credential_reference_id,
            "bearer_token": bearer_token,
            "nonce": nonce,
        }, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(message) > _PIPE_MESSAGE_MAX_BYTES:
            raise LarmError("VALIDATION_FAILED", "La petición de credencial supera 4 KiB")
        handle.write(message)
        reply = _decode_pipe_message(_read_line(handle), "respuesta")
    if isinstance(reply.get("code"), str):
        raise LarmError(
            reply["code"], str(reply.get("message") or reply["code"]),
            retryable=bool(reply.get("retryable", False)),
        )
    if reply.get("request_id") != request_id or not isinstance(reply.get("token"), str) or not reply["token"]:
        raise LarmError("INVALID_RESPONSE", "El canal de credenciales devolvió una respuesta que no es la pedida")
    return {"token": reply["token"], "expires_at": reply.get("expires_at")}


def _decode_pipe_message(raw: bytes, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise LarmError("INVALID_RESPONSE", f"El canal de credenciales envió un {what} ilegible") from error
    if not isinstance(payload, dict):
        raise LarmError("INVALID_RESPONSE", f"El canal de credenciales envió un {what} que no es objeto")
    return payload


# --- Diario de leases ---------------------------------------------------------


class LarmLeaseJournal:
    """Registro durable de las leases que el broker tiene o pudo tener.

    La fila se escribe ANTES de usar la lease y se cierra al liberarla. Si el
    proceso muere en medio, al arrancar quedan filas abiertas: se liberan en
    LARM (DELETE idempotente) en vez de dejar memoria reservada hasta que
    caduque. La clave es la invocación: un reintento de la MISMA invocación
    reutiliza su Idempotency-Key y LARM devuelve la misma lease, nunca una
    segunda."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def ensure_schema(self) -> None:
        for statement in LARM_LEASES_DDL:
            self.db.execute(statement)

    def requested(self, key: str, task_id: str | None, invocation_id: str | None, variant_id: str) -> None:
        now = utc_now_iso()
        self.db.execute(
            "INSERT INTO larm_leases (idempotency_key, task_id, invocation_id, variant_id, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'requested', ?, ?) "
            "ON CONFLICT(idempotency_key) DO UPDATE SET state = 'requested', updated_at = excluded.updated_at",
            (key, task_id, invocation_id, variant_id, now, now),
        )

    def update(self, key: str, *, state: str, lease: dict[str, Any] | None = None,
               detail: dict[str, Any] | None = None) -> None:
        assignments = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state, utc_now_iso()]
        if lease is not None:
            assignments += ["lease_id = ?", "generation = ?", "row_version = ?"]
            params += [lease.get("id"), lease.get("generation"), lease.get("row_version")]
        if detail is not None:
            assignments.append("detail_json = ?")
            params.append(dumps_json(detail))
        params.append(key)
        self.db.execute(
            f"UPDATE larm_leases SET {', '.join(assignments)} WHERE idempotency_key = ?", params,
        )

    def open_leases(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM larm_leases WHERE state NOT IN ('released', 'failed') ORDER BY created_at",
        )
        return [dict(row) | {"detail": loads_json(row["detail_json"], None)} for row in rows]

    def by_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM larm_leases WHERE invocation_id = ? ORDER BY created_at DESC LIMIT 1",
            (invocation_id,),
        )
        return dict(row) if row is not None else None


# --- Lease activa ---------------------------------------------------------------


@dataclass
class ActiveLease:
    idempotency_key: str
    variant_id: str
    lease_id: str
    owner_id: str
    instance_id: str
    generation: int
    row_version: int
    endpoint_id: str
    base_url: str
    protocol: str
    credential_reference: str | None
    execution_fingerprint: dict[str, Any]
    expires_at: str
    renew_after_seconds: int
    endpoint_token: str | None = field(default=None, repr=False)
    renewals: int = 0
    lost_reason: str | None = None
    lost: asyncio.Event = field(default_factory=asyncio.Event)

    def summary(self) -> dict[str, Any]:
        """Lo que se puede publicar de la lease: sin token ni rutas."""
        return {
            "lease_id": self.lease_id,
            "variant_id": self.variant_id,
            "instance_id": self.instance_id,
            "generation": self.generation,
            "endpoint_id": self.endpoint_id,
            "protocol": self.protocol,
            "execution_fingerprint_hash": self.execution_fingerprint.get("hash"),
            "renewals": self.renewals,
        }


@dataclass(frozen=True)
class LeaseResourceRequirements:
    capability: str
    context_tokens: int | None
    max_output_tokens: int | None

    def as_contract(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "batch": 1,
            "width": None,
            "height": None,
            "frames": None,
            "sample_rate": None,
            "audio_duration_ms": None,
            "hard_memory_budget_bytes": None,
        }


def lease_idempotency_key(task_id: str | None, invocation_id: str | None) -> str:
    """`task_id:invocation_id` (contrato 06). Sin contexto de invocación —una
    llamada fuera del coordinador— se genera una clave única: nunca se reutiliza
    la de otra invocación."""
    if task_id and invocation_id:
        key = f"{task_id}:{invocation_id}"
    else:
        key = f"adhoc:{uuid4().hex}"
    key = re.sub(r"[^A-Za-z0-9._:-]", "_", key)
    return key[:_IDEMPOTENCY_KEY_MAX]


class LocalRuntimeResolver:
    """resolve_model / request_lease / release_lease / get_endpoint /
    get_capabilities (spec LARM §63), sobre la API de LARM."""

    def __init__(
        self,
        config: LarmConfig,
        *,
        client: LarmClient | None = None,
        journal: LarmLeaseJournal | None = None,
        token_fetcher: Callable[..., dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.client = client or LarmClient(config)
        self.journal = journal
        self._token_fetcher = token_fetcher or fetch_endpoint_token_blocking
        self._clock = clock
        self._catalog: dict[str, Any] | None = None
        self._catalog_at: float | None = None
        self._catalog_error: LarmError | None = None
        self._launch_profile_id: str | None = config.launch_profile_id
        self._catalog_lock = asyncio.Lock()
        self._pipe_path: str | None = config.credentials_pipe

    async def close(self) -> None:
        await self.client.close()

    # --- catálogo ---------------------------------------------------------------

    async def refresh_catalog(self, *, force: bool = False) -> dict[str, Any] | None:
        """Catálogo vigente, refrescado cada `discovery_interval_seconds`.

        Un fallo no borra el último catálogo bueno hasta que caduca dos veces:
        un parpadeo de LARM no debe hacer desaparecer los modelos a mitad de
        una tirada. Pasado ese margen el catálogo se da por perdido y el
        puente deja de anunciarse."""
        async with self._catalog_lock:
            now = self._clock()
            interval = self.config.discovery_interval_seconds
            if not force and self._catalog_at is not None and now - self._catalog_at < interval:
                return self._catalog
            try:
                catalog = await self.client.catalog()
            except LarmError as error:
                self._catalog_error = error
                if self._catalog_at is None or now - self._catalog_at >= 2 * interval:
                    self._catalog = None
                logger.warning("larm.catalog_unavailable", extra={
                    "event": "larm.catalog_unavailable", "code": error.code,
                })
                return self._catalog
            self._catalog = catalog
            self._catalog_at = now
            self._catalog_error = None
            return catalog

    @property
    def last_error(self) -> LarmError | None:
        return self._catalog_error

    def get_capabilities(self) -> dict[str, Any]:
        available = self._catalog is not None
        return {
            "local_runtime_resolver": available,
            "larm_contract_version": self._catalog.get("contract_version") if available and self._catalog else None,
        }

    async def resolve_model(self, variant_id: str) -> dict[str, Any] | None:
        """Entrada exacta del catálogo para una variante, o None.

        Solo identificadores: un alias o un nombre no es identidad en LARM, así
        que lo que no sea un UUIDv7 no se busca ni se interpreta."""
        if not UUID7_PATTERN.match(variant_id):
            return None
        catalog = await self.refresh_catalog()
        if catalog is None:
            return None
        return next(
            (item for item in catalog["items"] if isinstance(item, dict) and item.get("variant_id") == variant_id),
            None,
        )

    async def launch_profile_id(self) -> str:
        if self._launch_profile_id:
            return self._launch_profile_id
        profiles = await self.client.launch_profiles()
        matches = sorted(
            (item for item in profiles if item.get("name") == self.config.launch_profile),
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        )
        if len(matches) != 1 or not UUID7_PATTERN.match(str(matches[0].get("id") or "")):
            raise LarmError(
                "VALIDATION_FAILED",
                f"LARM tiene {len(matches)} perfiles de arranque {self.config.launch_profile}; "
                "fija larm.launch_profile_id para elegir uno exacto",
            )
        self._launch_profile_id = str(matches[0]["id"])
        return self._launch_profile_id

    # --- leases -----------------------------------------------------------------

    def lease_request_body(
        self,
        entry: dict[str, Any],
        requirements: LeaseResourceRequirements,
        *,
        launch_profile_id: str,
        request_id: str,
        fallback_allowed: bool,
    ) -> dict[str, Any]:
        """Cuerpo estricto de POST /leases a partir de la entrada del catálogo.

        Las restricciones del catálogo viajan tal cual —nunca se relajan— y el
        fallback de backend solo se permite si LARM lo permite Y el cliente del
        broker no lo prohibió."""
        constraints = dict(entry.get("constraints") or {})
        return {
            "variant_id": entry["variant_id"],
            "capability": requirements.capability,
            "launch_profile_id": launch_profile_id,
            "constraints": {
                "runtime_version_id": constraints.get("runtime_version_id"),
                "backend_id": constraints.get("backend_id"),
                "execution_fingerprint": constraints.get("execution_fingerprint"),
                "allow_backend_fallback": bool(constraints.get("allow_backend_fallback")) and fallback_allowed,
                "max_memory_bytes": constraints.get("max_memory_bytes"),
            },
            "ttl_seconds": self.config.lease_ttl_seconds,
            "owner_id": self.config.owner_id,
            "request_id": request_id,
            "resource_requirements": requirements.as_contract(),
        }

    async def request_lease(
        self,
        entry: dict[str, Any],
        requirements: LeaseResourceRequirements,
        *,
        idempotency_key: str,
        task_id: str | None,
        invocation_id: str | None,
        fallback_allowed: bool,
    ) -> ActiveLease:
        variant_id = str(entry["variant_id"])
        if self.journal is not None:
            await asyncio.to_thread(self.journal.requested, idempotency_key, task_id, invocation_id, variant_id)
        try:
            body = self.lease_request_body(
                entry, requirements,
                launch_profile_id=await self.launch_profile_id(),
                request_id=idempotency_key,
                fallback_allowed=fallback_allowed,
            )
        except LarmError as error:
            if self.journal is not None:
                await asyncio.to_thread(
                    self.journal.update, idempotency_key, state="failed", detail={"code": error.code},
                )
            raise
        deadline = self._clock() + self.config.lease_acquire_timeout_seconds
        try:
            status, payload = await self.client.create_lease(body, idempotency_key)
        except LarmError as error:
            if error.code not in {"TIMEOUT", "UPSTREAM_UNAVAILABLE"}:
                # LARM contestó y no admitió la lease: no hay nada que liberar.
                if self.journal is not None:
                    await asyncio.to_thread(
                        self.journal.update, idempotency_key, state="failed", detail={"code": error.code},
                    )
                raise
            # Si este segundo intento también falla, la fila queda en
            # `requested` y la reconciliación del arranque la buscará por
            # request_id. No se sabe si LARM llegó a crearla. Repetir con la MISMA clave es
            # seguro: si existe, LARM devuelve la respuesta original en vez de
            # admitir una segunda lease.
            status, payload = await self.client.create_lease(body, idempotency_key)
        try:
            if status == 202:
                receipt = _require_keys(payload, ("job_id", "state"), "un job de lease")
                payload = await self._wait_for_job(str(receipt["job_id"]), deadline, idempotency_key)
            response = _require_keys(payload, _LEASE_RESPONSE_KEYS, "una lease")
            endpoint = _require_keys(
                response["endpoint"], ("base_url", "protocol", "credential_reference"), "un endpoint",
            )
            record = await self.client.get_lease(str(response["id"]))
        except LarmError as error:
            if error.code != "LEASE_ACQUIRE_TIMEOUT":
                await self._release_by_request_id(idempotency_key)
            if self.journal is not None:
                await asyncio.to_thread(
                    self.journal.update, idempotency_key, state="failed", detail={"code": error.code},
                )
            raise
        lease = ActiveLease(
            idempotency_key=idempotency_key,
            variant_id=variant_id,
            lease_id=str(response["id"]),
            owner_id=str(response["owner_id"]),
            instance_id=str(response["instance_id"]),
            generation=int(response["generation"]),
            row_version=int(record["row_version"]),
            endpoint_id=str(response["endpoint_id"]),
            base_url=str(endpoint["base_url"]),
            protocol=str(endpoint["protocol"]),
            credential_reference=endpoint.get("credential_reference"),
            execution_fingerprint=dict(response["execution_fingerprint"] or {}),
            expires_at=str(response["expires_at"]),
            renew_after_seconds=int(response["renew_after_seconds"]),
        )
        if self.journal is not None:
            await asyncio.to_thread(
                self.journal.update, idempotency_key, state="active",
                lease={"id": lease.lease_id, "generation": lease.generation, "row_version": lease.row_version},
                detail=lease.summary(),
            )
        if record.get("state") != "ACTIVE":
            await self.release_lease(lease)
            raise LarmError(
                "INVALID_STATE_TRANSITION",
                f"LARM entregó la lease {lease.lease_id} en estado {record.get('state')}, no ACTIVE",
            )
        if lease.owner_id != self.config.owner_id:
            await self.release_lease(lease)
            raise LarmError("FORBIDDEN", "LARM entregó una lease de otro propietario")
        return lease

    async def _wait_for_job(self, job_id: str, deadline: float, idempotency_key: str) -> dict[str, Any]:
        while True:
            job = await self.client.get_job(job_id)
            state = str(job.get("state") or "").upper()
            if state == "COMPLETED":
                result = job.get("result")
                if not isinstance(result, dict):
                    raise LarmError("INVALID_RESPONSE", f"El job {job_id} terminó sin lease en result")
                return result
            if state in _TERMINAL_JOB_STATES:
                error = _as_dict(job.get("error"))
                details = _as_dict(error.get("details"))
                raise LarmError(
                    str(error.get("code") or "INTERNAL"),
                    str(error.get("message") or f"El job de lease {job_id} terminó en {state}"),
                    retryable=bool(error.get("retryable", False)),
                    correlation_id=error.get("correlation_id"),
                    reason=details.get("reason"),
                    evidence_refs=[str(item) for item in details.get("evidence_refs") or []],
                )
            if self._clock() >= deadline:
                await self._release_by_request_id(idempotency_key)
                raise LarmError(
                    "LEASE_ACQUIRE_TIMEOUT",
                    f"La lease no llegó a ACTIVE en {self.config.lease_acquire_timeout_seconds:.0f} s "
                    f"(job {job_id} en {state or 'estado desconocido'})",
                    retryable=True,
                )
            await asyncio.sleep(self.config.job_poll_seconds)

    async def _release_by_request_id(self, request_id: str) -> None:
        """Una lease que llegó tarde, tras rendirse el broker, no puede quedarse
        reservando memoria: se busca por request_id y se libera."""
        try:
            for item in await self.client.list_leases():
                if item.get("request_id") == request_id and item.get("state") not in {"RELEASED", "EXPIRED", "FAILED"}:
                    await self.client.release_lease(str(item["id"]), row_version=int(item["row_version"]))
        except LarmError as error:
            logger.warning("larm.late_release_failed", extra={
                "event": "larm.late_release_failed", "code": error.code,
            })

    async def get_endpoint(self, lease: ActiveLease) -> tuple[str, str | None]:
        """(base_url, token) del endpoint de la lease. El token solo existe si
        el endpoint declara credential_reference, y solo llega por el pipe."""
        if lease.credential_reference is None:
            return lease.base_url, None
        if lease.endpoint_token is None:
            pipe = self._pipe_path or default_credentials_pipe(await asyncio.to_thread(current_user_sid))
            if pipe is None:
                raise LarmError(
                    "AUTH_BACKEND_UNAVAILABLE",
                    "El endpoint pide credencial y no hay canal local de credenciales en este sistema",
                )
            self._pipe_path = pipe
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    self._token_fetcher, pipe,
                    lease_id=lease.lease_id,
                    generation=lease.generation,
                    credential_reference_id=lease.credential_reference,
                    bearer_token=self.client.bearer_token(),
                ),
                timeout=self.config.credentials_pipe_timeout_seconds,
            )
            lease.endpoint_token = str(reply["token"])
        return lease.base_url, lease.endpoint_token

    async def renew_loop(self, lease: ActiveLease) -> None:
        """Renueva según `renew_after_seconds` hasta que se cancele.

        Si LARM niega la renovación (lease expirada, generación nueva tras un
        reinicio, otro propietario) o no se consigue renovar antes de que
        caduque, la lease se da por perdida: `lost` se activa y quien sirve la
        inferencia la corta, porque el endpoint deja de estar garantizado."""
        ttl = self.config.lease_ttl_seconds
        expires_by = self._clock() + ttl
        while True:
            await asyncio.sleep(max(1, lease.renew_after_seconds))
            try:
                renewed = await self.client.renew_lease(
                    lease.lease_id,
                    row_version=lease.row_version,
                    generation=lease.generation,
                    ttl_seconds=ttl,
                    idempotency_key=f"{lease.idempotency_key}:renew:{lease.renewals + 1}"[:_IDEMPOTENCY_KEY_MAX],
                )
            except LarmError as error:
                if error.retryable and self._clock() < expires_by:
                    logger.warning("larm.renew_retry", extra={
                        "event": "larm.renew_retry", "lease_id": lease.lease_id, "code": error.code,
                    })
                    continue
                lease.lost_reason = f"{error.code}: {error}"
                lease.lost.set()
                return
            lease.row_version = int(renewed["row_version"])
            lease.expires_at = str(renewed.get("expires_at") or lease.expires_at)
            lease.renewals += 1
            expires_by = self._clock() + ttl
            if renewed.get("state") != "ACTIVE" or int(renewed.get("generation", lease.generation)) != lease.generation:
                lease.lost_reason = f"LARM devolvió la lease en {renewed.get('state')} (generación {renewed.get('generation')})"
                lease.lost.set()
                return

    async def release_lease(self, lease: ActiveLease) -> None:
        """Liberación idempotente; se intenta aunque la tarea esté cancelándose.

        Un 409 por revisión vieja se resuelve releyendo la lease y repitiendo
        una vez. Un fallo definitivo deja la fila del diario abierta y la
        reconciliación del siguiente arranque la vuelve a intentar."""
        lease.endpoint_token = None
        try:
            try:
                await self.client.release_lease(lease.lease_id, row_version=lease.row_version)
            except LarmError as error:
                if error.code not in {"STALE_REVISION", "CONFLICT"}:
                    raise
                current = await self.client.get_lease(lease.lease_id)
                await self.client.release_lease(lease.lease_id, row_version=int(current["row_version"]))
        except LarmError as error:
            logger.warning("larm.release_failed", extra={
                "event": "larm.release_failed", "lease_id": lease.lease_id, "code": error.code,
            })
            if self.journal is not None:
                await asyncio.to_thread(
                    self.journal.update, lease.idempotency_key, state="release_pending",
                    detail=lease.summary() | {"release_error": error.code},
                )
            return
        if self.journal is not None:
            await asyncio.to_thread(
                self.journal.update, lease.idempotency_key, state="released", detail=lease.summary(),
            )

    @contextlib.asynccontextmanager
    async def leased(
        self,
        entry: dict[str, Any],
        requirements: LeaseResourceRequirements,
        *,
        idempotency_key: str,
        task_id: str | None,
        invocation_id: str | None,
        fallback_allowed: bool,
    ) -> AsyncIterator[ActiveLease]:
        lease = await self.request_lease(
            entry, requirements,
            idempotency_key=idempotency_key, task_id=task_id, invocation_id=invocation_id,
            fallback_allowed=fallback_allowed,
        )
        renewer = asyncio.create_task(self.renew_loop(lease))
        try:
            yield lease
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer
            # La liberación sobrevive a la cancelación de la tarea: si la
            # cancelaran a medias, la memoria quedaría reservada hasta que
            # caducara la lease.
            release = asyncio.ensure_future(self.release_lease(lease))
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError:
                await release
                raise

    async def reconcile(self) -> dict[str, int]:
        """Libera las leases que quedaron abiertas en el diario.

        Se ejecuta al arrancar el broker. LARM invalida las generaciones al
        reiniciarse él, pero si quien se reinició fue el broker, la lease sigue
        viva en LARM hasta que caduca: liberarla ya devuelve la memoria sin
        esperar al TTL. No relanza ninguna inferencia: la invocación que la
        tenía la trata la recuperación de tareas (`started` = ambigua)."""
        if self.journal is None:
            return {"released": 0, "failed": 0}
        released = failed = 0
        for row in await asyncio.to_thread(self.journal.open_leases):
            key = str(row["idempotency_key"])
            lease_id = row.get("lease_id")
            try:
                if lease_id:
                    current = await self.client.get_lease(str(lease_id))
                    if current.get("state") not in {"RELEASED", "EXPIRED", "FAILED"}:
                        await self.client.release_lease(str(lease_id), row_version=int(current["row_version"]))
                else:
                    await self._release_by_request_id(key)
            except LarmError as error:
                if error.code in {"NOT_FOUND"} or error.status == 404:
                    await asyncio.to_thread(self.journal.update, key, state="released")
                    released += 1
                    continue
                failed += 1
                logger.warning("larm.reconcile_failed", extra={
                    "event": "larm.reconcile_failed", "code": error.code,
                })
                continue
            await asyncio.to_thread(self.journal.update, key, state="released")
            released += 1
        return {"released": released, "failed": failed}


# --- Huella --------------------------------------------------------------------


def fingerprint_parts(
    larm_fingerprint: dict[str, Any] | None,
    *,
    variant_id: str,
    source: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """(componentes del broker, extensión `larm.v1`) desde una huella LARM.

    Solo se traducen los componentes con equivalente exacto; los estados
    VERIFIED/DECLARED/UNAVAILABLE pasan a verificado/declarado/no_disponible
    sin mejorar ninguno. Lo demás —backend, launch profile, hardware— viaja
    entero en la extensión con su estado, sin inventar valores."""
    components = (larm_fingerprint or {}).get("components") or {}
    mapped: dict[str, dict[str, Any]] = {}
    for larm_name, broker_name in _COMPONENT_MAP.items():
        item = components.get(larm_name)
        if not isinstance(item, dict) or item.get("value") in (None, ""):
            continue
        status = _STATUS_MAP.get(str(item.get("status") or "").upper())
        if status is None or status == "no_disponible":
            continue
        mapped[broker_name] = {"value": str(item["value"]), "status": status}
    extension = {
        "contract_version": LARM_CONTRACT_VERSION,
        "source": source,
        "variant_id": variant_id,
        "fingerprint_hash": (larm_fingerprint or {}).get("hash"),
        "components": {
            name: {
                "value": item.get("value") if isinstance(item, dict) else None,
                "status": str(item.get("status")) if isinstance(item, dict) else "UNAVAILABLE",
            }
            for name, item in sorted(components.items())
        },
    }
    return mapped, {FINGERPRINT_EXTENSION_KEY: extension}


def effective_backend(fingerprint: dict[str, Any] | None) -> str | None:
    item = ((fingerprint or {}).get("components") or {}).get("backend")
    if isinstance(item, dict) and item.get("value") not in (None, "") and str(item.get("status")).upper() != "UNAVAILABLE":
        return str(item["value"])
    return None
