"""LARM simulado para los contract tests del puente Broker–LARM (P107).

No es un LARM: es un doble que habla EXACTAMENTE el contrato 1.0 y lo hace
cumplir en las dos direcciones. Cada petición del broker se valida contra los
schemas LARM vendorizados en tests/fixtures/larm (LeaseRequest, Renew, cabeceras
obligatorias) y cada respuesta del doble también (BrokerCatalog, LeaseResponse,
runtime_lease, JobReceipt, JobState, Error): si el doble se desviara del
contrato, los tests fallarían por él y no darían un PASS falso.

Lo que el doble NO puede probar —carga real de un modelo, memoria real, el
named pipe de LARM— queda para la prueba real P107 (tools/p107_real.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

FIXTURES = Path(__file__).parent / "fixtures" / "larm"
# Hashes del paquete LARM v3 (manifest.json): el doble usa esos schemas y no
# otros. Un cambio de schema exige actualizar el vendorizado y estos hashes.
SCHEMA_SHA256 = {
    "larm.schema.json": "4413e5b9b932da0684a71bdf33a9a1e14e39cc477fd65d8f450625edeccf0cc3",
    "domain.schema.json": "25e24600f01ed044830fa9027433e7ae4591c70134db01f7109d298657f00635",
}


def _load_schemas() -> tuple[dict[str, Any], Registry]:
    documents = {}
    for name in SCHEMA_SHA256:
        raw = (FIXTURES / name).read_bytes()
        documents[name] = json.loads(raw)
    registry = Registry().with_resources(
        (name, Resource.from_contents(document)) for name, document in documents.items()
    )
    return documents["larm.schema.json"], registry


LARM_SCHEMA, REGISTRY = _load_schemas()


def validator(definition: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"larm.schema.json#/$defs/{definition}"}, registry=REGISTRY,
    )


VALIDATORS = {
    name: validator(name)
    for name in (
        "BrokerCatalog", "LeaseRequest", "LeaseResponse", "runtime_lease", "Renew",
        "JobReceipt", "JobState", "Error", "launch_profileList", "runtime_leaseList",
    )
}


def check(definition: str, payload: Any) -> None:
    errors = sorted(VALIDATORS[definition].iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise AssertionError(f"{definition} no cumple el schema LARM: {errors[0].message} en {list(errors[0].path)}")


def uuid7() -> str:
    millis = int(time.time() * 1000)
    rand = secrets.randbits(74)
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    text = f"{value:032x}"
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def ts(delta_seconds: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def component(value: str | None, status: str = "VERIFIED") -> dict[str, Any]:
    return {"value": value, "status": status if value is not None else "UNAVAILABLE", "evidence_refs": []}


def fingerprint(components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    canonical = json.dumps(
        {name: [item["value"], item["status"]] for name, item in sorted(components.items())},
        separators=(",", ":"), sort_keys=True,
    )
    return {"hash": hashlib.sha256(canonical.encode()).hexdigest(), "components": components}


def larm_error(code: str, status: int, message: str, *, retryable: bool = False,
               reason: str | None = None) -> httpx.Response:
    body = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "correlation_id": uuid7(),
        "details": {
            "reason": reason or code.lower(),
            "evidence_refs": ["evidence://fake/" + code.lower()],
            "resource_ids": [],
            "original_error": None,
        },
    }
    check("Error", body)
    return httpx.Response(status, json=body)


class FakeVariant:
    def __init__(self, *, capabilities: list[str] | None = None, backend: str = "VULKAN",
                 protocol: str = "openai", allow_backend_fallback: bool = False) -> None:
        self.variant_id = uuid7()
        self.runtime_version_id = uuid7()
        self.backend_id = uuid7()
        self.capabilities = capabilities or ["text.generate"]
        self.protocol = protocol
        self.allow_backend_fallback = allow_backend_fallback
        self.components = {
            "artifact_bundle_hash": component("a" * 64),
            "runtime_id": component("llama.cpp"),
            "runtime_version": component("b9999"),
            "backend": component(backend),
            "tokenizer_hash": component("b" * 64, "DECLARED"),
            "chat_template_hash": component(None),
            "hardware_fingerprint": component("c" * 64),
            "launch_profile_hash": component("d" * 64),
        }

    @property
    def fingerprint(self) -> dict[str, Any]:
        return fingerprint(self.components)

    def catalog_item(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "capabilities": list(self.capabilities),
            "runtime_version_ids": [self.runtime_version_id],
            "backend_ids": [self.backend_id],
            "protocol_profiles": [{
                "protocol": self.protocol,
                "profile_hash": "e" * 64,
                "capabilities": list(self.capabilities),
                "streaming": False,
                "tools": False,
                "request_schema_id": "openai.chat_completions.request.v1",
                "response_schema_id": "openai.chat_completions.response.v1",
            }],
            "fingerprint": self.fingerprint,
            "constraints": {
                "runtime_version_id": self.runtime_version_id,
                "backend_id": self.backend_id,
                "execution_fingerprint": None,
                "allow_backend_fallback": self.allow_backend_fallback,
                "max_memory_bytes": None,
            },
        }


class FakeLarm:
    """Estado mínimo de LARM: catálogo, perfiles, leases, jobs e idempotencia."""

    BASE = "http://127.0.0.1:8790"

    def __init__(self, *, token_env: str = "LARM_BROKER_TOKEN_TEST") -> None:
        self.token_env = token_env
        self.variants: list[FakeVariant] = [FakeVariant()]
        self.launch_profile_id = uuid7()
        self.leases: dict[str, dict[str, Any]] = {}
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.replays: dict[str, tuple[int, Any, str]] = {}
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.admissions = 0
        self.released: list[str] = []
        self.renewals: list[str] = []
        # Conductas configurables por test.
        self.offline = False
        self.forbid_leases = False
        self.resource_unavailable: bool | None = None  # True retryable, False terminal
        self.async_jobs = False
        self.job_never_completes = False
        self.renew_fails_with: str | None = None
        self.serve_backend: str | None = None
        self.credential_required = False
        self.renew_after_seconds = 1
        self.drop_first_lease_response = False
        self.endpoint_base = "http://127.0.0.1:18432/v1"
        # Gancho para casos que no merecen una conducta propia: devuelve una
        # respuesta (o lanza) antes del enrutado normal; None = seguir.
        self.intercept: Any = None

    @property
    def variant(self) -> FakeVariant:
        return self.variants[0]

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env)

    # --- transporte ------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.headers), body))
        if self.offline:
            raise httpx.ConnectError("LARM apagado", request=request)
        if self.intercept is not None:
            intercepted = self.intercept(request)
            if intercepted is not None:
                return intercepted
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return larm_error("AUTH_REQUIRED", 401, "Bearer inválido")
        path, method = request.url.path, request.method
        if method == "GET" and path == "/api/v1/broker/catalog":
            payload = {
                "contract_version": "1.0",
                "items": [variant.catalog_item() for variant in self.variants],
                "snapshot_at": ts(),
            }
            check("BrokerCatalog", payload)
            return httpx.Response(200, json=payload)
        if method == "GET" and path == "/api/v1/launch-profiles":
            payload = {"items": [self._launch_profile()], "next_cursor": None, "snapshot_at": ts()}
            check("launch_profileList", payload)
            return httpx.Response(200, json=payload)
        if method == "POST" and path == "/api/v1/leases":
            return self._create_lease(request, body)
        if method == "GET" and path == "/api/v1/leases":
            payload = {"items": [self._record(lease) for lease in self.leases.values()],
                       "next_cursor": None, "snapshot_at": ts()}
            check("runtime_leaseList", payload)
            return httpx.Response(200, json=payload)
        if path.startswith("/api/v1/jobs/") and method == "GET":
            job = self.jobs.get(path.rsplit("/", 1)[1])
            if job is None:
                return larm_error("NOT_FOUND", 404, "job desconocido")
            check("JobState", job)
            return httpx.Response(200, json=job)
        if path.startswith("/api/v1/leases/"):
            parts = path.split("/")
            lease = self.leases.get(parts[4])
            if lease is None:
                return larm_error("NOT_FOUND", 404, "lease desconocida")
            if method == "GET" and len(parts) == 5:
                record = self._record(lease)
                check("runtime_lease", record)
                return httpx.Response(200, json=record)
            if method == "POST" and len(parts) == 6 and parts[5] == "renew":
                return self._renew(request, lease, body)
            if method == "DELETE" and len(parts) == 5:
                return self._release(request, lease)
        return larm_error("NOT_FOUND", 404, f"ruta {method} {path} no existe")

    # --- operaciones -------------------------------------------------------------

    def _launch_profile(self) -> dict[str, Any]:
        return {
            "id": self.launch_profile_id,
            "created_at": ts(), "updated_at": ts(), "row_version": 1, "metadata": {},
            "name": "BALANCED",
            "parameters": {
                "threads": 8, "context": 4096, "batch": 1, "concurrency": 1, "offload_layers": None,
                "backend_id": self.variant.backend_id, "tokenizer_hash": None, "chat_template_hash": None,
                "generation": {"temperature": 0.0, "top_p": 1.0, "seed": 42, "max_output_tokens": 256},
                "memory_limit_bytes": None, "runtime_arguments": [],
                "environment": {"allowlist": [], "variables": []}, "adapter_options": {},
            },
            "profile_hash": "f" * 64,
            "backend_id": None,
        }

    def _create_lease(self, request: httpx.Request, body: Any) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        assert key and 1 <= len(key) <= 240, "POST /leases sin Idempotency-Key válida"
        check("LeaseRequest", body)
        body_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if key in self.replays:
            status, payload, previous_hash = self.replays[key]
            if previous_hash != body_hash:
                return larm_error("IDEMPOTENCY_CONFLICT", 409, "mismo key, cuerpo distinto")
            return httpx.Response(status, json=payload)
        if self.forbid_leases:
            return larm_error("FORBIDDEN", 403, "el principal no tiene leases:manage")
        variant = next((v for v in self.variants if v.variant_id == body["variant_id"]), None)
        if variant is None:
            return larm_error("MODEL_NOT_FOUND", 404, "variante desconocida")
        if self.resource_unavailable is not None:
            retryable = self.resource_unavailable
            return larm_error(
                "RESOURCE_UNAVAILABLE", 409,
                "no hay memoria para admitir la variante" if retryable else "supera el presupuesto total",
                retryable=retryable, reason="memory_budget",
            )
        assert body["launch_profile_id"] == self.launch_profile_id
        assert body["owner_id"] == "ai-broker"
        self.admissions += 1
        lease = self._admit(variant, body)
        response = self._lease_response(lease)
        check("LeaseResponse", response)
        if self.async_jobs:
            job_id = uuid7()
            receipt = {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}", "state": "QUEUED"}
            check("JobReceipt", receipt)
            self.jobs[job_id] = {
                "job_id": job_id, "state": "RUNNING", "resource_type": "runtime_lease",
                "resource_id": lease["id"], "progress": {}, "result": None, "error": None,
                "evidence_refs": [],
            }
            if not self.job_never_completes:
                self.jobs[job_id] |= {"state": "COMPLETED", "result": response}
            self.replays[key] = (202, receipt, body_hash)
            return httpx.Response(202, json=receipt)
        self.replays[key] = (201, response, body_hash)
        if self.drop_first_lease_response:
            # LARM crea la lease pero la respuesta se pierde por el camino.
            self.drop_first_lease_response = False
            raise httpx.ReadTimeout("respuesta perdida", request=request)
        return httpx.Response(201, json=response)

    def _admit(self, variant: FakeVariant, body: dict[str, Any]) -> dict[str, Any]:
        lease_id = uuid7()
        served = dict(variant.components)
        if self.serve_backend:
            served["backend"] = component(self.serve_backend)
        lease = {
            "id": lease_id,
            "created_at": ts(), "updated_at": ts(), "row_version": 1, "metadata": {},
            "owner_id": body["owner_id"],
            "request_id": body["request_id"],
            "instance_id": uuid7(),
            "generation": 3,
            "state": "ACTIVE",
            "expires_at": ts(body["ttl_seconds"]),
            "endpoint_id": uuid7(),
            "constraints": body["constraints"],
            "_fingerprint": fingerprint(served),
            "_credential": uuid7() if self.credential_required else None,
            "_requirements": body["resource_requirements"],
        }
        self.leases[lease_id] = lease
        if lease["_credential"]:
            self.endpoints[lease_id] = {"token": secrets.token_hex(32)}
        return lease

    def _lease_response(self, lease: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": lease["id"],
            "owner_id": lease["owner_id"],
            "instance_id": lease["instance_id"],
            "generation": lease["generation"],
            "endpoint_id": lease["endpoint_id"],
            "endpoint": {
                "base_url": self.endpoint_base,
                "protocol": "openai",
                "credential_reference": lease["_credential"],
            },
            "execution_fingerprint": lease["_fingerprint"],
            "expires_at": lease["expires_at"],
            "renew_after_seconds": self.renew_after_seconds,
        }

    @staticmethod
    def _record(lease: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in lease.items() if not key.startswith("_")}

    def _renew(self, request: httpx.Request, lease: dict[str, Any], body: Any) -> httpx.Response:
        assert request.headers.get("idempotency-key"), "renew sin Idempotency-Key"
        if_match = request.headers.get("if-match")
        if if_match is None:
            return larm_error("VALIDATION_FAILED", 428, "If-Match obligatorio")
        check("Renew", body)
        if self.renew_fails_with:
            return larm_error(self.renew_fails_with, 409, "la lease ya no está ACTIVE")
        if int(if_match) != lease["row_version"]:
            return larm_error("STALE_REVISION", 409, "row_version vieja")
        if body["generation"] != lease["generation"] or lease["state"] != "ACTIVE":
            return larm_error("INVALID_STATE_TRANSITION", 409, "generación o estado inválido")
        lease["row_version"] += 1
        lease["expires_at"] = ts(body["ttl_seconds"])
        lease["updated_at"] = ts()
        self.renewals.append(lease["id"])
        record = self._record(lease)
        check("runtime_lease", record)
        return httpx.Response(201, json=record)

    def _release(self, request: httpx.Request, lease: dict[str, Any]) -> httpx.Response:
        if_match = request.headers.get("if-match")
        if if_match is None:
            return larm_error("VALIDATION_FAILED", 428, "If-Match obligatorio")
        if lease["state"] == "RELEASED":
            return httpx.Response(204)
        if int(if_match) != lease["row_version"]:
            return larm_error("STALE_REVISION", 409, "row_version vieja")
        lease["state"] = "RELEASED"
        lease["row_version"] += 1
        self.endpoints.pop(lease["id"], None)
        self.released.append(lease["id"])
        return httpx.Response(204)

    # --- canal de credenciales simulado -----------------------------------------

    def token_fetcher(self, pipe: str, *, lease_id: str, generation: int,
                      credential_reference_id: str, bearer_token: str) -> dict[str, Any]:
        """Mismas comprobaciones que el pipe real: principal, generación, lease
        propia y referencia exacta. Lo único simulado es el transporte."""
        lease = self.leases.get(lease_id)
        assert bearer_token == self.token
        assert lease is not None and lease["state"] == "ACTIVE"
        assert lease["generation"] == generation
        assert lease["_credential"] == credential_reference_id
        return {"token": self.endpoints[lease_id]["token"], "expires_at": lease["expires_at"]}


class FakeRuntime:
    """Endpoint OpenAI del runtime servido bajo lease."""

    def __init__(self, larm: FakeLarm, *, delay_seconds: float = 0.0, content: str = "respuesta LARM") -> None:
        self.larm = larm
        self.delay_seconds = delay_seconds
        self.content = content
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append({"path": request.url.path, "headers": dict(request.headers), "body": body})
        self.started.set()
        active = [lease for lease in self.larm.leases.values() if lease["state"] == "ACTIVE"]
        assert active, "el broker llamó al endpoint sin lease ACTIVE"
        if any(lease["_credential"] for lease in active):
            tokens = {f"Bearer {self.larm.endpoints[lease['id']]['token']}" for lease in active if lease["_credential"]}
            assert request.headers.get("authorization") in tokens, "token de endpoint ausente o ajeno"
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={
                "object": "list", "model": "runtime-embed",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            })
        return httpx.Response(200, json={
            "id": "chatcmpl-larm", "object": "chat.completion", "model": "runtime-model-q4",
            "system_fingerprint": "fp_larm_runtime",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        })
