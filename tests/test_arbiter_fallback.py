"""Regresiones de task_c8da5dd6…, donde el árbitro devolvió su propio prompt.

Las tres propuestas eran buenas y estaban pagadas —tres minutos de GPU— y la
tarea murió igualmente porque el último eslabón falló. Aquí se fija que un
árbitro caído es una baja, no el final del consenso, y que la salida que lo
tumbó queda guardada en vez de perderse.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import BrokerConfig, PersistenceConfig
from app.coordinator import ConsensusCoordinator
from app.db import Database
from app.providers import ModelOutput, ProviderError
from app.repository import TaskRepository
from app.resource_scheduler import ResourceScheduler
from app.schemas import ModelReference, TaskCreateRequest


class ArbiterProvider:
    """Tres proposers que responden y un árbitro que no, como aquel día."""

    def __init__(self, *, code: str = "PROMPT_ECHOED", echoed: str | None = None,
                 arbiters_ok: set[str] | None = None) -> None:
        self.code = code
        self.echoed = echoed
        self.arbiters_ok = arbiters_ok or set()
        self.synthesis_attempts: list[str] = []

    async def select(self, request, count, roles):
        names = ["arbitro_1", "arbitro_2"] if roles[0] == "arbiter" else ["prop_1", "prop_2", "prop_3"]
        return [
            ModelReference(
                provider="ollama", deployment="local", model=names[index % len(names)], role=role,
            )
            for index, role in enumerate(roles)
        ]

    async def models(self):
        return [
            {"provider": "ollama", "deployment": "local", "name": name,
             "context_window": 32768, "capabilities": ["completion"], "size_bytes": 1}
            for name in ["prop_1", "prop_2", "prop_3", "arbitro_1", "arbitro_2"]
        ]

    async def local_footprints(self, models):
        return [1 for _ in models]

    async def propose(self, request, model, ordinal):
        return ModelOutput(
            content=f"respuesta completa de {model.model}", tokens_input=10, tokens_output=5,
            cost_usd=0.0, latency_ms=1.0,
        )

    async def synthesize(self, request, model, proposals):
        self.synthesis_attempts.append(model.model)
        if model.model in self.arbiters_ok:
            return ModelOutput(
                content=f"sintesis de {len(proposals)} propuestas", tokens_input=10,
                tokens_output=5, cost_usd=0.0, latency_ms=1.0,
            )
        error = ProviderError(self.code, f"{model.model} no sintetizó", retryable=False)
        if self.echoed is not None:
            error.output = ModelOutput(
                content=self.echoed, tokens_input=9000, tokens_output=4000,
                cost_usd=0.25, latency_ms=118000.0,
            )
        raise error

    async def close(self):
        pass


def _request(**overrides) -> TaskCreateRequest:
    payload = {
        "idempotency_key": "arbiter:fallback",
        "content": {"prompt": "Contesta en español"},
        "execution": {"strategy": "mixture_of_agents", "preset": "slow", "max_proposers": 3},
        **overrides,
    }
    return TaskCreateRequest.model_validate(payload)


def _run(tmp_path: Path, provider, request: TaskCreateRequest | None = None):
    config = BrokerConfig(persistence=PersistenceConfig(database=str(tmp_path / "arbiter.db")))
    db = Database(tmp_path / "arbiter.db")
    db.init_schema()
    coordinator = ConsensusCoordinator(db, ResourceScheduler(config), provider=provider)
    repository = TaskRepository(db)
    task, _ = repository.create_task(request or _request(), queue_max_size=100)
    asyncio.run(coordinator.process_task(repository, task.task_id))
    return repository.get_task(task.task_id).model_dump(mode="json"), db


class TestTheTaskSurvivesItsArbiter:
    def test_a_second_arbiter_takes_over_when_the_first_echoes(self, tmp_path) -> None:
        provider = ArbiterProvider(arbiters_ok={"arbitro_2"})
        state, _ = _run(tmp_path, provider)
        assert state["status"] == "completed"
        assert provider.synthesis_attempts == ["arbitro_1", "arbitro_2"]
        assert state["result"]["assistant_content"] == "sintesis de 3 propuestas"
        assert state["result"]["consensus"]["synthesized"] is True
        assert state["result"]["model_used"]["model"] == "arbitro_2"

    def test_without_any_arbiter_the_best_proposal_answers(self, tmp_path) -> None:
        provider = ArbiterProvider()
        state, _ = _run(tmp_path, provider)
        # Antes esto era una tarea 'failed' con tres respuestas válidas dentro.
        assert state["status"] == "completed"
        assert state["result"]["assistant_content"] == "respuesta completa de prop_1"
        assert state["result"]["consensus"]["synthesized"] is False
        assert state["result"]["model_used"]["model"] == "prop_1"

    def test_the_degradation_is_visible_not_silenciosa(self, tmp_path) -> None:
        state, db = _run(tmp_path, ArbiterProvider())
        failures = state["result"]["arbiter_failures"]
        assert [item["model"] for item in failures] == ["arbitro_1", "arbitro_2"]
        assert all(item["code"] == "PROMPT_ECHOED" for item in failures)
        assert any("sin síntesis" in warning for warning in state["result"]["consensus"]["warnings"])
        events = db.query_all(
            "SELECT event_type FROM events WHERE event_type IN ('arbiter.failed', 'arbiter.fallback')"
        )
        assert [row["event_type"] for row in events] == [
            "arbiter.failed", "arbiter.failed", "arbiter.fallback",
        ]
        stage = db.query_all("SELECT status FROM stages WHERE stage_type = 'synthesizing'")
        assert stage[0]["status"] == "degraded"

    def test_a_degraded_answer_is_not_charged_twice(self, tmp_path) -> None:
        """La propuesta que responde ya está contada como propuesta: sumarla
        otra vez como síntesis inventaría una inferencia que no existió."""
        state, _ = _run(tmp_path, ArbiterProvider())
        assert state["result"]["usage"]["invocations"] == 3
        assert state["result"]["usage"]["tokens_output"] == 15
        assert [item["model"] for item in state["result"]["models_used"]] == [
            "prop_1", "prop_2", "prop_3",
        ]

    def test_a_cancelled_task_is_not_rescued(self, tmp_path) -> None:
        """La degradación es para árbitros rotos, no para tareas que ya no
        interesan: TASK_CANCELLED sigue siendo terminal."""
        provider = ArbiterProvider(code="TASK_CANCELLED")
        state, _ = _run(tmp_path, provider)
        assert state["status"] == "failed"
        assert provider.synthesis_attempts == ["arbitro_1"]

    def test_a_hand_picked_arbiter_is_not_swapped(self, tmp_path) -> None:
        """Si el cliente eligió árbitro, el modelo es parte de lo pedido: se
        degrada, pero no se cambia por otro a su espalda."""
        provider = ArbiterProvider()
        request = _request(execution={
            "strategy": "mixture_of_agents", "preset": "slow", "max_proposers": 3,
            "selection": {
                "mode": "auto", "proposer_count": 3,
                "preferred_arbiter": {
                    "provider": "ollama", "deployment": "local", "model": "arbitro_1",
                },
            },
        })
        state, _ = _run(tmp_path, provider, request)
        assert provider.synthesis_attempts == ["arbitro_1"]
        assert state["status"] == "completed"
        assert state["result"]["consensus"]["synthesized"] is False


class TestTheRejectedOutputIsKept:
    def test_the_echoed_text_lands_in_the_invocation(self, tmp_path) -> None:
        """Sin esto, de la respuesta que rompió la tarea solo quedaba el
        porcentaje del mensaje de error: nada que auditar."""
        _, db = _run(tmp_path, ArbiterProvider(echoed="fallo en " * 500))
        rows = db.query_all(
            "SELECT output_json, tokens_output, cost_usd, latency_ms FROM model_invocations "
            "WHERE status = 'failed' ORDER BY started_at"
        )
        assert len(rows) == 2
        assert "fallo en fallo en" in rows[0]["output_json"]
        # El gasto ocurrió aunque la respuesta se rechazara: contarlo a cero
        # borraba de las métricas dos minutos de GPU por invocación.
        assert rows[0]["tokens_output"] == 4000
        assert rows[0]["cost_usd"] == 0.25
        assert rows[0]["latency_ms"] == 118000.0

    def test_a_failure_without_response_stays_empty(self, tmp_path) -> None:
        """Un fallo de transporte no trae salida que guardar y la fila no se
        inventa ninguna."""
        _, db = _run(tmp_path, ArbiterProvider(code="INVALID_PROVIDER_RESPONSE"))
        rows = db.query_all(
            "SELECT output_json, tokens_output FROM model_invocations WHERE status = 'failed'"
        )
        assert all(row["output_json"] is None and row["tokens_output"] == 0 for row in rows)
