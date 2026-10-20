"""Un agente en marcha cede el turno entre iteraciones a una tarea más urgente.

El broker ejecuta una inferencia a la vez. Sin esto, una tarea con `priority`
baja (urgente) podía ponerse la primera de la cola y aun así esperar a que una
investigación de diez pasos terminara entera. Ahora el agente, al cerrar una
iteración, mira si espera algo más urgente: si lo hay, congela la conversación
—el mismo estado que usa `waiting_for_tools`— y vuelve a la cola detrás de lo
urgente. Al retomarse sigue donde quedó, sin repetir la vuelta ya pagada.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.schemas import TaskCreateRequest


def _client(tmp_path: Path, **processing) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap", **processing),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def _agent(client: TestClient, key: str, priority: int = 100, strategy: str = "agent") -> str:
    created = client.post("/api/v1/tasks", json={
        "idempotency_key": key,
        "content": {"prompt": "Investiga el tiempo"},
        "priority": priority,
        "execution": {"strategy": strategy, "agent": {"skills": ["web_search"], "max_iterations": 4}},
    })
    assert created.status_code == 202
    return created.json()["task_id"]


def _arrive_mid_run(monkeypatch, client: TestClient, priority: int) -> list[str]:
    """Hace que una tarea nueva entre en la cola mientras el agente ejecuta su
    primera skill, que es justo cuando una app la mandaría en la vida real."""
    arrived: list[str] = []

    async def skill_with_arrival(name, arguments, **kwargs):
        if not arrived:
            request = TaskCreateRequest.model_validate({
                "idempotency_key": f"arrival-{priority}",
                "content": {"prompt": "Respuesta rápida para la app"},
                "priority": priority,
                "execution": {"strategy": "single"},
            })
            task, _ = client.app.state.repository.create_task(request, queue_max_size=100)
            arrived.append(task.task_id)
        return "resultado simulado"

    monkeypatch.setattr("app.coordinator.run_skill", skill_with_arrival)
    return arrived


def _events(client: TestClient, task_id: str, event_type: str) -> list[dict]:
    detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()
    return [e for e in detail["events"] if e["event_type"] == event_type]


def test_agent_yields_to_an_urgent_task_and_resumes_where_it_left(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        agent_id = _agent(client, "agent:yield")
        arrived = _arrive_mid_run(monkeypatch, client, priority=10)

        first = client.post("/api/v1/dispatcher/tick").json()
        assert first["task_id"] == agent_id
        # Cedió tras su primera iteración: vuelve a la cola, no ha terminado.
        assert client.get(f"/api/v1/tasks/{agent_id}").json()["status"] == "queued"
        yielded = _events(client, agent_id, "agent.yielded")
        assert yielded and yielded[0]["payload"]["urgent_task_id"] == arrived[0]

        # El siguiente turno es de la urgente…
        second = client.post("/api/v1/dispatcher/tick").json()
        assert second["task_id"] == arrived[0]
        assert client.get(f"/api/v1/tasks/{arrived[0]}").json()["status"] == "completed"

        # …y luego el agente sigue donde estaba.
        third = client.post("/api/v1/dispatcher/tick").json()
        assert third["task_id"] == agent_id
        done = client.get(f"/api/v1/tasks/{agent_id}").json()

    assert done["status"] == "completed"
    assert "Respuesta final del agente" in done["result"]["assistant_content"]
    # La vuelta ya pagada no se repite: una pidió la skill, la otra concluyó.
    assert done["result"]["agent"]["iterations"] == 2
    assert done["result"]["usage"]["invocations"] == 2


def test_agent_does_not_yield_to_a_task_that_is_not_more_urgent(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        agent_id = _agent(client, "agent:no-yield")
        arrived = _arrive_mid_run(monkeypatch, client, priority=100)

        client.post("/api/v1/dispatcher/tick")

        assert client.get(f"/api/v1/tasks/{agent_id}").json()["status"] == "completed"
        assert client.get(f"/api/v1/tasks/{arrived[0]}").json()["status"] == "queued"
        assert not _events(client, agent_id, "agent.yielded")


def test_yielding_can_be_turned_off(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path, yield_to_urgent_tasks=False) as client:
        agent_id = _agent(client, "agent:yield-off")
        _arrive_mid_run(monkeypatch, client, priority=10)

        client.post("/api/v1/dispatcher/tick")

        assert client.get(f"/api/v1/tasks/{agent_id}").json()["status"] == "completed"
        assert not _events(client, agent_id, "agent.yielded")
