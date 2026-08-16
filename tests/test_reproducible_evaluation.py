"""Contrato de evaluación reproducible: determinismo, huella, telemetría y
tráfico que no debe enseñar al broker.

Lo que se protege aquí, en una frase por bloque:

- Que pedir una semilla sirva de algo y que, cuando no sirve, se diga.
- Que dos respuestas del mismo NOMBRE de modelo se puedan distinguir cuando
  salieron de configuraciones distintas.
- Que un cliente de la API v1 pueda saber quién le respondió y a qué coste sin
  entrar por el panel de administración.
- Que medir un modelo no lo degrade en producción.
- Que `fallback_allowed=false` con `target_model` signifique de verdad "este
  modelo o error tipado".
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import execution_fingerprint as fp
from app.config import (
    BrokerConfig,
    DeepSeekConfig,
    ModelQuarantineConfig,
    OllamaConfig,
    PersistenceConfig,
    ProcessingConfig,
    ProvidersConfig,
    RoutingConfig,
)
from app.dashboard import DashboardQueryRepository
from app.db import Database
from app.main import create_app
from app.model_fingerprints import CHANGED, FIRST_SEEN, UNCHANGED, record_fingerprint
from app.model_quarantine import load_quarantine
from app.model_stats import ModelStats, load_model_stats
from app.providers import BootstrapModelProvider, OllamaProvider, ProviderError, RoutedModelProvider
from app.schemas import ModelReference, TaskCreateRequest
from app.shadow_probe import ShadowProbe, choose_aspirant

# --------------------------------------------------------------------------
# Utilidades compartidas
# --------------------------------------------------------------------------


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


class _CatalogStub:
    def __init__(self, models: list[dict]) -> None:
        self._models = models

    async def models(self) -> list[dict]:
        return self._models

    async def close(self) -> None:
        return None


def _entry(name: str, *, deployment: str = "local", provider: str = "ollama") -> dict:
    return {
        "name": name,
        "provider": provider,
        "deployment": deployment,
        "context_window": 100_000,
        "capabilities": ["completion"],
        "compatibility": "compatible",
    }


def _router(catalog: list[dict], **kwargs) -> RoutedModelProvider:
    return RoutedModelProvider(
        BrokerConfig(),
        ollama=_CatalogStub([item for item in catalog if item["provider"] == "ollama"]),
        deepseek=_CatalogStub([]),
        custom={
            provider: _CatalogStub([item for item in catalog if item["provider"] == provider])
            for provider in {item["provider"] for item in catalog}
            if provider != "ollama"
        },
        **kwargs,
    )


def _request(**overrides) -> TaskCreateRequest:
    payload: dict = {
        "idempotency_key": f"eval:{uuid4().hex}",
        "content": {"prompt": "hola"},
    }
    payload.update(overrides)
    return TaskCreateRequest.model_validate(payload)


def _stat(attempts: int) -> ModelStats:
    return ModelStats(attempts=attempts, successes=attempts, avg_latency_ms=1000.0, avg_cost_usd=0.0)


def _db_with_task(tmp_path: Path, name: str = "eval.db") -> Database:
    db = Database(tmp_path / name)
    db.init_schema()
    stamp = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO tasks (id, request_json, status, priority, created_at, updated_at) "
        "VALUES ('t', '{}', 'completed', 100, ?, ?)",
        (stamp, stamp),
    )
    return db


def _insert_invocation(
    db: Database,
    *,
    model: str,
    status: str,
    excluded: bool,
    latency_ms: float | None = 1000.0,
    cost_usd: float = 0.5,
    error_code: str | None = None,
    minutes_ago: float = 1,
    task_type: str = "prose",
) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    db.execute(
        "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, task_type, "
        "status, error_code, excluded_from_learning, tokens_input, tokens_output, cost_usd, "
        "latency_ms, created_at, updated_at) "
        "VALUES (?, 't', 'single', 'ollama', 'local', ?, ?, ?, ?, ?, 10, 20, ?, ?, ?, ?)",
        (
            f"inv_{uuid4().hex}", model, task_type, status, error_code, int(excluded),
            cost_usd, latency_ms, stamp, stamp,
        ),
    )


# --------------------------------------------------------------------------
# Cambio 1 — Determinismo: seed y top_p
# --------------------------------------------------------------------------


def _ollama_provider(handler, **catalog_extra) -> OllamaProvider:
    config = BrokerConfig(
        processing=ProcessingConfig(provider_mode="real"),
        providers=ProvidersConfig(ollama=OllamaConfig(), deepseek=DeepSeekConfig(enabled=False)),
    )
    return OllamaProvider(config, transport=httpx.MockTransport(handler))


def _ollama_handler(bodies: list[dict], *, template: str = "{{ .Prompt }}", digest: str = "sha256:aaa"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{
                "name": "qwen:latest", "size": 1024, "digest": digest,
                "details": {"family": "qwen", "parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
            }]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={
                "template": template,
                "model_info": {"qwen.context_length": 32768, "tokenizer.ggml.model": "gpt2"},
                "capabilities": ["completion"],
            })
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.1"})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/chat":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={
                "message": {"content": "respuesta"}, "prompt_eval_count": 3, "eval_count": 2,
            })
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return handler


def test_ollama_sends_seed_and_top_p_only_when_asked() -> None:
    async def scenario() -> None:
        bodies: list[dict] = []
        provider = _ollama_provider(_ollama_handler(bodies))
        try:
            await provider.generate(
                _request(generation={"temperature": 0, "seed": 42, "top_p": 0.9}), "qwen:latest", "hola",
            )
            await provider.generate(_request(), "qwen:latest", "hola")
        finally:
            await provider.close()

        with_seed, without_seed = bodies
        assert with_seed["options"]["seed"] == 42
        assert with_seed["options"]["top_p"] == 0.9
        # Una petición que no pide semilla no la lleva, ni con el valor por
        # defecto del proveedor: es lo que permite distinguir «no se pidió» de
        # «se pidió y se ignoró».
        assert "seed" not in without_seed["options"]
        assert "top_p" not in without_seed["options"]
        # `num_ctx` sí viaja siempre desde el dimensionado de contexto
        # (resources.gpu_context_sizing), que es posterior a estos dos campos y
        # se apaga por separado.
        assert set(without_seed["options"]) == {"temperature", "num_predict", "num_ctx"}

    asyncio.run(scenario())

def test_the_same_seed_produces_the_same_request_twice() -> None:
    """Lo único reproducible que el broker controla es lo que envía.

    Que la salida sea idéntica depende del runtime, y por eso no se afirma en
    ningún sitio: se declara qué se mandó (`seed_status`) y punto."""
    async def scenario() -> None:
        bodies: list[dict] = []
        provider = _ollama_provider(_ollama_handler(bodies))
        request = _request(generation={"temperature": 0, "seed": 7})
        try:
            first = await provider.generate(request, "qwen:latest", "hola")
            second = await provider.generate(request, "qwen:latest", "hola")
        finally:
            await provider.close()

        assert bodies[0] == bodies[1]
        for output in (first, second):
            assert output.generation["seed"] == 7
            assert output.generation["seed_status"] == "sent"

    asyncio.run(scenario())

def test_a_provider_that_cannot_carry_the_seed_says_so_and_does_not_fail() -> None:
    async def scenario() -> None:
        provider = BootstrapModelProvider()
        request = _request(generation={"seed": 99, "top_p": 0.5})

        output = await provider.propose(request, ModelReference(
            provider="ollama", deployment="bootstrap", model="bootstrap-single",
        ), 1)

        # La tarea NO falla por pedir algo que este proveedor no puede ejercer...
        assert output.content
        # ...pero quien mide se entera de que su resultado no es reproducible.
        assert output.generation["seed"] == 99
        assert output.generation["seed_status"] == "unsupported"
        assert output.generation["top_p_status"] == "unsupported"

    asyncio.run(scenario())

def test_top_p_out_of_range_is_a_contract_error(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={
            "idempotency_key": "eval:top-p",
            "content": {"prompt": "hola"},
            "generation": {"top_p": 1.5},
        })

    assert response.status_code == 422
    assert response.json()["code"] == "CONTRACT_VALIDATION_FAILED"


def test_generation_defaults_do_not_change(tmp_path: Path) -> None:
    """Los campos nuevos son opcionales y su ausencia es indistinguible del
    contrato anterior: es lo que permite que ChatyGPT no se entere."""
    payload = TaskCreateRequest.model_validate({
        "idempotency_key": "eval:defaults", "content": {"prompt": "hola"},
    })
    assert payload.generation.seed is None
    assert payload.generation.top_p is None
    assert payload.exclude_from_model_learning is False


# --------------------------------------------------------------------------
# Cambio 2 — Huella de ejecución
# --------------------------------------------------------------------------


def test_the_canonical_hash_is_stable_and_covers_every_component() -> None:
    components = {
        "provider": fp.component("ollama", fp.DECLARED),
        "digest": fp.component("sha256:abc", fp.VERIFIED),
    }
    first = fp.build(components)
    second = fp.build(dict(reversed(list(components.items()))))

    # Mismo contenido en otro orden: mismo hash. Es lo que hace que dos
    # arranques del broker con la misma configuración sean comparables.
    assert first["hash"] == second["hash"]
    # El conjunto de componentes es fijo: lo que no se sabe se declara.
    assert set(first["components"]) == set(fp.COMPONENT_NAMES)
    assert first["components"]["chat_template"] == {"value": None, "status": "no_disponible"}


def test_a_provider_without_any_data_yields_an_all_unavailable_fingerprint() -> None:
    empty = fp.build({})

    assert all(item["status"] == "no_disponible" for item in empty["components"].values())
    # Sigue teniendo hash: una huella incompleta es un resultado legítimo.
    assert empty["hash"]


def test_nothing_is_inferred_from_an_empty_value() -> None:
    """Un componente vacío no puede declararse verificado por descuido."""
    assert fp.component("", fp.VERIFIED)["status"] == "no_disponible"
    assert fp.component(None, fp.DECLARED)["status"] == "no_disponible"


def test_ollama_publishes_digest_template_tokenizer_and_runtime() -> None:
    async def scenario() -> None:
        provider = _ollama_provider(_ollama_handler([]))
        try:
            catalog = await provider.models()
        finally:
            await provider.close()

        components = catalog[0]["execution_fingerprint"]["components"]
        assert components["digest"] == {"value": "sha256:aaa", "status": "verificado"}
        assert components["quantization"]["value"] == "Q4_K_M"
        assert components["runtime_version"] == {"value": "0.5.1", "status": "verificado"}
        assert components["chat_template"]["status"] == "verificado"
        assert components["tokenizer"]["status"] == "verificado"
        # La plantilla se guarda hasheada, no entera.
        assert components["chat_template"]["value"] != "{{ .Prompt }}"

    asyncio.run(scenario())

def test_reinstalling_a_model_with_the_same_name_changes_its_fingerprint() -> None:
    async def scenario() -> None:
        before = _ollama_provider(_ollama_handler([], digest="sha256:viejo"))
        after = _ollama_provider(_ollama_handler([], digest="sha256:nuevo"))
        try:
            old = (await before.models())[0]["execution_fingerprint"]
            new = (await after.models())[0]["execution_fingerprint"]
        finally:
            await before.close()
            await after.close()

        assert old["hash"] != new["hash"]
        assert fp.changed_components(old, new) == ["digest"]

    asyncio.run(scenario())

def test_changing_only_the_chat_template_changes_the_fingerprint_but_not_the_digest() -> None:
    """El caso que motiva todo esto: mismos pesos, otra respuesta.

    Actualizar Ollama puede reescribir la plantilla con la que se formatean los
    mensajes sin tocar un byte del modelo, y el digest sigue siendo idéntico."""
    async def scenario() -> None:
        provider = _ollama_provider(_ollama_handler([], template="ANTIGUA {{ .Prompt }}"))
        try:
            old = (await provider.models())[0]["execution_fingerprint"]
            # Una plantilla que cambia sin cambiar el digest solo se ve al caducar
            # la caché de metadatos (o al reiniciar): aquí se fuerza esa caducidad.
            provider._catalog_cache.clear()
            provider._metadata_cache = {}
            provider.client = httpx.AsyncClient(
                base_url=provider.config.providers.ollama.base_url,
                transport=httpx.MockTransport(_ollama_handler([], template="NUEVA {{ .Prompt }}")),
            )
            new = (await provider.models())[0]["execution_fingerprint"]
        finally:
            await provider.close()

        assert old["hash"] != new["hash"]
        assert old["components"]["digest"] == new["components"]["digest"]
        assert fp.changed_components(old, new) == ["chat_template"]

    asyncio.run(scenario())

def test_the_change_signal_fires_once_and_names_what_changed(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path, "fingerprints.db")
    key = ("ollama", "local", "qwen:latest")
    old = fp.build({
        "digest": fp.component("sha256:abc", fp.VERIFIED),
        "chat_template": fp.component("plantilla-vieja", fp.VERIFIED),
    })
    new = fp.build({
        "digest": fp.component("sha256:abc", fp.VERIFIED),
        "chat_template": fp.component("plantilla-nueva", fp.VERIFIED),
    })

    # Descubrir un modelo por primera vez NO es un cambio: sin esto, el primer
    # arranque llenaría el historial de eventos falsos.
    assert record_fingerprint(db, key, old) == FIRST_SEEN
    assert record_fingerprint(db, key, old) == UNCHANGED
    assert record_fingerprint(db, key, new) == CHANGED
    # Y una vez registrado, no se repite en cada descubrimiento del catálogo.
    assert record_fingerprint(db, key, new) == UNCHANGED

    events = db.query_all(
        "SELECT payload_json FROM events WHERE event_type = 'model.fingerprint_changed'"
    )
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["changed"] == ["chat_template"]
    assert payload["model"] == "qwen:latest"
    db.close()


def test_a_remote_provider_adds_what_it_says_of_itself_when_it_answers(tmp_path: Path) -> None:
    """Capa C: `system_fingerprint` y el modelo devuelto no están en ningún
    catálogo, solo en la respuesta. Se suman a la huella guardada al arrancar
    sin pisar lo que ya se sabía."""
    from app.providers.base import ModelOutput, openai_observed_fingerprint
    from app.repository import TaskRepository

    db = _db_with_task(tmp_path, "remote.db")
    repository = TaskRepository(db)
    model = ModelReference(provider="nvidia", deployment="cloud", model="alias")
    declared = fp.build({"model": fp.component("alias", fp.DECLARED)})

    invocation_id = repository.start_invocation(
        "t", None, "single", model, "prose", None, fingerprint=declared,
    )
    repository.complete_invocation(invocation_id, "t", ModelOutput(
        "ok", 1, 1, 0.0, 10.0,
        fingerprint_observed=openai_observed_fingerprint({
            "system_fingerprint": "fp_44709", "model": "alias-2026-01-01",
        }),
    ))

    stored = repository.list_task_invocations("t").items[0]
    components = stored.execution_fingerprint.components
    assert components["system_fingerprint"].value == "fp_44709"
    assert components["remote_model"].value == "alias-2026-01-01"
    # Lo declarado al arrancar sigue ahí, y el hash ya no es el mismo.
    assert components["model"].value == "alias"
    assert stored.execution_fingerprint.hash != declared["hash"]
    db.close()


def test_the_router_records_each_model_once_and_again_when_it_changes() -> None:
    def scenario(entry: dict, recorded: list) -> None:
        router = RoutedModelProvider(
            BrokerConfig(),
            ollama=_CatalogStub([entry]),
            deepseek=_CatalogStub([]),
            fingerprint_recorder=lambda key, current: recorded.append((key, current["hash"])),
        )
        asyncio.run(router.models())
        asyncio.run(router.models())

    recorded: list = []
    entry = _entry("qwen:latest")
    entry["execution_fingerprint"] = fp.build({"digest": fp.component("sha256:a", fp.VERIFIED)})
    scenario(entry, recorded)
    # Dos catálogos, un solo registro: `models()` se llama en cada selección y
    # no puede pagar una consulta por modelo cada vez.
    assert len(recorded) == 1

    entry["execution_fingerprint"] = fp.build({"digest": fp.component("sha256:b", fp.VERIFIED)})
    scenario(entry, recorded)
    assert len(recorded) == 2
    assert recorded[0][1] != recorded[1][1]


def test_two_invocations_across_a_change_keep_different_fingerprints(tmp_path: Path) -> None:
    from app.providers.base import ModelOutput
    from app.repository import TaskRepository

    db = _db_with_task(tmp_path, "invocations.db")
    repository = TaskRepository(db)
    model = ModelReference(provider="ollama", deployment="local", model="qwen:latest")
    old = fp.build({"digest": fp.component("sha256:viejo", fp.VERIFIED)})
    new = fp.build({"digest": fp.component("sha256:nuevo", fp.VERIFIED)})

    first = repository.start_invocation(
        "t", None, "single", model, "prose", None, fingerprint=old,
    )
    second = repository.start_invocation(
        "t", None, "single", model, "prose", None, fingerprint=new,
    )
    for invocation_id in (first, second):
        repository.complete_invocation(invocation_id, "t", ModelOutput("ok", 1, 1, 0.0, 10.0))

    stored = repository.list_task_invocations("t")
    hashes = [item.execution_fingerprint.hash for item in stored.items]
    assert hashes == [old["hash"], new["hash"]]
    db.close()


def test_the_catalog_publishes_the_fingerprint_of_every_model(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        availability = client.get("/api/v1/models/availability").json()
        context = client.get(
            "/api/v1/models/context",
            params={"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single"},
        ).json()

    assert availability["items"][0]["execution_fingerprint"]["hash"]
    fingerprint = context["execution_fingerprint"]
    assert fingerprint["components"]["model"]["status"] == "declarado"
    # Un proveedor que no ejecuta nada no puede afirmar nada de la capa técnica.
    assert fingerprint["components"]["chat_template"]["status"] == "no_disponible"


# --------------------------------------------------------------------------
# Cambio 3 — Telemetría de invocación en el contrato v1
# --------------------------------------------------------------------------


def test_a_single_task_reports_one_invocation_with_its_numbers(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "eval:single", "content": {"prompt": "Hola"},
        }).json()
        client.post("/api/v1/dispatcher/tick")

        invocations = client.get(f"/api/v1/tasks/{created['task_id']}/invocations").json()
        state = client.get(f"/api/v1/tasks/{created['task_id']}").json()

    assert len(invocations["items"]) == 1
    item = invocations["items"][0]
    assert item["status"] == "completed"
    assert item["model"]["model"]
    assert item["tokens_input"] > 0 and item["tokens_output"] > 0
    assert item["latency_ms"] is not None
    assert item["cost_usd"] is not None
    # Los parámetros efectivos viajan con la invocación (Cambio 1).
    assert item["generation"]["max_output_tokens"] == 4000
    assert state["execution_summary"]["served_by"]["model"] == item["model"]["model"]


def test_a_mixture_reports_one_invocation_per_proposer_plus_the_arbiter(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "eval:mixture",
            "content": {"prompt": "Consenso"},
            "execution": {"strategy": "mixture_of_agents", "preset": "fast", "max_proposers": 3},
        }).json()
        client.post("/api/v1/dispatcher/tick")

        items = client.get(f"/api/v1/tasks/{created['task_id']}/invocations").json()["items"]

    roles = [item["role"] for item in items]
    assert roles.count("arbiter") == 1
    assert len([role for role in roles if role != "arbiter"]) >= 2


def test_a_fallback_is_declared_in_the_task_state(tmp_path: Path, monkeypatch) -> None:
    async def select_something_else(self, request, count, roles):
        return [
            ModelReference(
                provider="ollama", deployment="bootstrap", model="otro-modelo", role=roles[index],
            )
            for index in range(count)
        ]

    monkeypatch.setattr(BootstrapModelProvider, "select", select_something_else)
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "eval:fallback",
            "content": {"prompt": "Hola"},
            "model_requirements": {
                "target_model": {
                    "provider": "ollama", "deployment": "bootstrap", "model": "el-que-pedi",
                },
                "fallback_allowed": True,
            },
        }).json()
        client.post("/api/v1/dispatcher/tick")
        state = client.get(f"/api/v1/tasks/{created['task_id']}").json()

    summary = state["execution_summary"]
    assert summary["fallback_used"] is True
    assert summary["requested_model"]["model"] == "el-que-pedi"
    assert summary["served_by"]["model"] == "otro-modelo"


def test_invocations_of_an_unknown_task_fail_like_the_rest_of_the_contract(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        missing = client.get("/api/v1/tasks/task_inexistente/invocations")
        reference = client.get("/api/v1/tasks/task_inexistente")

    assert missing.status_code == reference.status_code == 404
    assert missing.json() == reference.json()


# --------------------------------------------------------------------------
# Cambio 4 — El tráfico que no debe enseñar al broker
# --------------------------------------------------------------------------


def test_excluded_failures_do_not_move_the_success_rate(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path, "learning.db")
    _insert_invocation(db, model="qwen", status="completed", excluded=False, latency_ms=100)
    baseline = load_model_stats(db, window_days=7)[("ollama", "local", "qwen", "prose")]

    for _ in range(5):
        _insert_invocation(
            db, model="qwen", status="failed", excluded=True,
            latency_ms=90_000, error_code="INVALID_PROVIDER_RESPONSE",
        )
    after = load_model_stats(db, window_days=7)[("ollama", "local", "qwen", "prose")]

    assert (after.attempts, after.successes) == (baseline.attempts, baseline.successes)
    assert after.success_rate == baseline.success_rate
    assert after.avg_latency_ms == baseline.avg_latency_ms
    db.close()


def test_excluded_failures_do_not_bring_a_model_closer_to_quarantine(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path, "quarantine.db")
    for index in range(4):
        _insert_invocation(
            db, model="roto:bf16", status="failed", excluded=True,
            error_code="INVALID_PROVIDER_RESPONSE", minutes_ago=10 - index,
        )

    assert load_quarantine(db, ModelQuarantineConfig()) == {}

    # Con las mismas dos invocaciones SIN excluir, el modelo sí sale de la
    # rotación: lo que cambia es la procedencia, no la regla.
    for index in range(2):
        _insert_invocation(
            db, model="roto:bf16", status="failed", excluded=False,
            error_code="INVALID_PROVIDER_RESPONSE", minutes_ago=2 - index,
        )
    assert ("ollama", "local", "roto:bf16") in load_quarantine(db, ModelQuarantineConfig())
    db.close()


def test_excluded_traffic_is_still_counted_in_cost_and_usage(tmp_path: Path) -> None:
    """Excluido del aprendizaje, no de la contabilidad: ese tráfico ocupa la
    máquina y se paga, así que tiene que verse."""
    db = _db_with_task(tmp_path, "usage.db")
    _insert_invocation(db, model="qwen", status="completed", excluded=True, cost_usd=2.5)

    usage = DashboardQueryRepository(db).usage(datetime.now(timezone.utc).strftime("%Y-%m"))

    assert usage.providers["ollama"]["cost_usd"] == 2.5
    assert usage.providers["ollama"]["invocations"] == 1
    db.close()


def test_a_model_with_only_excluded_evidence_looks_cold_not_reliable(tmp_path: Path) -> None:
    db = _db_with_task(tmp_path, "cold.db")
    for _ in range(20):
        _insert_invocation(db, model="solo-medido", status="completed", excluded=True)

    assert load_model_stats(db, window_days=7) == {}
    db.close()


def test_the_shadow_probe_does_not_treat_excluded_evidence_as_measurement() -> None:
    # El aspirante tiene 20 invocaciones... todas excluidas, así que para
    # `load_model_stats` no existe y sigue siendo un modelo sin medir.
    chosen = choose_aspirant(
        [_entry("sin-evidencia-util")], {}, "prose", min_invocations=3, allow_local=True,
    )
    assert chosen is not None and chosen["name"] == "sin-evidencia-util"

    # Y con evidencia de verdad, deja de ser candidato.
    assert choose_aspirant(
        [_entry("medido")],
        {("ollama", "local", "medido", "prose"): _stat(5)},
        "prose", min_invocations=3, allow_local=True,
    ) is None


def test_an_excluded_task_does_not_trigger_a_shadow_probe() -> None:
    """Un sondeo cuya medida no se va a contar es VRAM y tiempo tirados, y con
    el prompt de quien está midiendo el aspirante fallaría por un defecto que no
    es suyo."""
    probe = ShadowProbe(BootstrapModelProvider(), BrokerConfig())

    assert probe._is_probeable(_request(exclude_from_model_learning=True)) is False


def test_the_marker_travels_from_the_request_to_the_invocation(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "eval:excluded",
            "content": {"prompt": "Prompt adversarial"},
            "exclude_from_model_learning": True,
        }).json()
        client.post("/api/v1/dispatcher/tick")
        items = client.get(f"/api/v1/tasks/{created['task_id']}/invocations").json()["items"]
        # El panel lo enseña: nadie debe leer un indicador de fiabilidad sin
        # saber qué se contó.
        listed = client.get("/api/v1/dashboard/tasks").json()["items"]

    assert items and all(item["excluded_from_model_learning"] for item in items)
    assert listed[0]["excluded_from_model_learning"] is True


def test_capabilities_announce_the_new_contract(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        body = client.get("/api/v1/capabilities").json()

    assert body["generation_determinism"] is True
    assert body["exclude_from_model_learning"] is True
    assert body["invocation_telemetry"] is True
    assert body["execution_fingerprint"] is True


# --------------------------------------------------------------------------
# Cambio 5 — La fijación dura es dura
# --------------------------------------------------------------------------


def test_a_missing_target_without_fallback_is_an_error_never_another_model() -> None:
    async def scenario() -> None:
        router = _router([_entry("el-que-si-esta")])
        request = _request(model_requirements={
            "target_model": {"provider": "ollama", "deployment": "local", "model": "el-que-no-esta"},
            "fallback_allowed": False,
        })

        with pytest.raises(ProviderError) as error:
            await router.select(request, 1, ["single"])
        assert error.value.code == "MODEL_UNAVAILABLE"

    asyncio.run(scenario())

def test_a_quarantined_target_without_fallback_is_an_error_never_another_model() -> None:
    async def scenario() -> None:
        quarantined = ("ollama", "local", "apartado")

        class _Entry:
            reason = "2 fallos seguidos"

        router = _router(
            [_entry("apartado"), _entry("sano")],
            quarantine_loader=lambda: {quarantined: _Entry()},
        )
        request = _request(model_requirements={
            "target_model": {"provider": "ollama", "deployment": "local", "model": "apartado"},
            "fallback_allowed": False,
        })

        with pytest.raises(ProviderError) as error:
            await router.select(request, 1, ["single"])
        assert error.value.code == "MODEL_UNAVAILABLE"

    asyncio.run(scenario())

def test_the_same_case_with_fallback_allowed_does_substitute() -> None:
    async def scenario() -> None:
        router = _router([_entry("el-que-si-esta")])
        request = _request(model_requirements={
            "target_model": {"provider": "ollama", "deployment": "local", "model": "el-que-no-esta"},
            "fallback_allowed": True,
        })

        selected = await router.select(request, 1, ["single"])

        assert selected[0].model == "el-que-si-esta"

    asyncio.run(scenario())

def test_a_homonym_in_two_deployments_resolves_by_full_identity() -> None:
    async def scenario() -> None:
        router = _router([
            _entry("gemma:27b", deployment="local"),
            _entry("gemma:27b", deployment="cloud"),
        ])
        request = _request(model_requirements={
            "target_model": {"provider": "ollama", "deployment": "cloud", "model": "gemma:27b"},
            "fallback_allowed": False,
            "cloud_allowed": True,
        })

        selected = await router.select(request, 1, ["single"])

        assert (selected[0].deployment, selected[0].model) == ("cloud", "gemma:27b")

    asyncio.run(scenario())

def test_a_target_that_does_not_fit_the_context_does_not_silently_switch() -> None:
    """El otro camino por el que un target podría perderse: cabe en el catálogo
    pero no en el contexto. Sin fallback, también es error tipado."""
    async def scenario() -> None:
        small = _entry("pequeno")
        small["context_window"] = 10
        router = _router([small, _entry("grande")])
        request = _request(
            content={"prompt": "x" * 5000},
            model_requirements={
                "target_model": {"provider": "ollama", "deployment": "local", "model": "pequeno"},
                "fallback_allowed": False,
            },
        )

        with pytest.raises(ProviderError) as error:
            await router.select(request, 1, ["single"])
        assert error.value.code == "CONTEXT_LIMIT_EXCEEDED"

    asyncio.run(scenario())

def test_routing_config_does_not_change_the_hard_pin(tmp_path: Path) -> None:
    """Ni la selección adaptativa ni la exploración pueden desplazar un target
    fijado: la exploración se sortea sobre el ranking, no sobre la fijación."""
    async def scenario() -> None:
        config = BrokerConfig()
        config.routing = RoutingConfig(adaptive_selection=True, exploration_rate=1.0)
        router = RoutedModelProvider(
            config,
            ollama=_CatalogStub([_entry("elegido"), _entry("otro"), _entry("tercero")]),
            deepseek=_CatalogStub([]),
        )
        request = _request(model_requirements={
            "target_model": {"provider": "ollama", "deployment": "local", "model": "elegido"},
            "fallback_allowed": False,
        })

        for _ in range(20):
            selected = await router.select(request, 1, ["single"])
            assert selected[0].model == "elegido"

    asyncio.run(scenario())
