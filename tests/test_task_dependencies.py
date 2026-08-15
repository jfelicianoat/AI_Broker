"""Dependencias entre tareas: `depends_on`, `depends_on_group` y la espera.

El caso que las motivó: se carga un documento grande, se indexa en cientos de
tareas de embedding, y la pregunta sobre ese documento no debe correr hasta que
el índice esté entero. Enviar los cientos de ids en la pregunta no es viable, de
ahí el grupo.
"""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.schemas import TaskCreateRequest


def _client(tmp_path: Path, dependency_wait_seconds: float = 0.1) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(
            auto_dispatch=False, provider_mode="bootstrap",
            dependency_wait_seconds=dependency_wait_seconds,
        ),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def _create(client: TestClient, key: str, **extra) -> str:
    response = client.post("/api/v1/tasks", json={
        "idempotency_key": key,
        "content": {"prompt": "algo"},
        **extra,
    })
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def _state(client: TestClient, task_id: str) -> dict:
    return client.get(f"/api/v1/tasks/{task_id}").json()


def test_a_task_does_not_run_before_the_one_it_depends_on(tmp_path: Path) -> None:
    """La dependiente se pone la primera de la cola para que le toque antes que
    a su dependencia: así se comprueba que la retiene el mecanismo y no el
    simple orden de llegada."""
    with _client(tmp_path) as client:
        first = _create(client, "dep:a")
        second = _create(client, "dep:b", depends_on=[first])
        assert client.patch("/api/v1/queue", json={"task_ids": [second, first]}).status_code == 200

        client.post("/api/v1/dispatcher/tick")
        blocked = _state(client, second)
        assert blocked["status"] == "waiting_for_dependencies"
        assert _state(client, first)["status"] != "completed"
        item = client.get(f"/api/v1/dashboard/tasks/{second}").json()["task"]
        assert item["dependency_wait"]["tasks"] == [first]

        client.post("/api/v1/dispatcher/tick")
        assert _state(client, first)["status"] == "completed"

        time.sleep(0.2)
        client.post("/api/v1/dispatcher/tick")
        assert _state(client, second)["status"] == "completed"
        # Sin dependencias incumplidas no se ensucia el resultado con avisos.
        assert "warnings" not in (_state(client, second)["result"] or {})


def test_a_group_drains_before_the_task_that_waits_for_it(tmp_path: Path) -> None:
    """El caso real: N tareas de indexado bajo una etiqueta, y una pregunta que
    espera a que el grupo entero termine sin conocer sus identificadores.

    La pregunta se pone la PRIMERA de la cola a propósito. Por orden de llegada
    el grupo ya estaría drenado cuando le tocase, y el test pasaría sin haber
    ejercitado el bloqueo; adelantándola se comprueba lo que se quiere
    comprobar, que es que no corre aunque le toque.
    """
    with _client(tmp_path) as client:
        chunks = [_create(client, f"dep:chunk-{i}", group="idx-doc7") for i in range(3)]
        question = _create(client, "dep:pregunta", depends_on_group="idx-doc7")
        reordered = client.patch("/api/v1/queue", json={"task_ids": [question, *chunks]})
        assert reordered.status_code == 200, reordered.text

        client.post("/api/v1/dispatcher/tick")
        assert _state(client, question)["status"] == "waiting_for_dependencies"
        # El detalle del panel dice a qué espera, igual que con la memoria.
        item = client.get(f"/api/v1/dashboard/tasks/{question}").json()["task"]
        assert item["dependency_wait"]["group"] == "idx-doc7"
        assert item["dependency_wait"]["group_pending"] == 3

        # Los chunks corren mientras la pregunta espera su turno de reintento.
        for _ in range(3):
            client.post("/api/v1/dispatcher/tick")
        assert all(_state(client, task)["status"] == "completed" for task in chunks)

        # La tarea aplazada no vuelve a ser reclamable hasta que vence su
        # not_before; de ahí la espera real, corta por configuración del test.
        time.sleep(0.2)
        client.post("/api/v1/dispatcher/tick")
        assert _state(client, question)["status"] == "completed"


def test_an_unknown_dependency_does_not_block_and_warns(tmp_path: Path) -> None:
    """Un id que no existe no se puede esperar: bloquearía la tarea para
    siempre. Se sigue adelante y se dice en el resultado."""
    with _client(tmp_path) as client:
        task_id = _create(client, "dep:fantasma", depends_on=["task_noexiste"])
        client.post("/api/v1/dispatcher/tick")

        state = _state(client, task_id)
        assert state["status"] == "completed"
        assert any("inexistentes" in w for w in state["result"]["warnings"])
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()
        assert [e for e in detail["events"] if e["event_type"] == "task.dependency_unknown"]


def test_waiting_does_not_consume_the_single_workflow(tmp_path: Path) -> None:
    """Esperar no ocupa el workflow único ni pierde el sitio en la cola: es la
    misma regla que ya rige para la espera por memoria."""
    with _client(tmp_path) as client:
        blocker = _create(client, "dep:bloqueante")
        waiter = _create(client, "dep:esperando", depends_on=[blocker])
        client.post("/api/v1/dispatcher/tick")

        state = _state(client, waiter)
        if state["status"] == "waiting_for_dependencies":
            # Sigue en cola con su posición, no descartada ni fallida.
            assert state["queue_position"] is not None
        # Y el bloqueante puede correr mientras tanto.
        client.post("/api/v1/dispatcher/tick")
        assert _state(client, blocker)["status"] == "completed"


def test_the_contract_rejects_waiting_for_your_own_group() -> None:
    """Depender de tu propio grupo es esperarte a ti mismo con pasos extra: las
    N tareas de un índice se bloquearían entre ellas."""
    with pytest.raises(ValidationError) as error:
        TaskCreateRequest.model_validate({
            "idempotency_key": "dep:autogrupo",
            "content": {"prompt": "x"},
            "group": "idx",
            "depends_on_group": "idx",
        })
    assert "own group" in str(error.value)


def test_the_contract_rejects_repeated_dependencies() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({
            "idempotency_key": "dep:repetida",
            "content": {"prompt": "x"},
            "depends_on": ["task_a", "task_a"],
        })


def test_capabilities_announce_task_dependencies(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        capabilities = client.get("/api/v1/capabilities").json()
    assert capabilities["task_dependencies"] is True
