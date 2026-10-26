"""Caída del proveedor: la tarea vuelve a la cola en vez de fallar.

Regresión del 2026-10-24 a las 15:48: Ollama cortó la conexión a mitad de
respuesta (RemoteProtocolError), la tarea murió con INTERNAL_ERROR y su
invocación quedó en 'started' para siempre; las que se reclamaron mientras se
levantaba fallaron en ráfaga con MODEL_UNAVAILABLE.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.providers import ModelOutput, ProviderError
from app.providers.diagnostics import diagnose
from app.repository import TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import ModelReference, TaskCreateRequest, TaskStatus


class FlakyProvider:
    """Un único modelo cuyo runtime está caído las primeras `failures` veces."""

    def __init__(self, *, code: str = "PROVIDER_UNAVAILABLE", failures: int = 99,
                 retryable: bool = True, deployment: str = "local") -> None:
        self.code = code
        self.failures = failures
        self.retryable = retryable
        self.deployment = deployment
        self.calls = 0

    async def select(self, request, count, roles):
        return [
            ModelReference(provider="ollama", deployment=self.deployment, model="m", role=role)
            for role in roles
        ]

    async def models(self):
        return [{"provider": "ollama", "deployment": self.deployment, "name": "m",
                 "context_window": 32768, "capabilities": ["completion"], "size_bytes": 1}]

    async def local_footprints(self, models):
        return [1 for _ in models]

    async def propose(self, request, model, ordinal):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError(self.code, "Ollama se está reiniciando", retryable=self.retryable)
        return ModelOutput(content="hecho", tokens_input=1, tokens_output=1,
                           cost_usd=0.0, latency_ms=1.0)

    async def synthesize(self, request, model, proposals):
        raise AssertionError("single no sintetiza")

    async def close(self):
        pass


def _setup(tmp_path: Path, provider, *, max_attempts: int = 3):
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "outage.db")),
        processing=ProcessingConfig(max_task_attempts=max_attempts, outage_retry_seconds=60),
    )
    db = Database(tmp_path / "outage.db")
    db.init_schema()
    coordinator = ConsensusCoordinator(db, ResourceScheduler(config), provider=provider)
    repository = TaskRepository(db)
    request = TaskCreateRequest.model_validate({
        "idempotency_key": "outage:1",
        "content": {"prompt": "Hola"},
        "execution": {"strategy": "single"},
    })
    task, _ = repository.create_task(request, queue_max_size=100)
    return coordinator, repository, db, task.task_id


def _process(coordinator, repository, task_id: str) -> None:
    asyncio.run(coordinator.process_task(repository, task_id))


def test_an_outage_requeues_with_growing_wait(tmp_path: Path) -> None:
    coordinator, repository, db, task_id = _setup(tmp_path, FlakyProvider())
    _process(coordinator, repository, task_id)

    state = repository.get_task(task_id)
    assert state.status == TaskStatus.queued
    row = db.query_one("SELECT attempt, not_before FROM tasks WHERE id = ?", (task_id,))
    assert row["attempt"] == 1
    wait = datetime.fromisoformat(row["not_before"]) - datetime.now(timezone.utc)
    assert 50 < wait.total_seconds() <= 60
    events = db.query_all("SELECT event_type FROM events WHERE task_id = ?", (task_id,))
    assert "task.retry_scheduled" in [item["event_type"] for item in events]


def test_retries_stop_at_max_attempts(tmp_path: Path) -> None:
    coordinator, repository, db, task_id = _setup(tmp_path, FlakyProvider(), max_attempts=2)
    _process(coordinator, repository, task_id)
    assert repository.get_task(task_id).status == TaskStatus.queued
    _process(coordinator, repository, task_id)

    state = repository.get_task(task_id)
    assert state.status == TaskStatus.failed
    assert state.error["code"] == "PROVIDER_UNAVAILABLE"


def test_the_retry_succeeds_once_the_runtime_is_back(tmp_path: Path) -> None:
    provider = FlakyProvider(failures=1)
    coordinator, repository, _, task_id = _setup(tmp_path, provider)
    _process(coordinator, repository, task_id)
    _process(coordinator, repository, task_id)
    assert repository.get_task(task_id).status == TaskStatus.completed


def test_a_task_error_is_not_retried(tmp_path: Path) -> None:
    provider = FlakyProvider(code="PROMPT_ECHOED", retryable=False)
    coordinator, repository, _, task_id = _setup(tmp_path, provider)
    _process(coordinator, repository, task_id)
    assert repository.get_task(task_id).status == TaskStatus.failed


def test_no_retry_after_a_billed_remote_call(tmp_path: Path) -> None:
    coordinator, repository, db, task_id = _setup(tmp_path, FlakyProvider())
    db.execute(
        "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, status, "
        "created_at, updated_at) VALUES ('inv_remote', ?, 'single', 'nvidia', 'api', 'x', "
        "'completed', '2026-10-25T00:00:00+00:00', '2026-10-25T00:00:00+00:00')",
        (task_id,),
    )
    _process(coordinator, repository, task_id)
    assert repository.get_task(task_id).status == TaskStatus.failed


def test_open_invocations_are_closed_as_ambiguous(tmp_path: Path) -> None:
    coordinator, repository, db, task_id = _setup(tmp_path, FlakyProvider(failures=0))
    db.execute(
        "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, status, "
        "created_at, updated_at) VALUES ('inv_hung', ?, 'single', 'ollama', 'local', 'm', "
        "'started', '2026-10-25T00:00:00+00:00', '2026-10-25T00:00:00+00:00')",
        (task_id,),
    )
    _process(coordinator, repository, task_id)
    row = db.query_one("SELECT status FROM model_invocations WHERE id = 'inv_hung'")
    assert row["status"] == "ambiguous"


def test_llama_server_start_timeout_is_a_retryable_outage() -> None:
    diagnosis = diagnose('{"error":"timed out waiting for llama-server to start - "}')
    assert diagnosis is not None
    assert diagnosis.code == "MODEL_LOAD_TIMEOUT"
    assert diagnosis.retryable is True


def test_a_dropped_connection_is_a_transport_error() -> None:
    """El caso que se escapaba: RemoteProtocolError no es NetworkError."""
    assert issubclass(httpx.RemoteProtocolError, httpx.TransportError)
    assert not issubclass(httpx.RemoteProtocolError, httpx.NetworkError)


class BrokenFirstModel(FlakyProvider):
    """Tres candidatos; el primero responde 400 como DeepSeek-V4-Pro en HCNSEC."""

    async def select(self, request, count, roles):
        names = ["roto", "sano", "otro"]
        return [
            ModelReference(provider="HCNSEC", deployment="cloud", model=names[index % 3], role=role)
            for index, role in enumerate(roles)
        ]

    async def propose(self, request, model, ordinal):
        self.calls += 1
        if model.model == "roto":
            raise ProviderError("MODEL_ERROR", "HTTP 400: Requested model not supported")
        return ModelOutput(content=f"respuesta de {model.model}", tokens_input=1, tokens_output=1,
                           cost_usd=0.0, latency_ms=1.0)


def test_single_fails_over_to_the_next_candidate(tmp_path: Path) -> None:
    coordinator, repository, db, task_id = _setup(tmp_path, BrokenFirstModel())
    _process(coordinator, repository, task_id)

    state = repository.get_task(task_id)
    assert state.status == TaskStatus.completed
    assert state.result["model_used"]["model"] == "sano"
    events = [row["event_type"] for row in db.query_all(
        "SELECT event_type FROM events WHERE task_id = ?", (task_id,))]
    assert "single.failover" in events


def test_a_target_model_has_no_failover(tmp_path: Path) -> None:
    provider = BrokenFirstModel()
    coordinator, repository, _, _ = _setup(tmp_path, provider)
    request = TaskCreateRequest.model_validate({
        "idempotency_key": "outage:target",
        "content": {"prompt": "Hola"},
        "execution": {"strategy": "single"},
        "model_requirements": {"fallback_allowed": False},
    })
    task, _ = repository.create_task(request, queue_max_size=100)
    _process(coordinator, repository, task.task_id)
    assert repository.get_task(task.task_id).status == TaskStatus.failed
    assert provider.calls == 1


def test_an_advertised_but_unserved_model_is_quarantinable() -> None:
    """MODEL_UNAVAILABLE está en model_quarantine.definitive_codes; MODEL_ERROR no."""
    text = '{"error":{"message":"Requested model DeepSeek-V4-Flash-Vision-Exp not supported"}}'
    diagnosis = diagnose(text)
    assert diagnosis is not None
    assert diagnosis.code == "MODEL_UNAVAILABLE"
    assert diagnosis.retryable is False
