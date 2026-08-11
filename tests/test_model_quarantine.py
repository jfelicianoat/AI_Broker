from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import BrokerConfig, ModelQuarantineConfig, PersistenceConfig
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.model_quarantine import load_quarantine
from app.providers import ModelOutput, ProviderError
from app.repository import TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import ModelReference, TaskCreateRequest


def _now(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _invocation(db: Database, model: str, status: str, code: str | None, minutes_ago: float) -> None:
    stamp = _now(minutes_ago)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_invocations "
            "(id, task_id, role, provider, deployment, model, status, error_code, "
            " created_at, updated_at, task_type) "
            "VALUES (?, 't', 'proposer', 'ollama', 'local', ?, ?, ?, ?, ?, 'code')",
            (f"inv_{model}_{minutes_ago}_{status}", model, status, code, stamp, stamp),
        )


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "q.db")
    db.init_schema()
    # model_invocations referencia tasks: la fila padre tiene que existir.
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO tasks (id, request_json, status, priority, created_at, updated_at) "
            "VALUES ('t', '{}', 'completed', 100, ?, ?)",
            (_now(), _now()),
        )
    return db


class TestQuarantineRule:
    def test_two_definitive_failures_in_a_row_take_a_model_out(self, tmp_path) -> None:
        db = _db(tmp_path)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 10)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 5)
        quarantine = load_quarantine(db, ModelQuarantineConfig())
        assert ("ollama", "local", "roto:bf16") in quarantine
        entry = quarantine[("ollama", "local", "roto:bf16")]
        assert entry.failures == 2
        assert "INVALID_PROVIDER_RESPONSE" in entry.reason

    def test_a_single_failure_is_not_enough(self, tmp_path) -> None:
        db = _db(tmp_path)
        _invocation(db, "sano:q8", "failed", "INVALID_PROVIDER_RESPONSE", 5)
        assert load_quarantine(db, ModelQuarantineConfig()) == {}

    def test_a_later_success_clears_the_streak(self, tmp_path) -> None:
        db = _db(tmp_path)
        _invocation(db, "sano:q8", "failed", "INVALID_PROVIDER_RESPONSE", 30)
        _invocation(db, "sano:q8", "failed", "INVALID_PROVIDER_RESPONSE", 20)
        _invocation(db, "sano:q8", "completed", None, 10)
        assert load_quarantine(db, ModelQuarantineConfig()) == {}

    def test_running_out_of_memory_is_a_circumstance_not_a_defect(self, tmp_path) -> None:
        db = _db(tmp_path)
        _invocation(db, "grande:bf16", "failed", "VRAM_INSUFFICIENT", 30)
        _invocation(db, "grande:bf16", "failed", "VRAM_INSUFFICIENT", 20)
        _invocation(db, "grande:bf16", "failed", "PROVIDER_UNAVAILABLE", 10)
        assert load_quarantine(db, ModelQuarantineConfig()) == {}

    def test_circumstantial_failures_do_not_break_a_definitive_streak(self, tmp_path) -> None:
        db = _db(tmp_path)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 30)
        _invocation(db, "roto:bf16", "failed", "VRAM_INSUFFICIENT", 20)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 10)
        assert ("ollama", "local", "roto:bf16") in load_quarantine(db, ModelQuarantineConfig())

    def test_quarantine_expires_on_its_own(self, tmp_path) -> None:
        db = _db(tmp_path)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 60 * 30)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 60 * 29)
        assert load_quarantine(db, ModelQuarantineConfig(window_hours=24)) == {}
        assert load_quarantine(db, ModelQuarantineConfig(window_hours=48)) != {}

    def test_disabled_never_quarantines(self, tmp_path) -> None:
        db = _db(tmp_path)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 10)
        _invocation(db, "roto:bf16", "failed", "INVALID_PROVIDER_RESPONSE", 5)
        assert load_quarantine(db, ModelQuarantineConfig(enabled=False)) == {}


