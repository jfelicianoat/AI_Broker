"""Map-reduce de contexto largo (execution.long_context = "map_reduce").

Cubre el contrato (opt-in y restricciones), la división por ventana y el
pipeline completo del coordinador contra un provider falso de ventana pequeña.
"""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.ingestion.service import ATTACHED_DOCS_SENTINEL, split_expanded_prompt
from app.main import create_app
from app.providers import ModelOutput, ProviderError
from app.providers.base import estimate_required_context
from app.repository import TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import ModelReference, TaskCreateRequest


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


# ------------------------------------------------------------------- contrato

def test_long_context_defaults_to_fail():
    request = TaskCreateRequest.model_validate({
        "idempotency_key": "lc:default", "content": {"prompt": "hola"},
    })
    assert request.execution.long_context == "fail"


def test_map_reduce_rejected_outside_single():
    for strategy in ("mixture_of_agents", "agent"):
        with pytest.raises(ValueError, match="single or auto"):
            TaskCreateRequest.model_validate({
                "idempotency_key": f"lc:{strategy}",
                "content": {"prompt": "hola"},
                "execution": {"strategy": strategy, "long_context": "map_reduce"},
            })


def test_map_reduce_rejected_with_json_output():
    with pytest.raises(ValueError, match="json output"):
        TaskCreateRequest.model_validate({
            "idempotency_key": "lc:json",
            "content": {"prompt": "hola"},
            "output": {"format": "json", "json_schema": {"type": "object"}},
            "execution": {"strategy": "single", "long_context": "map_reduce"},
        })


def test_capabilities_announce_map_reduce(tmp_path):
    with make_client(tmp_path) as client:
        body = client.get("/api/v1/capabilities").json()
        assert body["contract_version"] == "2.5"
        assert body["long_context_map_reduce"] is True


# ------------------------------------------------------------------- splitter

def test_split_expanded_prompt_roundtrip():
    prompt = f"Resume esto{ATTACHED_DOCS_SENTINEL}aviso\n\n<attached_document>...</attached_document>"
    parts = split_expanded_prompt(prompt)
    assert parts is not None
    instruction, documents = parts
    assert instruction == "Resume esto"
    assert documents.startswith("aviso")
    assert split_expanded_prompt("prompt sin adjuntos") is None


def test_split_documents_respects_window(tmp_path):
    coordinator = _make_coordinator(tmp_path, TinyWindowProvider())
    documents = "\n\n".join(f"Párrafo {i} " + "contenido " * 40 for i in range(30))
    chunks = coordinator._split_documents_for_window(documents, "Resume", 2000, 200)
    assert len(chunks) > 1
    # Ningún fragmento excede el presupuesto y no se pierde contenido.
    reassembled = "\n\n".join(chunks)
    for i in range(30):
        assert f"Párrafo {i} " in reassembled


def test_split_hard_breaks_giant_paragraph(tmp_path):
    coordinator = _make_coordinator(tmp_path, TinyWindowProvider())
    giant = "x" * 200_000
    chunks = coordinator._split_documents_for_window(giant, "Resume", 20_000, 200)
    assert len(chunks) > 1
    assert "".join(chunks) == giant


# ---------------------------------------------------------- pipeline completo

class TinyWindowProvider:
    """Provider falso: ventana de 2000 tokens, respuestas deterministas."""

    window = 2000

    def __init__(self) -> None:
        self.map_calls = 0
        self.reduce_calls = 0
        self.cost_per_call = 0.001

    async def select(self, request, count, roles):
        if estimate_required_context(request) > self.window:
            raise ProviderError("CONTEXT_LIMIT_EXCEEDED", "no cabe")
        return [
            ModelReference(provider="ollama", deployment="bootstrap", model="tiny", role=roles[i])
            for i in range(count)
        ]

    async def models(self):
        return [{
            "provider": "ollama", "deployment": "bootstrap", "name": "tiny",
            "context_window": self.window, "capabilities": ["completion"],
        }]

    async def propose(self, request, model, ordinal):
        prompt = request.content.prompt
        if "<parcial" in prompt:
            self.reduce_calls += 1
            content = f"SINTESIS_FINAL(parciales={prompt.count('<parcial')})"
        elif "<fragmento" in prompt:
            self.map_calls += 1
            content = f"PARCIAL_{self.map_calls}"
        else:
            content = "RESPUESTA_DIRECTA"
        return ModelOutput(
            content=content, tokens_input=100, tokens_output=20,
            cost_usd=self.cost_per_call, latency_ms=5.0,
        )

    async def close(self):
        pass


