"""Casos límite del puente Broker–LARM: las ramas de recuperación.

Complementa tests/test_larm_bridge.py con lo que pasa cuando LARM contesta a
medias: una renovación que falla una vez, una liberación con revisión vieja,
un job que termina en error, un catálogo que parpadea o respuestas que no son
del contrato. Cada caso comprueba la conducta observable (qué código recibe el
cliente, qué queda en LARM, qué queda en el diario), no el algoritmo.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import invocation_context
from app.config import BrokerConfig
from app.db import Database
from app.larm import (
    FINGERPRINT_EXTENSION_KEY,
    LarmClient,
    LarmError,
    LarmLeaseJournal,
    LocalRuntimeResolver,
    effective_backend,
    fingerprint_parts,
)
from app.providers.base import ProviderError
from app.providers.larm import LarmProvider, provider_error
from app.providers.routing import RoutedModelProvider
from tests.larm_fake import FakeLarm, FakeRuntime, FakeVariant, larm_error, ts, uuid7
from tests.test_larm_bridge import TOKEN_ENV, bridge, broker_config, chat_request


@pytest.fixture(autouse=True)
def broker_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "broker-token-1")
    invocation_context.reset()


def generate(provider: LarmProvider, fake: FakeLarm, **kwargs: Any) -> Any:
    request = chat_request(fake.variant.variant_id, **kwargs)
    return asyncio.run(provider.generate(request, fake.variant.variant_id, "hola"))


def _once(fake: FakeLarm, method: str, suffix: str, response: httpx.Response) -> None:
    """Intercepta UNA petición que case con método y sufijo de ruta."""
    state = {"used": False}

    def intercept(request: httpx.Request) -> httpx.Response | None:
        if state["used"] or request.method != method or not request.url.path.endswith(suffix):
            return None
        state["used"] = True
        return response

    fake.intercept = intercept


# --- renovación y liberación ----------------------------------------------------------


def test_transient_renew_failure_is_retried_and_the_lease_survives(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    _once(fake, "POST", "/renew", larm_error("UPSTREAM_UNAVAILABLE", 503, "parpadeo", retryable=True))
    provider = bridge(config, fake, FakeRuntime(fake, delay_seconds=2.6))
    output = generate(provider, fake)
    assert output.content == "respuesta LARM"
    lease_id = next(iter(fake.leases))
    # Tras el parpadeo se renovó de verdad y al final se liberó.
    assert fake.renewals and fake.released == [lease_id]


def test_renewal_with_a_new_generation_counts_as_a_lost_lease(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()

    def bump_generation(request: httpx.Request) -> httpx.Response | None:
        if request.method == "POST" and request.url.path.endswith("/renew"):
            lease = fake.leases[request.url.path.split("/")[4]]
            record = {k: v for k, v in lease.items() if not k.startswith("_")}
            return httpx.Response(201, json=record | {"generation": lease["generation"] + 1, "row_version": 9})
        return None

    fake.intercept = bump_generation
    provider = bridge(config, fake, FakeRuntime(fake, delay_seconds=5))
    with pytest.raises(ProviderError) as raised:
        generate(provider, fake)
    assert raised.value.details["larm_error"]["code"] == "LEASE_LOST"


def test_stale_revision_on_release_is_resolved_by_rereading(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()

    def touch_before_release(request: httpx.Request) -> httpx.Response | None:
        if request.method == "DELETE":
            lease = fake.leases[request.url.path.split("/")[4]]
            if lease.get("_touched") is None:
                # Alguien (el supervisor de LARM) actualizó la fila entretanto.
                lease["_touched"] = True
                lease["row_version"] += 1
        return None

    fake.intercept = touch_before_release
    provider = bridge(config, fake, FakeRuntime(fake))
    generate(provider, fake)
    assert fake.released == [next(iter(fake.leases))]


def test_failed_release_stays_pending_until_the_next_reconciliation(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    db = Database(tmp_path / "broker.db")
    db.init_schema()
    journal = LarmLeaseJournal(db)
    fake.intercept = lambda request: (
        larm_error("INTERNAL", 500, "fallo al liberar") if request.method == "DELETE" else None
    )
    provider = bridge(config, fake, FakeRuntime(fake), journal=journal)

    async def scenario() -> None:
        invocation_context.bind("task_r", "inv_r")
        await provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola")

    asyncio.run(scenario())
    assert fake.released == []
    assert journal.by_invocation("inv_r")["state"] == "release_pending"
    fake.intercept = None
    assert asyncio.run(provider.resolver.reconcile()) == {"released": 1, "failed": 0}
    assert fake.released == [next(iter(fake.leases))]
    db.close()


def test_reconciliation_counts_failures_and_forgets_unknown_leases(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    db = Database(tmp_path / "broker.db")
    db.init_schema()
    journal = LarmLeaseJournal(db)
    journal.requested("k:gone", "k", "gone", fake.variant.variant_id)
    journal.update("k:gone", state="active", lease={"id": uuid7(), "generation": 1, "row_version": 1})
    journal.requested("k:never", "k", "never", fake.variant.variant_id)
    resolver = LocalRuntimeResolver(
        config.larm, client=LarmClient(config.larm, transport=fake.transport()), journal=journal,
    )
    # Una lease que LARM ya no conoce se da por liberada; una petición que
    # nunca llegó a lease se busca por request_id (y no hay nada).
    assert asyncio.run(resolver.reconcile()) == {"released": 2, "failed": 0}
    journal.requested("k:down", "k", "down", fake.variant.variant_id)
    journal.update("k:down", state="active", lease={"id": uuid7(), "generation": 1, "row_version": 1})
    fake.offline = True
    assert asyncio.run(resolver.reconcile()) == {"released": 0, "failed": 1}
    assert [row["idempotency_key"] for row in journal.open_leases()] == ["k:down"]
    db.close()


# --- admisión ----------------------------------------------------------------------------


def test_failed_admission_job_keeps_larm_error_semantics(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.async_jobs = True
    fake.job_never_completes = True
    provider = bridge(config, fake, FakeRuntime(fake))

    def fail_job(request: httpx.Request) -> httpx.Response | None:
        if request.method == "GET" and request.url.path.startswith("/api/v1/jobs/"):
            job = fake.jobs[request.url.path.rsplit("/", 1)[1]]
            error = larm_error("LOAD_OOM", 409, "el runtime se quedó sin memoria al cargar").json()
            return httpx.Response(200, json=job | {"state": "FAILED", "error": error})
        return None

    fake.intercept = fail_job
    with pytest.raises(ProviderError) as raised:
        generate(provider, fake)
    assert raised.value.code == "VRAM_MODEL_TOO_LARGE"
    assert raised.value.details["larm_error"]["code"] == "LOAD_OOM"


def test_lease_not_active_or_from_another_owner_is_released(tmp_path: Path) -> None:
    for field, value in (("state", "EXPIRING"), ("owner_id", "otro-principal")):
        config = broker_config(tmp_path)
        fake = FakeLarm()

        def tamper(request: httpx.Request, field: str = field, value: str = value,
                   fake: FakeLarm = fake) -> httpx.Response | None:
            path = request.url.path
            if request.method == "GET" and path.startswith("/api/v1/leases/") and path.count("/") == 4:
                lease = fake.leases[path.rsplit("/", 1)[1]]
                record = {k: v for k, v in lease.items() if not k.startswith("_")}
                return httpx.Response(200, json=record | ({field: value} if field == "state" else {}))
            if request.method == "POST" and path == "/api/v1/leases" and field == "owner_id":
                fake.intercept = None
                response = fake.handle(request)
                return httpx.Response(response.status_code, json=response.json() | {"owner_id": value})
            return None

        fake.intercept = tamper
        provider = bridge(config, fake, FakeRuntime(fake))
        with pytest.raises(ProviderError):
            generate(provider, fake)
        assert fake.released == [next(iter(fake.leases))]


def test_ambiguous_launch_profile_is_refused_and_explicit_id_wins(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()

    def duplicate(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/v1/launch-profiles":
            profile = fake._launch_profile()
            twin = profile | {"id": uuid7()}
            return httpx.Response(200, json={"items": [profile, twin], "next_cursor": None, "snapshot_at": ts()})
        return None

    fake.intercept = duplicate
    with pytest.raises(ProviderError) as raised:
        generate(bridge(config, fake, FakeRuntime(fake)), fake)
    assert raised.value.code == "INVALID_PROVIDER_RESPONSE"
    assert fake.admissions == 0
    pinned = broker_config(tmp_path, launch_profile_id=fake.launch_profile_id)
    assert generate(bridge(pinned, fake, FakeRuntime(fake)), fake).content == "respuesta LARM"


# --- catálogo ---------------------------------------------------------------------------


def test_catalog_survives_a_blip_but_not_a_long_outage(tmp_path: Path) -> None:
    config = broker_config(tmp_path, discovery_interval_seconds=30)
    fake = FakeLarm()
    now = [1000.0]
    resolver = LocalRuntimeResolver(
        config.larm, client=LarmClient(config.larm, transport=fake.transport()), clock=lambda: now[0],
    )
    assert asyncio.run(resolver.refresh_catalog()) is not None
    fake.offline = True
    now[0] += 31
    assert asyncio.run(resolver.refresh_catalog()) is not None  # parpadeo: se conserva
    assert resolver.last_error is not None and resolver.get_capabilities()["local_runtime_resolver"] is True
    now[0] += 31
    assert asyncio.run(resolver.refresh_catalog()) is None  # caído de verdad
    assert resolver.get_capabilities() == {"local_runtime_resolver": False, "larm_contract_version": None}


def test_other_contract_versions_are_not_spoken(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.intercept = lambda request: (
        httpx.Response(200, json={"contract_version": "2.0", "items": [], "snapshot_at": ts()})
        if request.url.path == "/api/v1/broker/catalog" else None
    )
    provider = bridge(config, fake, FakeRuntime(fake))
    routed = RoutedModelProvider(config, custom={}, larm=provider)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(routed.select(chat_request(fake.variant.variant_id), 1, ["single"]))
    assert raised.value.code == "MODEL_CAPABILITY_MISMATCH"
    assert raised.value.retryable is False
    assert raised.value.details["larm_error"]["code"] == "CONTRACT_UNSUPPORTED"


def test_unsupported_protocol_or_capability_is_a_capability_mismatch(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.variants = [FakeVariant(protocol="anthropic"), FakeVariant(capabilities=["embedding.create"])]
    provider = bridge(config, fake, FakeRuntime(fake))
    routed = RoutedModelProvider(config, custom={}, larm=provider)
    for variant in fake.variants:
        with pytest.raises(ProviderError) as raised:
            asyncio.run(routed.select(chat_request(variant.variant_id), 1, ["single"]))
        assert raised.value.code == "MODEL_CAPABILITY_MISMATCH"
        with pytest.raises(ProviderError) as direct:
            asyncio.run(provider.generate(chat_request(variant.variant_id), variant.variant_id, "hola"))
        assert direct.value.code == "MODEL_CAPABILITY_MISMATCH"
    assert fake.admissions == 0


def test_health_reports_catalog_state(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    provider = bridge(config, fake, FakeRuntime(fake))
    routed = RoutedModelProvider(config, custom={}, larm=provider)
    health = asyncio.run(routed.health())
    assert health["larm"]["status"] == "healthy"
    fake.variants = []
    assert asyncio.run(provider.health())["status"] == "degraded"
    fake.offline = True
    assert asyncio.run(provider.health())["status"] == "unavailable"


def test_reload_keeps_the_bridge_unless_its_config_changes(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    provider = bridge(config, fake, FakeRuntime(fake))
    same = asyncio.run(provider.reload_config(config.model_copy(update={"logging": config.logging})))
    assert same is provider
    changed = config.model_copy(update={"larm": config.larm.model_copy(update={"lease_ttl_seconds": 300})})
    replacement = asyncio.run(provider.reload_config(changed))
    assert replacement is not provider and replacement.config.larm.lease_ttl_seconds == 300


# --- respuestas fuera de contrato y credenciales --------------------------------------


def test_non_contract_responses_are_classified(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    cases = [
        (httpx.Response(200, text="<html>portal</html>"), "INVALID_RESPONSE", False),
        (httpx.Response(502, text="bad gateway"), "UPSTREAM_UNAVAILABLE", True),
        (httpx.Response(418, text="tetera"), "INVALID_RESPONSE", False),
        (httpx.Response(200, json={"contract_version": "1.0"}), "INVALID_RESPONSE", False),
        (httpx.Response(200, json=["no", "objeto"]), "INVALID_RESPONSE", False),
    ]
    for response, code, retryable in cases:
        client = LarmClient(config.larm, transport=httpx.MockTransport(lambda _r, resp=response: resp))
        with pytest.raises(LarmError) as raised:
            asyncio.run(client.catalog())
        assert (raised.value.code, raised.value.retryable) == (code, retryable)


def test_timeouts_are_retryable_and_missing_credentials_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = broker_config(tmp_path)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    with pytest.raises(LarmError) as raised:
        asyncio.run(LarmClient(config.larm, transport=httpx.MockTransport(slow)).catalog())
    assert raised.value.code == "TIMEOUT" and raised.value.retryable
    assert provider_error(raised.value, "v").code == "PROVIDER_UNAVAILABLE"
    monkeypatch.delenv(TOKEN_ENV)
    fake = FakeLarm()
    with pytest.raises(ProviderError) as missing:
        generate(bridge(config, fake, FakeRuntime(fake)), fake)
    assert missing.value.code == "CREDENTIALS_UNAVAILABLE"
    assert fake.requests == []


def test_credentials_channel_timeout_releases_the_lease(tmp_path: Path) -> None:
    config = broker_config(tmp_path, credentials_pipe_timeout_seconds=0.1)
    fake = FakeLarm()
    fake.credential_required = True

    def stuck(*_: Any, **__: Any) -> dict[str, Any]:
        import time

        time.sleep(0.5)
        return {"token": "tarde"}

    resolver = LocalRuntimeResolver(
        config.larm, client=LarmClient(config.larm, transport=fake.transport()), token_fetcher=stuck,
    )
    provider = LarmProvider(config, resolver=resolver, endpoint_transport=FakeRuntime(fake).transport())
    with pytest.raises(ProviderError) as raised:
        generate(provider, fake)
    assert raised.value.code == "PROVIDER_UNAVAILABLE" and raised.value.retryable
    assert fake.released == [next(iter(fake.leases))]


def test_error_mapping_table() -> None:
    expectations = {
        ("RESOURCE_UNAVAILABLE", True): "VRAM_INSUFFICIENT",
        ("RESOURCE_UNAVAILABLE", False): "VRAM_MODEL_TOO_LARGE",
        ("INCOMPATIBLE", False): "MODEL_CAPABILITY_MISMATCH",
        ("MODEL_OFFLINE", True): "MODEL_UNAVAILABLE",
        ("FORBIDDEN", False): "CREDENTIALS_UNAVAILABLE",
        ("IDEMPOTENCY_CONFLICT", False): "INVALID_PROVIDER_RESPONSE",
        ("RUNTIME_CRASH", False): "PROVIDER_UNAVAILABLE",
        ("SOMETHING_NEW", False): "PROVIDER_UNAVAILABLE",
    }
    for (code, retryable), expected in expectations.items():
        mapped = provider_error(LarmError(code, "x", retryable=retryable), "v")
        assert mapped.code == expected, code
    assert provider_error(LarmError("RUNTIME_CRASH", "x"), "v").retryable is True
    assert provider_error(LarmError("SOMETHING_NEW", "x"), "v").retryable is False
    waiting = provider_error(LarmError("RESOURCE_UNAVAILABLE", "x", retryable=True, reason="memory"), "v")
    assert waiting.memory_block["larm_reason"] == "memory"  # type: ignore[attr-defined]


def test_fingerprint_translation_never_upgrades_a_status() -> None:
    larm_fp = {
        "hash": "h" * 64,
        "components": {
            "runtime_id": {"value": "llama.cpp", "status": "DECLARED", "evidence_refs": []},
            "runtime_version": {"value": "b1", "status": "UNAVAILABLE", "evidence_refs": []},
            "artifact_bundle_hash": {"value": None, "status": "UNAVAILABLE", "evidence_refs": []},
            "backend": {"value": "VULKAN", "status": "VERIFIED", "evidence_refs": []},
        },
    }
    mapped, extension = fingerprint_parts(larm_fp, variant_id="v", source="lease")
    assert mapped == {"runtime": {"value": "llama.cpp", "status": "declarado"}}
    assert extension[FINGERPRINT_EXTENSION_KEY]["components"]["runtime_version"]["status"] == "UNAVAILABLE"
    assert effective_backend(larm_fp) == "VULKAN"
    assert effective_backend({"components": {"backend": {"value": "X", "status": "UNAVAILABLE"}}}) is None
    assert effective_backend(None) is None


def test_disabled_bridge_publishes_nothing(tmp_path: Path) -> None:
    config: BrokerConfig = broker_config(tmp_path, enabled=False)
    fake = FakeLarm()
    provider = bridge(config, fake, FakeRuntime(fake))
    assert asyncio.run(provider.models()) == []
    assert provider.capabilities() == {"local_runtime_resolver": False, "larm_contract_version": None}
    with pytest.raises(ProviderError) as raised:
        generate(provider, fake)
    assert raised.value.code == "PROVIDER_UNAVAILABLE"
    assert fake.requests == []


def test_local_only_tasks_accept_larm_targets(tmp_path: Path) -> None:
    from app.schemas import TaskCreateRequest

    for classification in ("local_only", "confidential"):
        request = TaskCreateRequest.model_validate({
            "idempotency_key": f"lo-{classification}",
            "content": {"prompt": "hola"},
            "risk": {"data_classification": classification},
            "model_requirements": {
                "target_model": {"provider": "larm", "deployment": "local", "model": uuid7()},
                "allowed_providers": ["larm", "deepseek"],
            },
        })
        assert request.model_requirements.cloud_allowed is False
        assert request.model_requirements.allowed_providers == ["larm"]


def test_non_loopback_lease_endpoint_never_receives_the_prompt(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.endpoint_base = "http://10.0.0.7:18432/v1"
    runtime = FakeRuntime(fake)
    provider = bridge(config, fake, runtime)
    with pytest.raises(ProviderError) as raised:
        generate(provider, fake)
    assert raised.value.code == "CLOUD_NOT_ALLOWED"
    assert runtime.calls == []
    assert fake.released == [next(iter(fake.leases))]

