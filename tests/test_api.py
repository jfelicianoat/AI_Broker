import asyncio
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.providers import BootstrapModelProvider


class ConcurrencyProbeProvider(BootstrapModelProvider):
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def propose(self, request, model, ordinal):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            return await super().propose(request, model, ordinal)
        finally:
            self.active -= 1


class TimeoutProbeProvider(BootstrapModelProvider):
    def __init__(self) -> None:
        self.cancelled = False

    async def propose(self, request, model, ordinal):
        try:
            await asyncio.sleep(5)
            return await super().propose(request, model, ordinal)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


def test_create_and_read_task(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={"idempotency_key": "test:create", "content": {"prompt": "Hola"}})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["execution_strategy"] == "single"

        task_response = client.get(body["status_url"])
        assert task_response.status_code == 200
        task = task_response.json()
        assert task["task_id"] == body["task_id"]
        assert task["progress"]["phase"] == "queued"


def test_invalid_contract_has_structured_error(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={"idempotency_key": "test:invalid", "content": {"prompt": ""}})

        assert response.status_code == 422
        assert response.json()["code"] == "CONTRACT_VALIDATION_FAILED"


def test_cancel_is_idempotent(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={"idempotency_key": "test:cancel", "content": {"prompt": "Hola"}}).json()

        first = client.delete(created["cancel_url"])
        second = client.delete(created["cancel_url"])

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["status"] == "cancelled"
        assert second.json()["status"] == "cancelled"


def test_health_ready(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json()["dependencies"]["sqlite"]["status"] == "healthy"


def test_models_endpoint_is_available(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/models")

        assert response.status_code == 200
        assert response.json()["models"][0]["deployment"] == "bootstrap"


def test_capabilities_publish_slow_and_runtime_limits(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-capabilities.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            max_parallel_invocations=2,
        ),
    )
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "2.1"
    assert body["presets"]["mixture_of_agents"] == ["fast", "slow"]
    assert body["scheduling_by_preset"]["fast"] == ["sequential"]
    assert "parallel" in body["scheduling_by_preset"]["slow"]
    assert body["max_active_workflows"] == 1
    assert body["max_parallel_invocations"] == 2


def test_dashboard_read_models_are_paged_filterable_and_source_backed(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "dashboard:first",
                "content": {
                    "prompt": "Primera tarea",
                    "metadata": {"origin": "prompt_tester"},
                },
            },
        ).json()
        client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "dashboard:second",
                "content": {
                    "prompt": "Segunda tarea",
                    "metadata": {"origin": "orchestrator"},
                },
            },
        )
        client.post("/api/v1/dispatcher/tick")

        summary = client.get("/api/v1/dashboard/summary?window_hours=24")
        page = client.get("/api/v1/dashboard/tasks?page=1&page_size=1")
        filtered = client.get("/api/v1/dashboard/tasks?origin=prompt_tester")
        detail = client.get(f"/api/v1/dashboard/tasks/{first['task_id']}")
        usage = client.get("/api/v1/usage")
        resources = client.get("/api/v1/dashboard/resources")

    assert summary.status_code == 200
    assert summary.json()["queued"] == 1
    assert summary.json()["completed"] == 1
    assert summary.json()["invocations"] == 1
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["page_size"] == 1
    assert page.json()["total_pages"] == 2
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["origin"] == "prompt_tester"
    assert detail.status_code == 200
    assert detail.json()["request"]["content"]["prompt"] == "Primera tarea"
    assert len(detail.json()["invocations"]) == 1
    assert detail.json()["events"]
    assert usage.status_code == 200
    assert usage.json()["providers"]["ollama"]["invocations"] == 1.0
    assert resources.status_code == 200
    assert resources.json()["provider"] == "bootstrap"
    assert resources.json()["used_vram_bytes"] == 0


def test_dispatcher_processes_single_task(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={"idempotency_key": "test:single", "content": {"prompt": "Resume esto"}}).json()

        tick = client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

        assert tick.status_code == 200
        assert tick.json()["task_id"] == created["task_id"]
        assert task["status"] == "completed"
        assert task["result"]["usage"]["invocations"] == 1
        assert "result_markdown" in task["result"]


def test_dispatcher_processes_fast_consensus(tmp_path: Path) -> None:
    payload = {
        "idempotency_key": "test:consensus",
        "content": {"prompt": "Compara dos enfoques de routing"},
        "execution": {
            "strategy": "mixture_of_agents",
            "preset": "fast",
            "max_proposers": 3,
            "selection": {"mode": "auto", "proposer_count": 3},
        },
    }
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json=payload).json()

        tick = client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

        assert tick.status_code == 200
        assert task["status"] == "completed"
        assert task["result"]["consensus"]["proposers_completed"] == 3
        assert task["result"]["usage"]["invocations"] == 4
        assert task["result"]["scheduling"]["mode_used"] in {"parallel", "waves", "sequential"}
        assert Path(task["result"]["artifacts_root"]).exists()
        assert (Path(task["result"]["artifacts_root"]) / "synthesis" / "final.md").exists()