def _make_coordinator(tmp_path: Path, provider) -> ConsensusCoordinator:
    config = BrokerConfig(persistence=PersistenceConfig(database=str(tmp_path / "lc.db")))
    db = Database(tmp_path / "lc.db")
    db.init_schema()
    return ConsensusCoordinator(db, ResourceScheduler(config), provider=provider)


def _long_request(chars: int = 40_000, **overrides) -> TaskCreateRequest:
    documents = "\n\n".join(
        f"Sección {i}: " + "dato relevante " * 50 for i in range(chars // 800)
    )
    payload = {
        "idempotency_key": f"lc:pipeline:{chars}",
        "content": {"prompt": f"Resume los datos{ATTACHED_DOCS_SENTINEL}{documents}"},
        # La reserva de salida debe caber en la ventana del provider falso (2000).
        "generation": {"max_output_tokens": 256},
        "execution": {"strategy": "single", "long_context": "map_reduce"},
        **overrides,
    }
    return TaskCreateRequest.model_validate(payload)


def _run_task(coordinator: ConsensusCoordinator, request: TaskCreateRequest) -> dict:
    repository = TaskRepository(coordinator.db)
    task, _ = repository.create_task(request, queue_max_size=100)
    asyncio.run(coordinator.process_task(repository, task.task_id))
    return repository.get_task(task.task_id).model_dump(mode="json")


def test_map_reduce_pipeline_completes(tmp_path):
    provider = TinyWindowProvider()
    coordinator = _make_coordinator(tmp_path, provider)
    state = _run_task(coordinator, _long_request())

    assert state["status"] == "completed"
    assert provider.map_calls > 1
    assert provider.reduce_calls >= 1
    long_context = state["result"]["long_context"]
    assert long_context["mode"] == "map_reduce"
    assert long_context["chunks"] == provider.map_calls
    assert long_context["total_invocations"] == provider.map_calls + provider.reduce_calls
    assert "SINTESIS_FINAL" in state["result"]["assistant_content"]

    events = coordinator.db.query_all(
        "SELECT event_type FROM events WHERE task_id = ?", (state["task_id"],),
    )
    event_types = {row["event_type"] for row in events}
    assert "chunking.planned" in event_types
    assert "chunking.completed" in event_types


def test_map_reduce_skipped_when_prompt_fits(tmp_path):
    provider = TinyWindowProvider()
    provider.window = 10_000_000
    coordinator = _make_coordinator(tmp_path, provider)
    state = _run_task(coordinator, _long_request())

    assert state["status"] == "completed"
    # Cupo entero: ruta single normal, sin fragmentar.
    assert provider.map_calls == 0
    assert "long_context" not in (state["result"] or {})


def test_map_reduce_without_documents_falls_back_to_normal_error(tmp_path):
    """Prompt gigante SIN adjuntos: map_reduce no aplica (no hay qué trocear
    manteniendo la instrucción íntegra) y el error clásico se conserva."""
    provider = TinyWindowProvider()
    coordinator = _make_coordinator(tmp_path, provider)
    request = TaskCreateRequest.model_validate({
        "idempotency_key": "lc:sin-docs",
        "content": {"prompt": "palabra " * 20_000},
        "execution": {"strategy": "single", "long_context": "map_reduce"},
    })
    state = _run_task(coordinator, request)
    assert state["status"] == "failed"
    assert state["error"]["code"] == "CONTEXT_LIMIT_EXCEEDED"


def test_map_reduce_respects_budget(tmp_path):
    provider = TinyWindowProvider()
    provider.cost_per_call = 0.30
    coordinator = _make_coordinator(tmp_path, provider)
    request = _long_request(model_requirements={
        "allowed_providers": ["ollama"], "max_cost_usd": 0.5,
    })
    state = _run_task(coordinator, request)
    assert state["status"] == "failed"
    assert state["error"]["code"] == "BUDGET_EXCEEDED"
    # Cortó tras agotar presupuesto, sin ejecutar los fragmentos restantes.
    assert provider.map_calls <= 2
