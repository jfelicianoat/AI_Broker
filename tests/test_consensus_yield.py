"""Un mixture_of_agents en marcha cede el turno a una tarea más urgente.

Es el trabajo más largo del broker con modelos locales: varias propuestas, una
síntesis y a veces una segunda ronda. Cede entre oleadas de proponentes y, si
queda segunda ronda, tras la primera síntesis. Al ceder guarda lo pagado —las
propuestas, los modelos elegidos, el plan de oleadas y la síntesis— y al
retomar no repite ninguna llamada: es lo que distingue pausar de reiniciar.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.providers.bootstrap import BootstrapModelProvider
from app.schemas import TaskCreateRequest


def _client(tmp_path: Path, **processing) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap", **processing),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def _mixture(client: TestClient, key: str, **execution) -> str:
    created = client.post("/api/v1/tasks", json={
        "idempotency_key": key,
        "content": {"prompt": "Compara tres alternativas de arquitectura"},
        "execution": {
            "strategy": "mixture_of_agents", "preset": "fast", "max_proposers": 3, **execution,
        },
    })
    assert created.status_code == 202
    return created.json()["task_id"]


def _enqueue_urgent(client: TestClient, key: str) -> str:
    request = TaskCreateRequest.model_validate({
        "idempotency_key": key,
        "content": {"prompt": "Respuesta rápida para la app"},
        "priority": 10,
        "execution": {"strategy": "single"},
    })
    task, _ = client.app.state.repository.create_task(request, queue_max_size=100)
    return task.task_id


class _Calls:
    """Cuenta las llamadas al proveedor y mete una tarea urgente en la cola en
    la llamada elegida, como haría una app mientras el consenso trabaja."""

    def __init__(self, monkeypatch, client: TestClient, *, arrive_on: str, at_call: int = 1) -> None:
        self.propose = 0
        self.synthesize = 0
        self.arrived: list[str] = []
        original_propose = BootstrapModelProvider.propose
        original_synthesize = BootstrapModelProvider.synthesize
        calls = self

        async def propose(provider, request, model, ordinal):
            # La tarea urgente, una single, también pasa por aquí: solo cuentan
            # las llamadas del consenso.
            if request.execution.strategy.value == "mixture_of_agents":
                calls.propose += 1
                if arrive_on == "propose" and calls.propose == at_call:
                    calls.arrived.append(_enqueue_urgent(client, "urgent-propose"))
            return await original_propose(provider, request, model, ordinal)

        async def synthesize(provider, request, model, proposals):
            calls.synthesize += 1
            if arrive_on == "synthesize" and calls.synthesize == at_call:
                calls.arrived.append(_enqueue_urgent(client, "urgent-synthesize"))
            return await original_synthesize(provider, request, model, proposals)

        monkeypatch.setattr(BootstrapModelProvider, "propose", propose)
        monkeypatch.setattr(BootstrapModelProvider, "synthesize", synthesize)


def _events(client: TestClient, task_id: str, event_type: str) -> list[dict]:
    detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()
    return [e for e in detail["events"] if e["event_type"] == event_type]


def _tick(client: TestClient) -> str | None:
    return client.post("/api/v1/dispatcher/tick").json().get("task_id")


def test_mixture_yields_between_waves_and_resumes_without_repeating(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        mixture_id = _mixture(client, "mix:yield-waves")
        calls = _Calls(monkeypatch, client, arrive_on="propose", at_call=1)

        assert _tick(client) == mixture_id
        # Cedió tras la primera oleada: una propuesta pagada, ninguna síntesis.
        assert client.get(f"/api/v1/tasks/{mixture_id}").json()["status"] == "queued"
        yielded = _events(client, mixture_id, "consensus.yielded")
        assert yielded[0]["payload"]["urgent_task_id"] == calls.arrived[0]
        assert yielded[0]["payload"]["proposals"] == 1
        assert (calls.propose, calls.synthesize) == (1, 0)

        assert _tick(client) == calls.arrived[0]
        assert client.get(f"/api/v1/tasks/{calls.arrived[0]}").json()["status"] == "completed"

        assert _tick(client) == mixture_id
        done = client.get(f"/api/v1/tasks/{mixture_id}").json()

    assert done["status"] == "completed"
    # La propuesta de antes de ceder no se repite: tres en total, no cuatro.
    assert (calls.propose, calls.synthesize) == (3, 1)
    assert done["result"]["consensus"]["proposers_completed"] == 3
    # Propuestas + árbitro, con la propuesta del primer tramo incluida.
    assert done["result"]["usage"]["invocations"] == 4


def test_mixture_yields_after_the_first_synthesis_when_a_second_round_remains(
    tmp_path: Path, monkeypatch,
) -> None:
    with _client(tmp_path) as client:
        # max_judges=0: la segunda ronda se gasta siempre, sin juez de por medio.
        mixture_id = _mixture(client, "mix:yield-synthesis", max_rounds=2, max_judges=0)
        calls = _Calls(monkeypatch, client, arrive_on="synthesize", at_call=1)

        assert _tick(client) == mixture_id
        yielded = _events(client, mixture_id, "consensus.yielded")
        assert yielded and yielded[0]["payload"]["synthesized"] is True
        assert (calls.propose, calls.synthesize) == (3, 1)

        assert _tick(client) == calls.arrived[0]
        assert _tick(client) == mixture_id
        done = client.get(f"/api/v1/tasks/{mixture_id}").json()

    assert done["status"] == "completed"
    assert done["result"]["consensus"]["rounds"] == 2
    # Ronda 1 entera antes de ceder, ronda 2 entera después; nada repetido.
    assert (calls.propose, calls.synthesize) == (6, 2)


def test_mixture_does_not_yield_without_a_more_urgent_task(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        mixture_id = _mixture(client, "mix:no-urgent")
        calls = _Calls(monkeypatch, client, arrive_on="nothing")

        assert _tick(client) == mixture_id
        assert client.get(f"/api/v1/tasks/{mixture_id}").json()["status"] == "completed"
        assert not _events(client, mixture_id, "consensus.yielded")
        assert (calls.propose, calls.synthesize) == (3, 1)


def test_yielding_can_be_turned_off_for_mixtures(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path, yield_to_urgent_tasks=False) as client:
        mixture_id = _mixture(client, "mix:yield-off")
        _Calls(monkeypatch, client, arrive_on="propose", at_call=1)

        assert _tick(client) == mixture_id
        assert client.get(f"/api/v1/tasks/{mixture_id}").json()["status"] == "completed"
        assert not _events(client, mixture_id, "consensus.yielded")