class TestQuarantinedModelsLeaveTheRotation:
    @staticmethod
    def _provider(quarantined: dict) -> object:
        import httpx

        from app.providers import OllamaProvider, RoutedModelProvider

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [
                    {"name": name, "size": 1, "capabilities": ["completion"],
                     "details": {"context_length": 32768}}
                    for name in ["roto:bf16", "sano:q8_0"]
                ]})
            return httpx.Response(200, json={"models": []})

        config = BrokerConfig()
        return RoutedModelProvider(
            config,
            ollama=OllamaProvider(config, transport=httpx.MockTransport(handler)),
            quarantine_loader=lambda: quarantined,
        )

    def test_a_quarantined_model_is_not_selected(self) -> None:
        from app.model_quarantine import QuarantineEntry

        provider = self._provider({
            ("ollama", "local", "roto:bf16"): QuarantineEntry(
                failures=2, last_code="INVALID_PROVIDER_RESPONSE", last_failure_at=_now()
            )
        })
        request = TaskCreateRequest.model_validate({
            "idempotency_key": "quarantine:select", "content": {"prompt": "hola"},
        })
        chosen = asyncio.run(provider.select(request, 1, ["generalist"]))
        catalog = asyncio.run(provider.models())
        asyncio.run(provider.close())
        assert [item.model for item in chosen] == ["sano:q8_0"]
        # Sigue en el catálogo, pero marcado y con el motivo: un modelo que
        # desaparece sin explicación es un misterio que se paga depurando.
        apartado = next(item for item in catalog if item["name"] == "roto:bf16")
        assert apartado["quarantined"] is True
        assert "INVALID_PROVIDER_RESPONSE" in apartado["quarantine_reason"]

    def test_quarantine_is_bypassed_rather_than_leaving_no_models(self) -> None:
        from app.model_quarantine import QuarantineEntry

        entry = QuarantineEntry(
            failures=2, last_code="INVALID_PROVIDER_RESPONSE", last_failure_at=_now()
        )
        provider = self._provider({
            ("ollama", "local", "roto:bf16"): entry,
            ("ollama", "local", "sano:q8_0"): entry,
        })
        request = TaskCreateRequest.model_validate({
            "idempotency_key": "quarantine:bypass", "content": {"prompt": "hola"},
        })
        chosen = asyncio.run(provider.select(request, 1, ["generalist"]))
        asyncio.run(provider.close())
        assert len(chosen) == 1


class BrokenProposerProvider:
    """Tres proposers de los que uno devuelve basura de forma definitiva, como
    north-mini-code-1.0:bf16 en la máquina real."""

    def __init__(self, broken: str = "roto") -> None:
        self.broken = broken
        self.proposals = 0

    async def select(self, request, count, roles):
        names = ["bueno_1", self.broken, "bueno_2"]
        return [
            ModelReference(provider="ollama", deployment="local", model=names[index % 3], role=role)
            for index, role in enumerate(roles)
        ]

    async def models(self):
        return [
            {"provider": "ollama", "deployment": "local", "name": name,
             "context_window": 32768, "capabilities": ["completion"], "size_bytes": 1}
            for name in ["bueno_1", self.broken, "bueno_2"]
        ]

    async def local_footprints(self, models):
        return [1 for _ in models]

    async def propose(self, request, model, ordinal):
        if model.model == self.broken:
            raise ProviderError(
                "INVALID_PROVIDER_RESPONSE", "Ollama no devolvió message.content"
            )
        self.proposals += 1
        return ModelOutput(
            content=f"propuesta de {model.model}", tokens_input=10, tokens_output=5,
            cost_usd=0.0, latency_ms=1.0,
        )

    async def synthesize(self, request, model, proposals):
        return ModelOutput(
            content=f"sintesis de {len(proposals)} propuestas", tokens_input=10,
            tokens_output=5, cost_usd=0.0, latency_ms=1.0,
        )

    async def close(self):
        pass


def _moa_request(**overrides) -> TaskCreateRequest:
    payload = {
        "idempotency_key": "quarantine:moa",
        "content": {"prompt": "Analiza esto"},
        "execution": {"strategy": "mixture_of_agents", "preset": "slow", "max_proposers": 3},
        **overrides,
    }
    return TaskCreateRequest.model_validate(payload)


def _run(tmp_path: Path, provider) -> dict:
    config = BrokerConfig(persistence=PersistenceConfig(database=str(tmp_path / "moa.db")))
    db = Database(tmp_path / "moa.db")
    db.init_schema()
    coordinator = ConsensusCoordinator(db, ResourceScheduler(config), provider=provider)
    repository = TaskRepository(db)
    task, _ = repository.create_task(_moa_request(), queue_max_size=100)
    asyncio.run(coordinator.process_task(repository, task.task_id))
    return repository.get_task(task.task_id).model_dump(mode="json"), db


class TestConsensusSurvivesABrokenProposer:
    def test_a_broken_proposer_is_a_casualty_not_the_end_of_the_task(self, tmp_path) -> None:
        provider = BrokenProposerProvider()
        state, db = _run(tmp_path, provider)
        # Dos propuestas buenas alcanzan el quorum: antes el error definitivo
        # del tercero tumbaba la tarea entera con las otras dos ya cobradas.
        assert state["status"] == "completed"
        assert provider.proposals == 2
        assert "sintesis de 2 propuestas" in state["result"]["assistant_content"]

    def test_the_casualty_is_recorded_with_its_reason(self, tmp_path) -> None:
        _, db = _run(tmp_path, BrokenProposerProvider())
        skipped = db.query_all(
            "SELECT payload_json FROM events WHERE event_type = 'proposer.skipped'"
        )
        assert len(skipped) == 1
        assert "INVALID_PROVIDER_RESPONSE" in skipped[0]["payload_json"]

    def test_the_failure_code_lands_in_model_invocations(self, tmp_path) -> None:
        _, db = _run(tmp_path, BrokenProposerProvider())
        rows = db.query_all(
            "SELECT model, error_code FROM model_invocations WHERE status = 'failed'"
        )
        assert [(row["model"], row["error_code"]) for row in rows] == [
            ("roto", "INVALID_PROVIDER_RESPONSE")
        ]
