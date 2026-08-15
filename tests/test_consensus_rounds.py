"""Segunda ronda del consenso: los proponentes ven la síntesis y la mejoran.

`max_rounds` era un campo del contrato que se aceptaba, se persistía en los
límites del run y no hacía nada. Aquí se ejercita lo que hace ahora, y sobre
todo el invariante que lo hace seguro: una segunda ronda no puede dejar la tarea
peor que si no se hubiera intentado.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import (
    BrokerConfig,
    PersistenceConfig,
    ProcessingConfig,
    StrategyRouterConfig,
)
from app.main import create_app
from app.providers.base import ModelOutput, ProviderError
from app.providers.bootstrap import BootstrapModelProvider


def _client(tmp_path: Path, threshold: float = 0.6) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        strategy_router=StrategyRouterConfig(enabled=True, escalation_min_confidence=threshold),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def _judge_scoring(monkeypatch, score: str) -> None:
    """El juez devuelve una puntuación fija; el resto de propuestas, normales."""
    original = BootstrapModelProvider.propose

    async def judging_propose(self, request, model, ordinal):
        if "PREGUNTA" in request.content.prompt and "RESPUESTA" in request.content.prompt:
            return ModelOutput(score, 5, 1, 0.0, 1.0)
        return await original(self, request, model, ordinal)

    monkeypatch.setattr(BootstrapModelProvider, "propose", judging_propose)


def _run(client: TestClient, key: str, **execution) -> dict:
    created = client.post("/api/v1/tasks", json={
        "idempotency_key": key,
        "content": {"prompt": "Compara Postgres y MySQL"},
        "execution": {"strategy": "mixture_of_agents", "max_proposers": 2, **execution},
    })
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    client.post("/api/v1/dispatcher/tick")
    return client.get(f"/api/v1/dashboard/tasks/{task_id}").json()


def test_second_round_refines_when_the_judge_is_not_convinced(tmp_path: Path, monkeypatch) -> None:
    _judge_scoring(monkeypatch, "0.2")
    with _client(tmp_path) as client:
        detail = _run(client, "rounds:refine", max_rounds=2)

    assert detail["task"]["status"] == "completed"
    consensus = detail["result"]["consensus"]
    assert consensus["rounds"] == 2
    assert consensus["confidence"] == 0.2
    gate = [e for e in detail["events"] if e["event_type"] == "consensus.round_gate"]
    assert gate and gate[0]["payload"]["second_round"] is True
    # El proveedor bootstrap devuelve el prompt recibido, así que la respuesta
    # final demuestra qué vieron los proponentes de la segunda ronda.
    answer = detail["result"]["assistant_content"]
    assert "<previous_synthesis>" in answer
    assert "refiner" in answer


def test_second_round_is_skipped_when_the_judge_is_convinced(tmp_path: Path, monkeypatch) -> None:
    """El techo no se gasta si la primera ronda ya está bien: `max_rounds` es un
    máximo, no una orden."""
    _judge_scoring(monkeypatch, "0.95")
    with _client(tmp_path) as client:
        detail = _run(client, "rounds:convinced", max_rounds=2)

    consensus = detail["result"]["consensus"]
    assert consensus["rounds"] == 1
    assert consensus["confidence"] == 0.95
    gate = [e for e in detail["events"] if e["event_type"] == "consensus.round_gate"]
    assert gate and gate[0]["payload"]["second_round"] is False
    assert "<previous_synthesis>" not in detail["result"]["assistant_content"]


def test_second_round_runs_unjudged_when_the_client_declines_a_judge(tmp_path: Path) -> None:
    """`max_judges: 0` es "no pagues por juzgar": entonces pedir dos rondas se
    lee literalmente y la segunda se gasta siempre."""
    with _client(tmp_path) as client:
        detail = _run(client, "rounds:nojudge", max_rounds=2, max_judges=0)

    consensus = detail["result"]["consensus"]
    assert consensus["rounds"] == 2
    assert consensus["confidence"] is None
    gate = [e for e in detail["events"] if e["event_type"] == "consensus.round_gate"]
    assert gate and gate[0]["payload"]["score"] is None


def test_one_round_by_default_and_no_judge_is_paid(tmp_path: Path) -> None:
    """El default (`max_rounds: 1`) no cambia nada: ni segunda ronda ni juez."""
    with _client(tmp_path) as client:
        detail = _run(client, "rounds:default")

    assert detail["result"]["consensus"]["rounds"] == 1
    assert detail["result"]["consensus"]["confidence"] is None
    types = [e["event_type"] for e in detail["events"]]
    assert "consensus.round_gate" not in types
    roles = [i["role"] for i in detail["invocations"]]
    assert "confidence_judge" not in roles


def test_a_failed_second_round_keeps_the_first(tmp_path: Path, monkeypatch) -> None:
    """El invariante que hace segura la ronda 2: si revienta —aquí con un error
    definitivo de tarea, que en la ronda 1 tumbaría el consenso entero— la tarea
    entrega la síntesis de la ronda 1 en vez de fallar."""
    original = BootstrapModelProvider.propose

    async def fails_for_refiners(self, request, model, ordinal):
        if "PREGUNTA" in request.content.prompt and "RESPUESTA" in request.content.prompt:
            return ModelOutput("0.1", 5, 1, 0.0, 1.0)
        if model.role == "refiner":
            raise ProviderError("CONTEXT_LIMIT_EXCEEDED", "la síntesis añadida no cabe")
        return await original(self, request, model, ordinal)

    monkeypatch.setattr(BootstrapModelProvider, "propose", fails_for_refiners)
    with _client(tmp_path) as client:
        detail = _run(client, "rounds:failed", max_rounds=2)

    assert detail["task"]["status"] == "completed"
    consensus = detail["result"]["consensus"]
    assert consensus["rounds"] == 1
    assert "Síntesis" in detail["result"]["assistant_content"]
    failed = [e for e in detail["events"] if e["event_type"] == "consensus.round_failed"]
    assert failed and failed[0]["payload"]["round"] == 2


def test_progress_never_exceeds_its_declared_total(tmp_path: Path, monkeypatch) -> None:
    """La barra cuenta invocaciones contra un total declarado. Con dos rondas el
    total tiene que subir al empezar la segunda; si no, marcaría 4 de 3."""
    _judge_scoring(monkeypatch, "0.2")
    with _client(tmp_path) as client:
        detail = _run(client, "rounds:progress", max_rounds=2)

    progress = detail["progress"]
    assert progress["invocations_completed"] <= progress["invocations_total"]
    # 2 proponentes × 2 rondas + 2 árbitros + 1 juez.
    assert progress["invocations_total"] == 7


def test_second_round_costs_are_not_lost_from_the_usage(tmp_path: Path, monkeypatch) -> None:
    """Lo de la ronda 1 sale del resultado pero no del recuento: se pagó."""
    _judge_scoring(monkeypatch, "0.2")
    with _client(tmp_path) as client:
        detail = _run(client, "rounds:usage", max_rounds=2)

    # 2 proponentes + árbitro por ronda (4 + 2), más el juez.
    assert detail["result"]["usage"]["invocations"] == 7
