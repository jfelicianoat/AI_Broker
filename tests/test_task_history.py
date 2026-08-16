"""Histórico de tareas: la vista que permite encontrar una tarea entre miles.

El panel de Tareas solo enseña las últimas. En una tirada de cientos, lo
anterior desaparece de la vista aunque siga en la base de datos, y ahí es donde
está justo lo que se busca: qué falló, con qué modelo y por qué.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig, ServerConfig
from app.dashboard import DashboardQueryRepository
from app.db import Database, dumps_json
from app.main import create_app
from app.schemas import TaskKind, TaskStatus


def _task(
    db: Database,
    *,
    task_id: str,
    status: str = "completed",
    origin: str = "model_drift",
    models: tuple[tuple[str, str, str], ...] = (("ollama", "local", "qwen3:8b"),),
    error_code: str | None = None,
    excluded: bool = False,
    minutes_ago: float = 5,
    duration_seconds: float = 12.0,
    kind: str = "inference",
    request_id: str | None = None,
) -> None:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    updated = created + timedelta(seconds=duration_seconds)
    request = {
        "idempotency_key": task_id,
        "content": {"prompt": "hola", "metadata": {"origin": origin}},
        "exclude_from_model_learning": excluded,
        "execution": {"strategy": "single", "preset": "fast"},
    }
    db.execute(
        "INSERT INTO tasks (id, request_id, request_json, kind, status, priority, "
        "progress_json, result_json, error_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 100, '{}', ?, ?, ?, ?)",
        (
            task_id, request_id, dumps_json(request), kind, status,
            dumps_json({"model_used": {
                "provider": models[0][0], "deployment": models[0][1], "model": models[0][2],
            }}) if models and status == "completed" else None,
            dumps_json({"code": error_code, "message": "x", "retryable": False}) if error_code else None,
            created.isoformat(), updated.isoformat(),
        ),
    )
    for provider, deployment, model in models:
        db.execute(
            "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, "
            "task_type, status, tokens_input, tokens_output, cost_usd, latency_ms, "
            "excluded_from_learning, created_at, updated_at) "
            "VALUES (?, ?, 'single', ?, ?, ?, 'prose', ?, 10, 20, 0.25, 1000, ?, ?, ?)",
            (
                f"inv_{uuid4().hex}", task_id, provider, deployment, model,
                "completed" if status == "completed" else "failed",
                int(excluded), created.isoformat(), updated.isoformat(),
            ),
        )


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "history.db")
    db.init_schema()
    return db


def _seed(db: Database) -> None:
    _task(db, task_id="task_ok_ollama", status="completed",
          models=(("ollama", "local", "qwen3:8b"),), minutes_ago=10)
    _task(db, task_id="task_fail_vram", status="failed", error_code="VRAM_MODEL_TOO_LARGE",
          models=(("ollama", "local", "laguna-s"),), minutes_ago=8, excluded=True)
    _task(db, task_id="task_ok_cloud", status="completed", origin="chatygpt",
          models=(("nvidia", "cloud", "deepseek-v4"),), minutes_ago=6)
    _task(db, task_id="task_mixture", status="completed", origin="prompt_tester",
          models=(("ollama", "local", "qwen3:8b"), ("nvidia", "cloud", "deepseek-v4")),
          minutes_ago=4, request_id="req-buscable")
    _task(db, task_id="task_cancelled", status="cancelled", models=(), minutes_ago=2)


# --------------------------------------------------------------------------
# Consultas
# --------------------------------------------------------------------------


def test_the_history_only_holds_finished_tasks(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)
    _task(db, task_id="task_viva", status="generating")

    page = DashboardQueryRepository(db).list_history()

    ids = {item.task_id for item in page.items}
    assert "task_viva" not in ids
    assert page.total == 5
    db.close()


def test_filtering_by_model_finds_tasks_where_it_merely_took_part(tmp_path: Path) -> None:
    """La diferencia entre buscar por invocación y buscar por resultado: en un
    mixture intervienen varios modelos y solo uno queda en `model_used`."""
    db = _db(tmp_path)
    _seed(db)

    found = DashboardQueryRepository(db).list_history(model="deepseek-v4")

    ids = {item.task_id for item in found.items}
    # task_mixture lo usó como segundo proponente, no como modelo final.
    assert ids == {"task_ok_cloud", "task_mixture"}
    db.close()


def test_a_failed_task_that_never_produced_a_result_is_still_findable(tmp_path: Path) -> None:
    """Una tarea fallida no tiene `model_used`, pero sí invocó a alguien: sin
    esto, buscar «qué pasó con este modelo» escondería justo los fallos."""
    db = _db(tmp_path)
    _seed(db)

    found = DashboardQueryRepository(db).list_history(model="laguna-s")

    assert [item.task_id for item in found.items] == ["task_fail_vram"]
    assert found.items[0].error_code == "VRAM_MODEL_TOO_LARGE"
    db.close()


def test_filters_by_provider_status_origin_and_learning(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)
    queries = DashboardQueryRepository(db)

    # Tres, no dos: el mixture también usó ollama aunque no fuera su modelo final.
    assert queries.list_history(provider="ollama").total == 3
    assert queries.list_history(provider="OLLAMA").total == 3, "sin distinguir mayúsculas"
    assert queries.list_history(status=TaskStatus.failed).total == 1
    assert queries.list_history(origin="chatygpt").total == 1
    assert queries.list_history(excluded=True).total == 1
    assert queries.list_history(excluded=False).total == 4
    assert queries.list_history(kind=TaskKind.inference).total == 5
    db.close()


def test_filters_combine_instead_of_replacing_each_other(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)

    both = DashboardQueryRepository(db).list_history(
        provider="ollama", status=TaskStatus.completed,
    )

    # De las tres de ollama cae la fallida: los filtros se acumulan.
    assert [item.task_id for item in both.items] == ["task_mixture", "task_ok_ollama"]
    db.close()


def test_searching_by_identifier_matches_task_and_request_id(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)
    queries = DashboardQueryRepository(db)

    assert queries.list_history(search="task_fail").total == 1
    assert queries.list_history(search="req-buscable").total == 1
    db.close()


def test_the_date_range_narrows_by_when_the_task_finished(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)

    recent = DashboardQueryRepository(db).list_history(since=cutoff)

    assert {item.task_id for item in recent.items} == {"task_mixture", "task_cancelled"}
    db.close()


def test_results_are_paged_newest_first(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)
    queries = DashboardQueryRepository(db)

    first = queries.list_history(page=1, page_size=2)
    second = queries.list_history(page=2, page_size=2)

    assert first.total == 5 and first.total_pages == 3
    assert [item.task_id for item in first.items] == ["task_cancelled", "task_mixture"]
    assert len(second.items) == 2
    assert not {i.task_id for i in first.items} & {i.task_id for i in second.items}
    db.close()


def test_each_row_carries_what_the_search_was_for(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)

    page = DashboardQueryRepository(db).list_history(search="task_mixture")

    item = page.items[0]
    assert {model.model for model in item.models} == {"qwen3:8b", "deepseek-v4"}
    assert item.invocations == 2
    assert item.duration_seconds == 12.0
    assert item.cost_actual_usd == 0.5
    assert item.origin == "prompt_tester"
    db.close()


def test_a_task_without_invocations_does_not_break_the_row(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)

    page = DashboardQueryRepository(db).list_history(search="task_cancelled")

    assert page.items[0].models == []
    assert page.items[0].invocations == 0
    db.close()


def test_the_options_come_from_the_data_not_the_config(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed(db)

    options = DashboardQueryRepository(db).history_options()

    assert options.providers == ["nvidia", "ollama"]
    assert "laguna-s" in options.models
    assert set(options.origins) == {"chatygpt", "model_drift", "prompt_tester"}
    db.close()


# --------------------------------------------------------------------------
# La página
# --------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        server=ServerConfig(host="127.0.0.1"),
        persistence=PersistenceConfig(database=str(tmp_path / "history.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


def test_the_history_page_renders_with_its_filters(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _seed(Database(tmp_path / "history.db"))
        response = client.get("/dashboard/history")

    assert response.status_code == 200
    body = response.text
    assert "Histórico" in body
    # Los desplegables se rellenan con lo que existe de verdad.
    assert "laguna-s" in body and "nvidia" in body
    assert "VRAM_MODEL_TOO_LARGE" in body


def test_the_fragment_applies_filters_and_can_be_swapped_alone(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _seed(Database(tmp_path / "history.db"))
        filtered = client.get(
            "/dashboard/fragments/task-history", params={"status": "failed"},
        )

    assert filtered.status_code == 200
    assert "task_fail_vram" in filtered.text
    assert "task_ok_ollama" not in filtered.text
    # Es un fragmento: no arrastra el documento entero.
    assert "<html" not in filtered.text


def test_a_bogus_filter_does_not_take_the_page_down(tmp_path: Path) -> None:
    """La query string la escribe cualquiera —un enlace guardado, un typo—: un
    valor que no existe se ignora en vez de devolver un 500."""
    with _client(tmp_path) as client:
        _seed(Database(tmp_path / "history.db"))
        response = client.get(
            "/dashboard/history",
            params={"status": "inventado", "since": "no-es-fecha", "page": "-3"},
        )

    assert response.status_code == 200


def test_the_tasks_page_points_to_the_full_history(tmp_path: Path) -> None:
    """El sitio donde se nota la falta es el panel de Tareas: ahí es donde se
    pierden las anteriores."""
    with _client(tmp_path) as client:
        response = client.get("/dashboard/tasks")

    assert response.status_code == 200
    assert "/dashboard/history" in response.text


# --------------------------------------------------------------------------
# Estado de una tirada (GET /api/v1/groups/{group})
# --------------------------------------------------------------------------


def _grouped(db: Database, task_id: str, group: str, status: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO tasks (id, request_json, task_group, status, priority, progress_json, "
        "created_at, updated_at) VALUES (?, '{}', ?, ?, 100, '{}', ?, ?)",
        (task_id, group, status, stamp, stamp),
    )


def test_a_group_reports_its_progress_at_a_glance(tmp_path: Path) -> None:
    """La pregunta que hace quien lanza una tanda: ¿ha terminado ya? Sin esto
    había que sondear cada identificador por separado, que es justo lo que
    empuja a mandar las tareas de una en una."""
    from app.repository import TaskRepository

    db = _db(tmp_path)
    for index, status in enumerate(["completed", "completed", "failed", "queued", "generating"]):
        _grouped(db, f"task_{index}", "tirada-1", status)

    state = TaskRepository(db).get_group_state("tirada-1")

    assert state.total == 5
    assert (state.completed, state.failed) == (2, 1)
    assert state.pending == 1
    assert state.active == 1
    assert state.finished is False
    db.close()


def test_a_group_is_finished_only_when_nothing_is_alive(tmp_path: Path) -> None:
    from app.repository import TaskRepository

    db = _db(tmp_path)
    _grouped(db, "task_a", "tirada-2", "completed")
    _grouped(db, "task_b", "tirada-2", "cancelled")
    _grouped(db, "task_c", "tirada-2", "failed")

    state = TaskRepository(db).get_group_state("tirada-2")

    # Terminada no es lo mismo que exitosa: una tirada con fallos ha terminado.
    assert state.finished is True
    assert state.total == state.completed + state.failed + state.cancelled
    db.close()


def test_waiting_states_count_as_pending_not_as_running(tmp_path: Path) -> None:
    """Una tarea esperando memoria o dependencias no está corriendo: contarla
    como activa haría creer que la máquina está ocupada cuando no lo está."""
    from app.repository import TaskRepository

    db = _db(tmp_path)
    for index, status in enumerate(
        ["waiting_for_memory", "waiting_for_dependencies", "waiting_for_tools", "queued"]
    ):
        _grouped(db, f"task_w{index}", "tirada-3", status)

    state = TaskRepository(db).get_group_state("tirada-3")

    assert state.pending == 4
    assert state.active == 0
    assert state.finished is False
    db.close()


def test_an_unknown_group_is_not_an_empty_one(tmp_path: Path) -> None:
    """Devolver ceros haría que un cliente diera por terminada una tirada que
    en realidad nunca llegó a encolarse — o cuyo nombre escribió mal."""
    import pytest

    from app.repository import TaskRepository

    db = _db(tmp_path)
    _grouped(db, "task_x", "tirada-real", "completed")

    with pytest.raises(KeyError):
        TaskRepository(db).get_group_state("tirada-que-no-existe")
    db.close()


def test_the_group_endpoint_answers_under_the_v1_contract(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "grupo:api",
            "content": {"prompt": "Hola"},
            "group": "tanda-api",
        })
        assert created.status_code == 202

        state = client.get("/api/v1/groups/tanda-api")
        missing = client.get("/api/v1/groups/no-existe")
        capabilities = client.get("/api/v1/capabilities").json()

    assert state.status_code == 200
    body = state.json()
    assert body["group"] == "tanda-api"
    assert body["total"] == 1 and body["pending"] == 1
    assert body["finished"] is False
    assert missing.status_code == 404
    assert capabilities["task_group_status"] is True


def test_the_history_can_be_narrowed_to_one_run(tmp_path: Path) -> None:
    """La tirada es la unidad en la que se lanza una evaluación, así que también
    debe serlo al revisarla."""
    db = _db(tmp_path)
    _seed(db)
    db.execute("UPDATE tasks SET task_group = 'run-1' WHERE id IN ('task_ok_ollama', 'task_fail_vram')")
    db.execute("UPDATE tasks SET task_group = 'run-2' WHERE id = 'task_ok_cloud'")

    queries = DashboardQueryRepository(db)

    assert queries.list_history(group="run-1").total == 2
    assert queries.list_history(group="run-2").total == 1
    # Combinado con el resto de filtros, como cualquier otro.
    assert queries.list_history(group="run-1", status=TaskStatus.failed).total == 1
    assert "run-1" in queries.history_options().groups
    db.close()