def test_slow_consensus_runs_proposers_in_parallel_but_fast_remains_serial(tmp_path: Path) -> None:
    def execute(preset: str, database_name: str) -> tuple[dict, int]:
        config = BrokerConfig(
            persistence=PersistenceConfig(database=str(tmp_path / database_name)),
            processing=ProcessingConfig(
                auto_dispatch=False,
                provider_mode="bootstrap",
                max_parallel_invocations=3,
            ),
        )
        probe = ConcurrencyProbeProvider()
        with TestClient(create_app(config)) as client:
            client.app.state.coordinator.provider = probe
            payload = {
                "idempotency_key": f"test:{preset}:parallelism",
                "content": {"prompt": "Compara tres alternativas"},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "preset": preset,
                    "scheduling": "adaptive",
                    "max_proposers": 3,
                    "selection": {"mode": "auto", "proposer_count": 3},
                },
            }
            created = client.post("/api/v1/tasks", json=payload)
            assert created.status_code == 202
            task_id = created.json()["task_id"]
            client.post("/api/v1/dispatcher/tick")
            task = client.get(f"/api/v1/tasks/{task_id}").json()
        return task, probe.max_active

    slow_task, slow_parallelism = execute("slow", "broker-slow.db")
    fast_task, fast_parallelism = execute("fast", "broker-fast.db")

    assert slow_task["status"] == "completed"
    assert slow_task["result"]["consensus"]["level"] == "slow"
    assert slow_task["result"]["scheduling"]["mode_used"] == "parallel"
    assert slow_task["result"]["scheduling"]["max_parallel_invocations_launched"] == 3
    assert slow_parallelism == 3

    assert fast_task["status"] == "completed"
    assert fast_task["result"]["consensus"]["level"] == "fast"
    assert fast_task["result"]["scheduling"]["mode_used"] == "sequential"
    assert fast_parallelism == 1


def test_slow_parallel_fails_before_launch_when_capacity_is_insufficient(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-slow-capacity.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            max_parallel_invocations=1,
        ),
    )
    probe = ConcurrencyProbeProvider()
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = probe
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "test:slow:insufficient",
                "content": {"prompt": "Compara tres alternativas"},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "preset": "slow",
                    "scheduling": "parallel",
                    "max_proposers": 3,
                    "selection": {"mode": "auto", "proposer_count": 3},
                },
            },
        ).json()

        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

    assert task["status"] == "failed"
    assert task["error"]["code"] == "PARALLEL_CAPACITY_INSUFFICIENT"
    assert probe.max_active == 0


def test_single_rejects_slow_preset(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "test:single:slow",
                "content": {"prompt": "No válido"},
                "execution": {"strategy": "single", "preset": "slow"},
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "CONTRACT_VALIDATION_FAILED"


def test_execution_timeout_cancels_provider_and_persists_typed_error(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-timeout.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            task_timeout_seconds=10,
        ),
    )
    probe = TimeoutProbeProvider()
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = probe
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "test:task:timeout",
                "content": {"prompt": "Tarea lenta"},
                "execution": {"strategy": "single", "timeout_seconds": 1},
            },
        ).json()

        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

    assert task["status"] == "failed"
    assert task["error"]["code"] == "TASK_TIMEOUT"
    assert task["progress"]["timeout_seconds"] == 1
    assert probe.cancelled


def test_standard_consensus_fails_until_phase_c(tmp_path: Path) -> None:
    payload = {
        "idempotency_key": "test:standard",
        "content": {"prompt": "Analiza con rúbrica"},
        "execution": {
            "strategy": "mixture_of_agents",
            "preset": "standard",
            "selection": {"mode": "auto", "proposer_count": 3},
        },
    }
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json=payload).json()

        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

        assert task["status"] == "failed"
        assert task["error"]["code"] == "CONSENSUS_PRESET_NOT_IMPLEMENTED"


def test_create_is_idempotent_and_conflicting_payload_returns_409(tmp_path: Path) -> None:
    payload = {"idempotency_key": "idem:same", "request_id": "local-1", "content": {"prompt": "Hola"}}
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/tasks", json=payload)
        replay = client.post("/api/v1/tasks", json=payload)
        conflict = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "idem:same", "request_id": "local-1", "content": {"prompt": "Otro"}},
        )

        assert first.status_code == 202
        assert replay.status_code == 200
        assert replay.json()["task_id"] == first.json()["task_id"]
        assert conflict.status_code == 409
        assert len(client.get("/api/v1/queue").json()["pending"]) == 1


def test_background_dispatcher_consumes_queue_without_tick(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-auto.db")),
        processing=ProcessingConfig(auto_dispatch=True, dispatcher_interval_seconds=0.01, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "auto:single", "content": {"prompt": "Procesa automáticamente"}},
        ).json()
        deadline = time.monotonic() + 2
        task = client.get(created["status_url"]).json()
        while task["status"] not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
            time.sleep(0.01)
            task = client.get(created["status_url"]).json()

        assert task["status"] == "completed"
        assert task["result"]["result_markdown"]


def test_idempotency_survives_broker_restart(tmp_path: Path) -> None:
    database = tmp_path / "broker-restart.db"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(database)),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    payload = {"idempotency_key": "restart:same", "content": {"prompt": "No duplicar"}}
    with TestClient(create_app(config)) as first_client:
        first = first_client.post("/api/v1/tasks", json=payload)
        assert first.status_code == 202
        task_id = first.json()["task_id"]

    with TestClient(create_app(config)) as restarted_client:
        replay = restarted_client.post("/api/v1/tasks", json=payload)
        assert replay.status_code == 200
        assert replay.json()["task_id"] == task_id
        assert len(restarted_client.get("/api/v1/queue").json()["pending"]) == 1


def test_claim_is_atomic_and_never_activates_second_workflow(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for ordinal in (1, 2):
            response = client.post(
                "/api/v1/tasks",
                json={"idempotency_key": f"claim:{ordinal}", "content": {"prompt": f"Tarea {ordinal}"}},
            )
            assert response.status_code == 202
        repository = client.app.state.repository
        first = repository.claim_next_queued_task_id()
        second = repository.claim_next_queued_task_id()
        queue = client.get("/api/v1/queue").json()

        assert first is not None
        assert second is None
        assert len(queue["active"]) == 1
        assert len(queue["pending"]) == 1
