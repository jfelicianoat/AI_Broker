"""Contract tests del puente Broker–LARM (prompt P107, contrato LARM 1.0).

Oráculos del prompt: LARM off/offline, scope insuficiente, variante
desconocida, lease expirada, timeout de carga, backend efectivo distinto,
reintento idempotente, rotación del token, cancelación y reinicio del broker,
y ningún fallback cuando `fallback_allowed: false`.

El LARM de estos tests es un doble que valida peticiones y respuestas contra
los schemas del paquete LARM v3 (tests/larm_fake.py). Esto da CODE_PASS; la
prueba real contra LARM e inferencia real la hace tools/p107_real.py.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import execution_fingerprint, invocation_context
from app.artifacts import ArtifactStore
from app.config import (
    BrokerConfig,
    LarmConfig,
    OllamaConfig,
    PersistenceConfig,
    ProcessingConfig,
    ProvidersConfig,
)
from app.db import Database
from app.larm import (
    FINGERPRINT_EXTENSION_KEY,
    LarmClient,
    LarmError,
    LarmLeaseJournal,
    LocalRuntimeResolver,
    default_credentials_pipe,
    lease_idempotency_key,
)
from app.main import create_app
from app.providers.base import ProviderError
from app.providers.larm import LarmProvider, provider_error
from app.schemas import TaskCreateRequest
from tests.larm_fake import FIXTURES, SCHEMA_SHA256, FakeLarm, FakeRuntime, FakeVariant, uuid7

TOKEN_ENV = "LARM_BROKER_TOKEN_TEST"


@pytest.fixture(autouse=True)
def broker_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(TOKEN_ENV, "broker-token-1")
    # Otros tests abren invocaciones en el hilo principal: sin esto, una
    # llamada directa al provider heredaría su task_id:invocation_id.
    invocation_context.reset()
    return "broker-token-1"


def larm_settings(**overrides: Any) -> LarmConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "api_key_env": TOKEN_ENV,
        # Ranura que no existe: el test no puede leer una credencial real.
        "keyring_service": "larm-contract-tests-no-such-service",
        "job_poll_seconds": 0.02,
        "discovery_interval_seconds": 60,
    }
    values.update(overrides)
    return LarmConfig(**values)


def broker_config(tmp_path: Path, **larm_overrides: Any) -> BrokerConfig:
    return BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False),
        providers=ProvidersConfig(ollama=OllamaConfig(enabled=False)),
        larm=larm_settings(**larm_overrides),
    )


def bridge(config: BrokerConfig, fake: FakeLarm, runtime: FakeRuntime,
           journal: LarmLeaseJournal | None = None) -> LarmProvider:
    resolver = LocalRuntimeResolver(
        config.larm,
        client=LarmClient(config.larm, transport=fake.transport()),
        journal=journal,
        token_fetcher=fake.token_fetcher,
    )
    return LarmProvider(config, resolver=resolver, endpoint_transport=runtime.transport())


def install(app: Any, config: BrokerConfig, fake: FakeLarm, runtime: FakeRuntime) -> LarmProvider:
    provider = app.state.provider
    provider.larm = bridge(config, fake, runtime, journal=provider.larm.resolver.journal)
    return provider.larm


def task_payload(model: str, *, key: str, fallback_allowed: bool = False, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "idempotency_key": key,
        "content": {"prompt": "Di hola en una palabra."},
        "model_requirements": {
            "target_model": {"provider": "larm", "deployment": "local", "model": model},
            "fallback_allowed": fallback_allowed,
        },
        "generation": {"temperature": 0, "seed": 42, "max_output_tokens": 32},
    }
    payload.update(extra)
    return payload


def chat_request(model: str, *, fallback_allowed: bool = False) -> TaskCreateRequest:
    return TaskCreateRequest.model_validate(task_payload(model, key="direct", fallback_allowed=fallback_allowed))


def run_task(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    accepted = client.post("/api/v1/tasks", json=payload)
    assert accepted.status_code == 202, accepted.text
    task_id = accepted.json()["task_id"]
    client.post("/api/v1/dispatcher/tick")
    return client.get(f"/api/v1/tasks/{task_id}").json()


def test_vendored_schemas_are_the_larm_v3_package() -> None:
    for name, expected in SCHEMA_SHA256.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected


# --- LARM apagado / caído ---------------------------------------------------------


def test_bridge_off_announces_nothing_and_refuses_larm_targets(tmp_path: Path) -> None:
    config = broker_config(tmp_path, enabled=False)
    fake = FakeLarm()
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.artifacts = ArtifactStore(tmp_path / "tasks")
        install(client.app, config, fake, FakeRuntime(fake))
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["local_runtime_resolver"] is False
        assert capabilities["larm_contract_version"] is None
        assert capabilities["contract_version"] == "2.12"
        state = run_task(client, task_payload(fake.variant.variant_id, key="larm-off"))
    assert state["status"] == "failed"
    assert state["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert state["error"]["retryable"] is False
    # Apagado significa apagado: ni catálogo ni leases.
    assert fake.requests == []


def test_larm_offline_is_retryable_and_never_falls_back_when_forbidden(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.offline = True
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.artifacts = ArtifactStore(tmp_path / "tasks")
        install(client.app, config, fake, FakeRuntime(fake))
        assert client.get("/api/v1/capabilities").json()["local_runtime_resolver"] is False
        state = run_task(client, task_payload(fake.variant.variant_id, key="larm-down"))
    assert state["status"] == "failed"
    assert state["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert state["error"]["retryable"] is True
    assert state["error"]["details"]["larm_error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert not any(path == "/api/v1/leases" for _, path, _, _ in fake.requests)


def test_capabilities_announce_bridge_only_with_live_catalog(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    with TestClient(create_app(config)) as client:
        install(client.app, config, fake, FakeRuntime(fake))
        capabilities = client.get("/api/v1/capabilities").json()
        models = client.get("/api/v1/models").json()
    assert capabilities["local_runtime_resolver"] is True
    assert capabilities["larm_contract_version"] == "1.0"
    # El puente es aditivo: el contrato del broker no cambia por LARM.
    assert capabilities["contract_version"] == "2.12"
    larm_models = [item for item in models["models"] if item["provider"] == "larm"]
    assert [item["name"] for item in larm_models] == [fake.variant.variant_id]
    assert larm_models[0]["deployment"] == "local"
    assert larm_models[0]["capabilities"] == ["completion"]


# --- camino feliz ---------------------------------------------------------------------


def test_exact_target_is_served_under_lease_renewed_and_released(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    runtime = FakeRuntime(fake, delay_seconds=1.6)
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.artifacts = ArtifactStore(tmp_path / "tasks")
        install(client.app, config, fake, runtime)
        state = run_task(client, task_payload(fake.variant.variant_id, key="larm-happy"))
        task_id = state["task_id"]
        invocations = client.get(f"/api/v1/tasks/{task_id}/invocations").json()
        artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()
        finals = [item for item in artifacts["items"] if item.get("final")]
        assert len(finals) == 1 and finals[0]["available"] is True
        downloaded = client.get(finals[0]["download_url"])
    assert state["status"] == "completed", state.get("error")
    summary = state["execution_summary"]
    expected = {"provider": "larm", "deployment": "local", "model": fake.variant.variant_id}
    assert {k: summary["served_by"][k] for k in expected} == expected
    assert summary["fallback_used"] is False
    # Una sola admisión, renovada mientras la inferencia seguía, y liberada.
    assert fake.admissions == 1
    lease_id = next(iter(fake.leases))
    assert fake.renewals and set(fake.renewals) == {lease_id}
    assert fake.released == [lease_id]
    # El broker transmite los requisitos de la petición para la admisión.
    requirements = fake.leases[lease_id]["_requirements"]
    assert requirements["capability"] == "text.generate"
    assert requirements["max_output_tokens"] == 32
    assert requirements["context_tokens"] and requirements["context_tokens"] > 0
    # Clave idempotente estable task_id:invocation_id.
    invocation = invocations["items"][0]
    assert fake.leases[lease_id]["request_id"] == f"{task_id}:{invocation['invocation_id']}"
    # Huella: componentes conocidos + extensión versionada con la huella efectiva.
    fp = invocation["execution_fingerprint"]
    assert fp["components"]["runtime"] == {"value": "llama.cpp", "status": "verificado"}
    assert fp["components"]["tokenizer"]["status"] == "declarado"
    assert fp["components"]["remote_model"]["value"] == "runtime-model-q4"
    extension = fp["extensions"][FINGERPRINT_EXTENSION_KEY]
    assert extension["source"] == "lease"
    assert extension["variant_id"] == fake.variant.variant_id
    assert extension["fingerprint_hash"] == fake.leases[lease_id]["_fingerprint"]["hash"]
    assert extension["components"]["backend"] == {"value": "VULKAN", "status": "VERIFIED"}
    assert extension["components"]["chat_template_hash"]["status"] == "UNAVAILABLE"
    # Artefacto final descargable con su hash.
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == finals[0]["sha256"]
    assert "respuesta LARM" in downloaded.text


def test_embedding_uses_embedding_capability(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.variants = [FakeVariant(capabilities=["embedding.create"])]
    runtime = FakeRuntime(fake)
    provider = bridge(config, fake, runtime)
    request = TaskCreateRequest.model_validate(
        task_payload(fake.variant.variant_id, key="emb", inference_kind="embedding",
                     output={"format": "json", "json_schema": {
                         "type": "object", "required": ["embedding"],
                         "properties": {"embedding": {"type": "array", "items": {"type": "number"}}},
                     }}),
    )
    output = asyncio.run(provider.embed(request, fake.variant.variant_id, "texto a vectorizar"))
    assert output.embedding == (0.1, 0.2, 0.3)
    lease = next(iter(fake.leases.values()))
    assert lease["_requirements"]["capability"] == "embedding.create"
    assert lease["_requirements"]["max_output_tokens"] is None
    assert runtime.calls[0]["path"].endswith("/embeddings")
    assert fake.released == [lease["id"]]


def test_endpoint_credential_travels_only_from_the_local_channel(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.credential_required = True
    runtime = FakeRuntime(fake)
    provider = bridge(config, fake, runtime)
    output = asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert output.content == "respuesta LARM"
    # FakeRuntime ya exige el token de ESA lease; además nunca es el del broker.
    assert runtime.calls[0]["headers"]["authorization"] != "Bearer broker-token-1"
    # Y el token no se queda en la lease liberada.
    assert not fake.endpoints


# --- negativos del contrato -----------------------------------------------------------


def test_unknown_variant_and_aliases_are_rejected_without_lease(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.artifacts = ArtifactStore(tmp_path / "tasks")
        install(client.app, config, fake, FakeRuntime(fake))
        unknown = run_task(client, task_payload(uuid7(), key="larm-unknown"))
        alias = run_task(client, task_payload("qwen3:8b", key="larm-alias"))
    for state in (unknown, alias):
        assert state["status"] == "failed"
        assert state["error"]["code"] == "MODEL_UNAVAILABLE"
        assert state["error"]["retryable"] is False
    assert fake.admissions == 0


def test_insufficient_scope_is_a_credentials_error(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.forbid_leases = True
    provider = bridge(config, fake, FakeRuntime(fake))
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert raised.value.code == "CREDENTIALS_UNAVAILABLE"
    assert raised.value.retryable is False
    assert raised.value.details["larm_error"]["code"] == "FORBIDDEN"


def test_memory_pressure_waits_and_absolute_shortage_fails(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.resource_unavailable = True
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.artifacts = ArtifactStore(tmp_path / "tasks")
        install(client.app, config, fake, FakeRuntime(fake))
        waiting = run_task(client, task_payload(fake.variant.variant_id, key="larm-wait"))
    assert waiting["status"] == "waiting_for_memory"
    provider = bridge(config, fake, FakeRuntime(fake))
    fake.resource_unavailable = False
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert raised.value.code == "VRAM_MODEL_TOO_LARGE"
    assert raised.value.retryable is False
    assert raised.value.details["larm_error"]["evidence_refs"]


def test_lost_lease_cuts_the_inference_and_releases(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.renew_fails_with = "INVALID_STATE_TRANSITION"
    runtime = FakeRuntime(fake, delay_seconds=5)
    provider = bridge(config, fake, runtime)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert raised.value.code == "PROVIDER_UNAVAILABLE"
    assert raised.value.retryable is True
    assert raised.value.details["larm_error"]["code"] == "LEASE_LOST"
    assert fake.released == [next(iter(fake.leases))]


def test_load_timeout_gives_up_and_releases_the_late_lease(tmp_path: Path) -> None:
    config = broker_config(tmp_path, lease_acquire_timeout_seconds=0.2)
    fake = FakeLarm()
    fake.async_jobs = True
    fake.job_never_completes = True
    runtime = FakeRuntime(fake)
    provider = bridge(config, fake, runtime)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert raised.value.code == "PROVIDER_UNAVAILABLE"
    assert raised.value.retryable is True
    assert raised.value.details["larm_error"]["code"] == "LEASE_ACQUIRE_TIMEOUT"
    # La lease que LARM llegó a crear no se queda reservando memoria.
    assert fake.released == [next(iter(fake.leases))]
    assert runtime.calls == []


def test_async_admission_is_polled_until_active(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.async_jobs = True
    runtime = FakeRuntime(fake)
    provider = bridge(config, fake, runtime)
    output = asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert output.content == "respuesta LARM"
    assert any(path.startswith("/api/v1/jobs/") for _, path, _, _ in fake.requests)


def test_backend_mismatch_is_rejected_before_calling_the_runtime(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.serve_backend = "CPU"
    runtime = FakeRuntime(fake)
    provider = bridge(config, fake, runtime)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert raised.value.code == "MODEL_CAPABILITY_MISMATCH"
    assert raised.value.details["larm_error"]["code"] == "BACKEND_MISMATCH"
    assert runtime.calls == []
    assert fake.released == [next(iter(fake.leases))]


def test_backend_fallback_needs_larm_and_client_permission(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.variants = [FakeVariant(allow_backend_fallback=True)]
    fake.serve_backend = "CPU"
    provider = bridge(config, fake, FakeRuntime(fake))
    # El cliente prohibió el fallback: la restricción viaja en false aunque
    # LARM lo permitiera, y el backend distinto se rechaza.
    with pytest.raises(ProviderError):
        asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    first = next(iter(fake.leases.values()))
    assert first["constraints"]["allow_backend_fallback"] is False
    output = asyncio.run(provider.generate(
        chat_request(fake.variant.variant_id, fallback_allowed=True), fake.variant.variant_id, "hola",
    ))
    assert output.content == "respuesta LARM"


def test_lost_lease_response_is_retried_with_the_same_key(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    fake.drop_first_lease_response = True
    provider = bridge(config, fake, FakeRuntime(fake))

    async def scenario() -> Any:
        invocation_context.bind("task_1", "inv_1")
        return await provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola")

    output = asyncio.run(scenario())
    assert output.content == "respuesta LARM"
    keys = [headers["idempotency-key"] for method, path, headers, _ in fake.requests
            if method == "POST" and path == "/api/v1/leases"]
    assert keys == ["task_1:inv_1", "task_1:inv_1"]
    # Dos POST, una sola lease: el reintento no admite una segunda.
    assert fake.admissions == 1 and len(fake.leases) == 1


def test_token_rotation_is_picked_up_without_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    provider = bridge(config, fake, FakeRuntime(fake))
    asyncio.run(provider.models())
    monkeypatch.setenv(TOKEN_ENV, "broker-token-2")
    output = asyncio.run(provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"))
    assert output.content == "respuesta LARM"
    tokens = [headers["authorization"] for _, _, headers, _ in fake.requests]
    assert "Bearer broker-token-1" in tokens and tokens[-1] == "Bearer broker-token-2"


def test_revoked_token_is_not_retried_in_a_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm(token_env="LARM_SERVER_SIDE_TOKEN")
    monkeypatch.setenv("LARM_SERVER_SIDE_TOKEN", "otro-token")
    client = LarmClient(config.larm, transport=fake.transport())
    with pytest.raises(LarmError) as raised:
        asyncio.run(client.catalog())
    assert raised.value.code == "AUTH_REQUIRED"
    assert len(fake.requests) == 1
    assert provider_error(raised.value, "x").code == "CREDENTIALS_UNAVAILABLE"


def test_cancellation_releases_the_lease(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    runtime = FakeRuntime(fake, delay_seconds=30)
    provider = bridge(config, fake, runtime)

    async def scenario() -> None:
        task = asyncio.create_task(
            provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"),
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert fake.released == [next(iter(fake.leases))]


def test_broker_restart_releases_leases_left_open(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    db = Database(tmp_path / "broker.db")
    db.init_schema()
    journal = LarmLeaseJournal(db)
    provider = bridge(config, fake, FakeRuntime(fake, delay_seconds=30), journal=journal)

    async def crash_mid_inference() -> None:
        invocation_context.bind("task_crash", "inv_crash")
        task = asyncio.create_task(
            provider.generate(chat_request(fake.variant.variant_id), fake.variant.variant_id, "hola"),
        )
        while not fake.leases or journal.open_leases() == [] or journal.open_leases()[0]["state"] != "active":
            await asyncio.sleep(0.01)
        # Muerte abrupta: nadie llega a liberar.
        provider.resolver.release_lease = _never  # type: ignore[method-assign]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(crash_mid_inference())
    lease_id = next(iter(fake.leases))
    assert fake.released == []
    assert journal.by_invocation("inv_crash")["lease_id"] == lease_id
    db.close()

    # Nuevo proceso del broker: diario leído de disco, LARM sigue vivo.
    restarted = bridge(config, fake, FakeRuntime(fake), journal=LarmLeaseJournal(Database(tmp_path / "broker.db")))
    result = asyncio.run(restarted.resolver.reconcile())
    assert result == {"released": 1, "failed": 0}
    assert fake.released == [lease_id]
    assert restarted.resolver.journal.open_leases() == []


async def _never(*_: Any, **__: Any) -> None:
    return None


def test_startup_reconciliation_runs_from_the_app_lifespan(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    db = Database(tmp_path / "broker.db")
    db.init_schema()
    journal = LarmLeaseJournal(db)
    provider = bridge(config, fake, FakeRuntime(fake))
    lease = fake._admit(fake.variant, {
        "owner_id": "ai-broker", "request_id": "task_x:inv_x", "ttl_seconds": 120,
        "constraints": fake.variant.catalog_item()["constraints"], "resource_requirements": None,
    })
    journal.requested("task_x:inv_x", "task_x", "inv_x", fake.variant.variant_id)
    journal.update("task_x:inv_x", state="active", lease={"id": lease["id"], "generation": 3, "row_version": 1})
    db.close()
    app = create_app(config)
    app.state.provider.larm = bridge(
        config, fake, FakeRuntime(fake), journal=app.state.provider.larm.resolver.journal,
    )
    with TestClient(app):
        pass
    assert fake.released == [lease["id"]]
    del provider


def test_mixture_and_auto_routing_never_pick_larm_variants(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    provider = bridge(config, fake, FakeRuntime(fake))
    from app.providers.routing import RoutedModelProvider

    routed = RoutedModelProvider(config, custom={}, larm=provider)
    request = TaskCreateRequest.model_validate({"idempotency_key": "plain", "content": {"prompt": "hola"}})
    catalog, _, context = asyncio.run(routed.eligible_catalog(request))
    assert all(item["provider"] != "larm" for item in catalog + context)
    targeted = asyncio.run(routed.eligible_catalog(chat_request(fake.variant.variant_id)))
    assert [item["name"] for item in targeted[2]] == [fake.variant.variant_id]


# --- piezas puras ---------------------------------------------------------------------


def test_idempotency_key_shape() -> None:
    assert lease_idempotency_key("task_a", "inv_b") == "task_a:inv_b"
    adhoc = lease_idempotency_key(None, None)
    assert adhoc.startswith("adhoc:") and adhoc != lease_idempotency_key(None, None)
    long_key = lease_idempotency_key("t" * 200, "i" * 200)
    assert len(long_key) == 240


def test_credentials_pipe_name_is_derived_from_the_sid() -> None:
    name = default_credentials_pipe("S-1-5-21-1-2-3-1001")
    digest = hashlib.sha256(b"S-1-5-21-1-2-3-1001").hexdigest()[:16]
    assert name == rf"\\.\pipe\LARM-{digest}-credentials"
    assert default_credentials_pipe(None) is None


def test_fingerprint_without_extensions_keeps_its_hash() -> None:
    components = {"provider": execution_fingerprint.component("ollama", "declarado")}
    plain = execution_fingerprint.build(components)
    assert plain["hash"] == execution_fingerprint.canonical_hash(plain["components"])
    assert "extensions" not in plain
    extended = execution_fingerprint.build(components, {"larm.v1": {"variant_id": "x"}})
    assert extended["hash"] != plain["hash"]
    other = execution_fingerprint.build(components, {"larm.v1": {"variant_id": "y"}})
    assert other["hash"] != extended["hash"]
    assert execution_fingerprint.changed_components(extended, other) == ["extensions.larm.v1"]


def test_lease_request_body_never_relaxes_catalog_constraints(tmp_path: Path) -> None:
    config = broker_config(tmp_path)
    fake = FakeLarm()
    resolver = LocalRuntimeResolver(config.larm, client=LarmClient(config.larm, transport=fake.transport()))
    entry = fake.variant.catalog_item()
    from app.larm import LeaseResourceRequirements

    body = resolver.lease_request_body(
        entry, LeaseResourceRequirements("text.generate", 100, 10),
        launch_profile_id=fake.launch_profile_id, request_id="t:i", fallback_allowed=True,
    )
    from tests.larm_fake import check

    check("LeaseRequest", body)
    assert body["constraints"]["runtime_version_id"] == fake.variant.runtime_version_id
    assert body["constraints"]["backend_id"] == fake.variant.backend_id
    assert body["ttl_seconds"] == 120 and body["owner_id"] == "ai-broker"


def test_config_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError):
        LarmConfig(endpoint="http://192.168.1.20:8790")
    with pytest.raises(ValueError):
        BrokerConfig.model_validate({"providers": {"custom": [{"id": "larm", "base_url": "http://x"}]}})
    assert LarmConfig().enabled is False
    assert LarmConfig().endpoint == "http://127.0.0.1:8790"


# --- canal de credenciales sobre un named pipe real (Windows) ---------------------


class _PipeServer:
    """Servidor de una sola conexión con el handshake del contrato LARM 04."""

    def __init__(self, name: str, *, expected_bearer: str, token: str, tamper_nonce: bool = False) -> None:
        import threading

        self.name = name
        self.expected_bearer = expected_bearer
        self.token = token
        self.tamper_nonce = tamper_nonce
        self.received: dict[str, Any] | None = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _serve(self) -> None:
        import ctypes
        import json as _json
        import secrets as _secrets
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ]
        kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        pipe_access_duplex, pipe_reject_remote = 0x3, 0x8
        handle = kernel32.CreateNamedPipeW(self.name, pipe_access_duplex, pipe_reject_remote, 1, 4096, 4096, 0, None)
        self.ready.set()
        kernel32.ConnectNamedPipe(handle, None)

        def write(payload: dict[str, Any]) -> None:
            data = (_json.dumps(payload) + "\n").encode()
            written = wintypes.DWORD()
            kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)

        try:
            nonce = _secrets.token_hex(32)
            write({"nonce": nonce, "expires_at": "2099-01-01T00:00:00.000Z"})
            raw = bytearray()
            buffer = ctypes.create_string_buffer(1)
            read = wintypes.DWORD()
            while not raw.endswith(b"\n") and len(raw) <= 4096:
                if not kernel32.ReadFile(handle, buffer, 1, ctypes.byref(read), None) or not read.value:
                    break
                raw.extend(buffer.raw[: read.value])
            self.received = _json.loads(raw)
            ok = (
                self.received.get("nonce") == nonce and not self.tamper_nonce
                and self.received.get("bearer_token") == self.expected_bearer
            )
            if ok:
                write({"request_id": self.received["request_id"], "token": self.token,
                       "expires_at": "2099-01-01T00:00:00.000Z"})
            else:
                write({"code": "FORBIDDEN", "message": "nonce o principal inválido", "retryable": False})
        finally:
            kernel32.CloseHandle(handle)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="named pipes de Windows")
def test_endpoint_token_over_a_real_named_pipe() -> None:
    from app.larm import fetch_endpoint_token_blocking

    name = rf"\\.\pipe\LARM-test-{uuid7()}-credentials"
    server = _PipeServer(name, expected_bearer="broker-token-1", token="endpoint-secret")
    reply = fetch_endpoint_token_blocking(
        name, lease_id="lease-1", generation=3, credential_reference_id="cred-1", bearer_token="broker-token-1",
    )
    server.thread.join(5)
    assert reply["token"] == "endpoint-secret"
    assert server.received is not None
    assert {k: server.received[k] for k in ("lease_id", "generation", "credential_reference_id")} == {
        "lease_id": "lease-1", "generation": 3, "credential_reference_id": "cred-1",
    }


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="named pipes de Windows")
def test_named_pipe_refusal_is_a_larm_error_without_token() -> None:
    from app.larm import fetch_endpoint_token_blocking

    name = rf"\\.\pipe\LARM-test-{uuid7()}-credentials"
    server = _PipeServer(name, expected_bearer="otro", token="no-debe-salir")
    with pytest.raises(LarmError) as raised:
        fetch_endpoint_token_blocking(
            name, lease_id="lease-1", generation=3, credential_reference_id="cred-1", bearer_token="broker-token-1",
        )
    server.thread.join(5)
    assert raised.value.code == "FORBIDDEN"
    assert "no-debe-salir" not in str(raised.value)
