"""Carril cloud y plazo agotado sin reintento (2026-10-27).

Regresión: nvidia/deepseek-v4.1-flash no contestaba ni a «ok». La tarea
reintentó el mismo modelo tres veces, 300 s cada vez, y como compartía el
workflow único de inferencia, las otras siete tareas —también cloud— esperaron
detrás con la GPU ociosa.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.config import (
    BrokerConfig,
    CloudLaneConfig,
    OpenAICompatibleProviderConfig,
    PersistenceConfig,
    ProcessingConfig,
    RoutingConfig,
    cloud_lane_providers,
)
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.main import create_app
from app.model_stats import load_model_stats
from app.providers import ModelOutput, ProviderError, RoutedModelProvider
from app.providers.base import provider_error_from_transport
from app.providers.routing import _remote_only, remote_only_scope
from app.repository import TaskRepository, lane_for_request, request_lane
from app.resource_scheduler import ResourceScheduler
from app.schemas import ModelReference, TaskCreateRequest, TaskKind, TaskStatus

REMOTE = frozenset({"nvidia", "hcnsec"})


def _config(tmp_path: Path | None = None, **lane) -> BrokerConfig:
    config = BrokerConfig(
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap",
                                    cloud_lane=CloudLaneConfig(**lane)),
    )
    if tmp_path is not None:
        config.persistence = PersistenceConfig(database=str(tmp_path / "cloud.db"))
    config.providers.custom = [
        OpenAICompatibleProviderConfig(id="nvidia", enabled=True, deployment="api",
                                       base_url="https://integrate.api.nvidia.com/v1"),
        # Declarado cloud pero corre en este PC.
        OpenAICompatibleProviderConfig(id="unsloth", enabled=True, deployment="cloud",
                                       base_url="http://127.0.0.1:8888/v1"),
        OpenAICompatibleProviderConfig(id="lemonade", enabled=True, deployment="api",
                                       base_url="http://localhost:13305/v1"),
    ]
    return config


def _request(**overrides) -> TaskCreateRequest:
    payload = {
        "idempotency_key": f"cloud:{uuid4().hex}",
        "content": {"prompt": "Resume esto."},
        "model_requirements": {"cloud_allowed": True},
    }
    payload.update(overrides)
    return TaskCreateRequest.model_validate(payload)


def _repository(tmp_path: Path) -> tuple[TaskRepository, Database]:
    db = Database(tmp_path / "cloud.db")
    db.init_schema()
    return TaskRepository(db), db


# --- a qué carril va -----------------------------------------------------------


def test_only_providers_on_another_machine_count_as_cloud() -> None:
    assert cloud_lane_providers(_config()) == frozenset({"nvidia"})


def test_cloud_lane_off_or_cloud_first_off_means_no_cloud_lane() -> None:
    assert cloud_lane_providers(_config(enabled=False)) is None
    config = _config()
    config.routing = RoutingConfig(prefer_cloud_when_allowed=False)
    assert cloud_lane_providers(config) is None


def test_single_chat_allowed_to_go_remote_goes_to_cloud_lane() -> None:
    assert lane_for_request(_request(), embedding_lane=True, cloud_providers=REMOTE) == TaskKind.cloud
    # El caso real: lista mixta de locales y remotos.
    mixed = _request(model_requirements={
        "cloud_allowed": True, "allowed_providers": ["lmstudio", "nvidia", "unsloth"],
    })
    assert lane_for_request(mixed, embedding_lane=True, cloud_providers=REMOTE) == TaskKind.cloud


def test_auto_allowed_to_go_remote_goes_to_cloud_lane() -> None:
    """El router puede acabar en consenso o agente, y aun así va a la cola
    cloud: ahí solo se eligen remotos. En la local ocupaba el único turno con
    la GPU parada mientras proponían nvidia y FreeLLMapi."""
    auto = _request(execution={"strategy": "auto"}, model_requirements={
        "cloud_allowed": True, "allowed_providers": ["lmstudio", "nvidia", "unsloth"],
    })
    assert lane_for_request(auto, embedding_lane=True, cloud_providers=REMOTE) == TaskKind.cloud
    only_local = _request(execution={"strategy": "auto"}, model_requirements={
        "cloud_allowed": True, "allowed_providers": ["lmstudio", "unsloth"],
    })
    assert lane_for_request(only_local, embedding_lane=True, cloud_providers=REMOTE) == TaskKind.inference
    no_cloud = _request(execution={"strategy": "auto"}, model_requirements={"cloud_allowed": False})
    assert lane_for_request(no_cloud, embedding_lane=True, cloud_providers=REMOTE) == TaskKind.inference


def test_what_must_not_leave_the_gpu_queue_stays_local() -> None:
    cases = [
        _request(model_requirements={"cloud_allowed": False}),
        _request(model_requirements={"cloud_allowed": True, "allowed_providers": ["lmstudio", "unsloth"]}),
        _request(execution={"strategy": "mixture_of_agents"}),
        _request(execution={"strategy": "agent"}),
        _request(model_requirements={"cloud_allowed": True, "target_model": {
            "provider": "nvidia", "deployment": "api", "model": "x"}}),
    ]
    for request in cases:
        assert lane_for_request(request, embedding_lane=True, cloud_providers=REMOTE) == TaskKind.inference
    assert lane_for_request(_request(), embedding_lane=True, cloud_providers=None) == TaskKind.inference


def test_confidential_data_never_reaches_the_cloud_lane() -> None:
    request = _request(model_requirements={}, risk={"data_classification": "confidential"})
    assert lane_for_request(request, embedding_lane=True, cloud_providers=REMOTE) == TaskKind.inference


def test_request_lane_reads_the_config() -> None:
    assert request_lane(_request(), _config()) == TaskKind.cloud
    assert request_lane(_request(), _config(enabled=False)) == TaskKind.inference


# --- reclamo ------------------------------------------------------------------


def test_cloud_tasks_run_in_parallel_while_local_is_busy(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    local, _ = repository.create_task(_request(model_requirements={"cloud_allowed": False}), queue_max_size=10)
    clouds = [
        repository.create_task(_request(), queue_max_size=10, kind=TaskKind.cloud)[0].task_id
        for _ in range(3)
    ]
    assert repository.claim_next_queued_task_id() == local.task_id
    assert repository.claim_next_queued_task_id() is None
    assert repository.claim_next_cloud_task(2) == clouds[0]
    assert repository.claim_next_cloud_task(2) == clouds[1]
    # Tope del carril alcanzado.
    assert repository.claim_next_cloud_task(2) is None


def test_demotion_moves_the_task_to_the_local_queue_by_priority(tmp_path: Path) -> None:
    repository, db = _repository(tmp_path)
    late, _ = repository.create_task(_request(priority=500), queue_max_size=10)
    cloud, _ = repository.create_task(_request(priority=100), queue_max_size=10, kind=TaskKind.cloud)
    assert repository.claim_next_cloud_task(4) == cloud.task_id

    assert repository.demote_to_inference_lane(cloud.task_id, {"code": "PROVIDER_UNAVAILABLE", "message": "x"})
    state = repository.get_task(cloud.task_id)
    assert state.kind == TaskKind.inference
    assert state.status == TaskStatus.queued
    # Más urgente que la local que ya esperaba: va delante.
    assert repository.claim_next_queued_task_id() == cloud.task_id
    assert late.task_id != cloud.task_id
    events = [row["event_type"] for row in db.query_all(
        "SELECT event_type FROM events WHERE task_id = ?", (cloud.task_id,))]
    assert "task.lane_demoted" in events
    # Ya no está en el carril cloud: una segunda bajada no hace nada.
    assert repository.demote_to_inference_lane(cloud.task_id, {"code": "X"}) is False


# --- plazo agotado ------------------------------------------------------------


def test_a_timeout_is_marked_and_says_what_happened() -> None:
    error = provider_error_from_transport(httpx.ReadTimeout(""), provider="nvidia", timeout_seconds=300)
    assert error.code == "PROVIDER_UNAVAILABLE" and error.retryable
    assert error.details["timed_out"] is True
    assert "300 s" in str(error)
    refused = provider_error_from_transport(httpx.ConnectError("refused"), provider="nvidia")
    assert refused.details["timed_out"] is False


class _Remotes:
    """Tres remotos; `hung` agota el plazo y los demás responden o no según se diga."""

    def __init__(self, *, all_fail: bool = False) -> None:
        self.all_fail = all_fail
        self.calls: list[str] = []
        self.remote_only_seen: list[bool] = []

    async def select(self, request, count, roles):
        self.remote_only_seen.append(_remote_only.get())
        names = ["hung", "sano", "otro"]
        return [ModelReference(provider="nvidia", deployment="api", model=names[index % 3], role=role)
                for index, role in enumerate(roles)]

    async def models(self):
        return []

    async def local_footprints(self, models):
        return [0 for _ in models]

    async def propose(self, request, model, ordinal):
        self.calls.append(model.model)
        if model.model == "hung":
            raise provider_error_from_transport(httpx.ReadTimeout(""), provider="nvidia", timeout_seconds=300)
        if self.all_fail:
            raise ProviderError("PROVIDER_UNAVAILABLE", "caído", retryable=True)
        return ModelOutput(content=f"respuesta de {model.model}", tokens_input=1, tokens_output=1,
                           cost_usd=0.0, latency_ms=1.0)

    async def close(self):
        pass


def _run(tmp_path: Path, provider, *, kind: TaskKind) -> tuple[TaskRepository, Database, str]:
    repository, db = _repository(tmp_path)
    coordinator = ConsensusCoordinator(db, ResourceScheduler(_config()), provider=provider)
    created, _ = repository.create_task(_request(), queue_max_size=10, kind=kind)
    claim = repository.claim_next_cloud_task if kind == TaskKind.cloud else (
        lambda _cap: repository.claim_next_queued_task_id())
    assert claim(4) == created.task_id
    asyncio.run(coordinator.process_task(repository, created.task_id))
    return repository, db, created.task_id


def test_a_timed_out_model_is_not_retried_and_the_next_one_answers(tmp_path: Path) -> None:
    provider = _Remotes()
    repository, db, task_id = _run(tmp_path, provider, kind=TaskKind.inference)
    # Antes: hung, hung, hung (900 s) y solo entonces el siguiente.
    assert provider.calls == ["hung", "sano"]
    state = repository.get_task(task_id)
    assert state.status == TaskStatus.completed
    assert state.result["model_used"]["model"] == "sano"


def test_the_timeout_still_counts_against_the_model_in_the_ranking(tmp_path: Path) -> None:
    _, db, _ = _run(tmp_path, _Remotes(), kind=TaskKind.cloud)
    stats = load_model_stats(db, window_days=1)
    hung = next(value for key, value in stats.items() if key[2] == "hung")
    healthy = next(value for key, value in stats.items() if key[2] == "sano")
    # Un solo fallo por plazo agotado (no tres) y el sano suma su acierto.
    assert (hung.attempts, hung.successes) == (1, 0)
    assert (healthy.attempts, healthy.successes) == (1, 1)


def test_cloud_lane_selects_only_remote_models(tmp_path: Path) -> None:
    provider = _Remotes()
    _run(tmp_path, provider, kind=TaskKind.cloud)
    assert provider.remote_only_seen and all(provider.remote_only_seen)
    local = _Remotes()
    (tmp_path / "local").mkdir()
    _run(tmp_path / "local", local, kind=TaskKind.inference)
    assert local.remote_only_seen and not any(local.remote_only_seen)


def test_cloud_task_with_no_remote_answer_goes_to_the_local_queue(tmp_path: Path) -> None:
    repository, _, task_id = _run(tmp_path, _Remotes(all_fail=True), kind=TaskKind.cloud)
    state = repository.get_task(task_id)
    assert state.status == TaskStatus.queued
    assert state.kind == TaskKind.inference
    assert state.progress["demoted_from_cloud"]["code"] == "PROVIDER_UNAVAILABLE"


def test_the_same_failure_in_the_local_lane_still_fails_or_retries(tmp_path: Path) -> None:
    repository, _, task_id = _run(tmp_path, _Remotes(all_fail=True), kind=TaskKind.inference)
    state = repository.get_task(task_id)
    assert state.kind == TaskKind.inference
    assert "demoted_from_cloud" not in (state.progress or {})


def test_remote_only_scope_filters_the_real_catalog() -> None:
    config = _config()
    router = RoutedModelProvider(config, custom={}, stats_loader=lambda: {})
    catalog = [
        {"name": "nim", "provider": "nvidia", "deployment": "api", "context_window": 100000,
         "capabilities": ["completion"], "compatibility": "compatible"},
        {"name": "studio", "provider": "unsloth", "deployment": "cloud", "context_window": 100000,
         "capabilities": ["completion"], "compatibility": "compatible"},
        {"name": "gemma", "provider": "ollama", "deployment": "local", "context_window": 100000,
         "capabilities": ["completion"], "compatibility": "compatible"},
    ]

    async def models():
        return catalog

    router.models = models  # type: ignore[method-assign]
    request = _request()

    async def names(remote: bool) -> list[str]:
        if remote:
            with remote_only_scope():
                eligible, _, _ = await router.eligible_catalog(request)
        else:
            eligible, _, _ = await router.eligible_catalog(request)
        return sorted(item["name"] for item in eligible)

    assert asyncio.run(names(False)) == ["gemma", "nim", "studio"]
    assert asyncio.run(names(True)) == ["nim"]


# --- API y panel --------------------------------------------------------------


def test_api_enqueues_in_cloud_lane_and_announces_it(tmp_path: Path) -> None:
    with TestClient(create_app(_config(tmp_path, max_parallel=3))) as client:
        body = client.get("/api/v1/capabilities").json()
        assert "cloud" in body["work_lanes"]
        response = client.post("/api/v1/tasks", json={
            "idempotency_key": "api:cloud", "content": {"prompt": "hola"},
            "model_requirements": {"cloud_allowed": True, "allowed_providers": ["nvidia"]},
        })
        assert response.status_code == 202, response.text
        task = client.get(f"/api/v1/tasks/{response.json()['task_id']}").json()
        assert task["kind"] == "cloud"


def test_dashboard_shows_one_column_per_lane(tmp_path: Path) -> None:
    with TestClient(create_app(_config(tmp_path))) as client:
        fragment = client.get("/dashboard/fragments/lanes")
        assert fragment.status_code == 200
        for label in ("Local", "Cloud", "Embeddings"):
            assert f">{label}</h3>" in fragment.text
        assert "lane-cloud" in fragment.text


def test_the_local_lane_marks_a_running_call_served_elsewhere(tmp_path: Path) -> None:
    """Un consenso en la cola local puede tener a un remoto proponiendo: ocupa
    el turno local, pero no la GPU. El panel lo marca «remoto» para que no
    parezca un cloud colado en la cola equivocada. Un `cloud` en loopback
    (unsloth) corre en este PC y no lleva la marca."""
    from app.dashboard import DashboardQueryRepository

    config = _config(enabled=True)
    db = Database(tmp_path / "lanes.db")
    db.init_schema()
    repository = TaskRepository(db)
    try:
        running = {}
        for provider, deployment in (("nvidia", "api"), ("unsloth", "cloud"), ("ollama", "local")):
            state, _ = repository.create_task(
                TaskCreateRequest(idempotency_key=f"remoto:{uuid4().hex}", content={"prompt": "hola"}),
                queue_max_size=100,
            )
            repository.update_task(state.task_id, TaskStatus.proposing, progress={
                "active_invocations": [{"provider": provider, "deployment": deployment, "model": "m"}],
            })
            running[state.task_id] = provider

        lanes = DashboardQueryRepository(db).lanes_view(config)
        local = next(lane for lane in lanes if lane["kind"] == TaskKind.inference)
        marks = {running[task["task_id"]]: task["remote"] for task in local["running"]}
    finally:
        db.close()

    assert marks == {"nvidia": True, "unsloth": False, "ollama": False}


class _RemoteConsensus(_Remotes):
    """Los mismos remotos, sin el que se cuelga y capaces de sintetizar."""

    async def select(self, request, count, roles):
        self.remote_only_seen.append(_remote_only.get())
        return [ModelReference(provider="nvidia", deployment="api", model=f"m{index}", role=role)
                for index, role in enumerate(roles)]

    async def synthesize(self, request, model, proposals):
        self.calls.append(f"síntesis:{model.model}")
        return ModelOutput(content="síntesis", tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0)


def test_an_auto_task_routed_to_consensus_in_the_cloud_lane_stays_remote(tmp_path: Path) -> None:
    """El caso de task_f80e…: auto con un prompt largo acaba en consenso. En la
    cola cloud cada elección (proponentes y árbitro) se hace con el filtro de
    remotos puesto, así que nunca llega a la GPU fuera del turno local."""
    from app.config import StrategyRouterConfig

    config = _config()
    config.strategy_router = StrategyRouterConfig(enabled=True, mixture_min_prompt_chars=10)
    repository, db = _repository(tmp_path)
    provider = _RemoteConsensus()
    coordinator = ConsensusCoordinator(db, ResourceScheduler(config), provider=provider)
    request = _request(execution={"strategy": "auto"}, content={"prompt": "Apuntes de esta clase larga."})
    assert request_lane(request, config) == TaskKind.cloud
    created, _ = repository.create_task(request, queue_max_size=10, kind=TaskKind.cloud)
    assert repository.claim_next_cloud_task(4) == created.task_id

    asyncio.run(coordinator.process_task(repository, created.task_id))

    routed = repository.latest_event_payload(created.task_id, "strategy.routed")
    assert routed is not None and routed["chosen_strategy"] == "mixture_of_agents"
    assert provider.remote_only_seen and all(provider.remote_only_seen)
    assert any(call.startswith("síntesis:") for call in provider.calls)
    assert repository.get_task(created.task_id).status == TaskStatus.completed
    db.close()
