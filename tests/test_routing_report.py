from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig, StrategyRouterConfig
from app.main import create_app
from app.routing_report import BarRow, daily_chart, hbar_chart, report_summary, strategy_totals


def _client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        strategy_router=StrategyRouterConfig(enabled=True),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def _run_auto_tasks(client: TestClient, count: int) -> None:
    for i in range(count):
        client.post("/api/v1/tasks", json={
            "idempotency_key": f"report-{i}",
            "content": {"prompt": "¿Cuál es la capital de Francia?"},
            "execution": {"strategy": "auto"},
            "model_requirements": {"allowed_providers": ["ollama"]},
        })
        client.post("/api/v1/dispatcher/tick")


def test_routing_tab_marks_every_table_sortable_and_links_report(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _run_auto_tasks(client, 2)
        page = client.get("/dashboard/routing").text

    tables = re.findall(r"<table[^>]*>", page)
    assert tables, "la pestaña debería tener tablas con datos"
    assert all("data-sortable" in tag for tag in tables)
    assert 'action="/dashboard/routing/report"' in page


def test_routing_report_renders_charts_tables_and_own_csp(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _run_auto_tasks(client, 3)
        response = client.get("/dashboard/routing/report")

    assert response.status_code == 200
    body = response.text
    assert "Informe de enrutamiento" in body
    assert body.count("<svg") >= 2
    assert "<table>" in body
    assert "petición directa" in body
    # El script de imprimir va en línea con nonce; la CSP global lo bloquearía.
    csp = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce is not None
    assert f'<script nonce="{nonce.group(1)}">' in body
    assert "content-disposition" not in response.headers
    assert 'href="/dashboard/routing/report?download=1"' in body


def test_routing_report_download_is_attachment_without_panel_links(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/dashboard/routing/report?download=1")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert re.search(r'filename="informe-enrutamiento-\d{8}-\d{4}\.html"', disposition)
    # Abierto desde disco no hay broker detrás: los enlaces al panel sobran.
    assert "/dashboard/routing" not in response.text
    assert "Aún no hay casos del meta-router" in response.text


def test_hbar_chart_scales_to_max_and_escapes_labels() -> None:
    svg = hbar_chart(
        [BarRow("<modelo>", 50, "50 %"), BarRow("otro", 0, "0 %")],
        title="Éxito", max_value=100,
    )
    assert "&lt;modelo&gt;" in svg
    assert "<modelo>" not in svg
    # La pista mide 410 px de ancho útil: el 50 % ocupa la mitad.
    assert 'width="205.0"' in svg
    assert hbar_chart([], title="vacío") == ""


def test_daily_chart_has_one_group_per_day_and_stacks_failures() -> None:
    activity = [
        {"day": "2026-10-20", "completed": 3, "failed": 1, "routing_cases": 2},
        {"day": "2026-10-21", "completed": 0, "failed": 0, "routing_cases": 0},
    ]
    svg = daily_chart(activity, title="Actividad")
    assert svg.count("<g>") == 2
    assert "20/10/2026: 3 completadas, 1 fallidas, 2 casos del meta-router" in svg
    assert "var(--status-critical)" in svg


def test_summary_weights_success_by_attempts_not_by_row() -> None:
    buckets = [{
        "total": 4, "recommendation": None,
        "strategies": [
            {"name": "single", "total": 3, "completed": 3, "success_rate": 100},
            {"name": "mixture_of_agents", "total": 1, "completed": 0, "success_rate": 0},
        ],
    }]
    # success_rate es el suavizado (Laplace): el resumen no debe usarlo.
    model_routing = {"groups": [{"rows": [
        {"provider": "a", "model": "x", "attempts": 9, "successes": 9, "success_rate": 91},
        {"provider": "b", "model": "y", "attempts": 1, "successes": 0, "success_rate": 33},
    ]}]}
    summary = report_summary(buckets, model_routing, [])
    assert summary["success_rate"] == 90
    assert summary["models"] == 2
    totals = strategy_totals(buckets + buckets)
    assert totals[0] == {"name": "single", "total": 6, "completed": 6, "success_rate": 100}


def test_daily_activity_adds_up_to_the_model_tables(tmp_path: Path) -> None:
    """La suma de la actividad diaria tiene que ser el total de invocaciones
    del resumen: si cada cifra sale de una población distinta, el informe se
    contradice a sí mismo."""
    with _client(tmp_path) as client:
        _run_auto_tasks(client, 3)
        body = client.get("/dashboard/routing/report").text

    rows = re.findall(
        r"<tr><td>\d\d/\d\d/\d{4}</td><td class=\"num\">(\d+)</td><td class=\"num\">(\d+)</td>", body,
    )
    assert rows
    daily_total = sum(int(done) + int(failed) for done, failed in rows)
    kpi = re.search(r'<div class="kpi"><strong>(\d+)</strong><span>llamadas a modelos en la ventana', body)
    assert kpi is not None
    assert int(kpi.group(1)) == daily_total


def test_routing_tab_says_when_data_boundary_overrode_the_heuristic(tmp_path: Path) -> None:
    """Con `local_only` la petición que pide información actual no puede ir a
    `agent`; la pestaña no debe decir que «sigue la heurística (agent)» cuando
    lo que corrió fue otra estrategia."""
    with _client(tmp_path) as client:
        client.post("/api/v1/tasks", json={
            "idempotency_key": "boundary-1",
            "content": {"prompt": "Compara las últimas noticias de hoy sobre baterías de estado sólido"},
            "execution": {"strategy": "auto"},
            "risk": {"data_classification": "local_only"},
            "model_requirements": {"allowed_providers": ["ollama"]},
        })
        client.post("/api/v1/dispatcher/tick")
        page = client.get("/dashboard/routing").text

    assert "la heurística diría <em>agent</em>" in page
    assert "frontera de datos" in page


def test_percentile_uses_nearest_rank_without_inventing_values() -> None:
    from app.routing_report import _percentile

    assert _percentile([10.0], 0.95) == 10.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert _percentile([float(v) for v in range(1, 21)], 0.95) == 19.0


def test_failure_breakdown_explains_codes_and_flags_model_faults(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from app.db import Database
    from app.routing_report import failure_breakdown

    db = Database(tmp_path / "f.db")
    db.init_schema()
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO tasks (id, request_json, status, priority, created_at, updated_at) "
            "VALUES ('t', '{}', 'completed', 100, ?, ?)", (now, now),
        )
        for index, (model, code) in enumerate([
            ("a", "INVALID_PROVIDER_RESPONSE"), ("a", "INVALID_PROVIDER_RESPONSE"),
            ("b", "INVALID_PROVIDER_RESPONSE"), ("b", "MODEL_OUT_OF_MEMORY"), ("c", "RARO"),
        ]):
            connection.execute(
                "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, status, "
                "error_code, created_at, updated_at, task_type) "
                "VALUES (?, 't', 'single', 'ollama', 'local', ?, 'failed', ?, ?, ?, 'code')",
                (f"inv{index}", model, code, now, now),
            )
    breakdown = failure_breakdown(db, window_days=7)
    first = breakdown[0]
    assert first["code"] == "INVALID_PROVIDER_RESPONSE"
    assert first["count"] == 3 and first["model_count"] == 2
    assert first["top_models"][0] == ("ollama/a", 2)
    assert first["model_fault"] is True
    memory = next(item for item in breakdown if item["code"] == "MODEL_OUT_OF_MEMORY")
    assert memory["model_fault"] is False and memory["explanation"]
    # Un código desconocido no recibe una explicación inventada.
    assert next(item for item in breakdown if item["code"] == "RARO")["explanation"] is None


def test_report_has_reading_guide_glossary_and_failures_section(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _run_auto_tasks(client, 3)
        body = client.get("/dashboard/routing/report").text

    for heading in ("Cómo leer este informe", "Fallos: qué ha salido mal", "Glosario", "Lo más destacado"):
        assert heading in body
    assert "no que la respuesta fuera correcta" in body


def _csrf(client: TestClient) -> str:
    assert client.get("/dashboard/routing").status_code == 200
    token = client.cookies.get("ai_broker_dashboard_csrf")
    assert token
    return token


def test_interpretation_flow_from_request_to_report(tmp_path: Path) -> None:
    """Pedir la interpretación encola una tarea local, urgente y excluida del
    aprendizaje; cuando el modelo termina, el informe la muestra firmada."""
    with _client(tmp_path) as client:
        _run_auto_tasks(client, 2)
        token = _csrf(client)
        created = client.post("/dashboard/routing/report/interpretation", headers={"X-CSRF-Token": token})
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        request = client.get(f"/api/v1/tasks/{task_id}").json()
        pending = client.get(f"/dashboard/routing/report/interpretation/{task_id}").json()
        assert pending["done"] is False

        client.post("/api/v1/dispatcher/tick")
        status = client.get(f"/dashboard/routing/report/interpretation/{task_id}").json()
        report = client.get(f"/dashboard/routing/report?interpretation={task_id}").text

    assert request["status"] in {"queued", "completed"}
    assert status["done"] is True and status["ok"] is True
    assert "Interpretación de un modelo del broker" in report
    assert "Escrita por" in report


def test_interpretation_task_is_local_urgent_and_excluded(tmp_path: Path) -> None:
    from app.db import Database
    from app.repository import TaskRepository

    with _client(tmp_path) as client:
        token = _csrf(client)
        task_id = client.post(
            "/dashboard/routing/report/interpretation", headers={"X-CSRF-Token": token},
        ).json()["task_id"]

    payload = TaskRepository(Database(tmp_path / "broker.db")).get_task_request(task_id)
    assert payload.priority == 10
    assert payload.exclude_from_model_learning is True
    assert payload.risk.data_classification.value == "local_only"
    assert payload.model_requirements.cloud_allowed is False
    assert "no inventes números" in payload.content.prompt


def test_interpretation_requires_csrf(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.post("/dashboard/routing/report/interpretation").status_code == 403


def test_report_refuses_to_show_an_unrelated_task_as_interpretation(tmp_path: Path) -> None:
    """El id viaja en la URL: no puede servir para leer el resultado de
    cualquier otra tarea del broker."""
    with _client(tmp_path) as client:
        other = client.post("/api/v1/tasks", json={
            "idempotency_key": "secreto", "content": {"prompt": "dato privado"},
        }).json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        report = client.get(f"/dashboard/routing/report?interpretation={other}").text
        status = client.get(f"/dashboard/routing/report/interpretation/{other}")

    assert "Esa tarea no es una interpretación del informe." in report
    assert status.status_code == 404


def test_interpretation_html_escapes_model_output() -> None:
    from app.routing_report import interpretation_html

    html = interpretation_html("## Título\n\nUn **dato** <script>alert(1)</script>\n\n- uno\n- dos")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>dato</strong>" in html
    assert "<ul><li>uno</li><li>dos</li></ul>" in html
    assert "##" not in html


def test_pdf_format_downloads_a_real_pdf(tmp_path: Path) -> None:
    import pytest

    pytest.importorskip("playwright")
    with _client(tmp_path) as client:
        _run_auto_tasks(client, 2)
        response = client.get("/dashboard/routing/report?format=pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert re.search(r'filename="informe-enrutamiento-\d{8}-\d{4}\.pdf"', response.headers["content-disposition"])


def test_pdf_reports_what_is_missing_when_it_cannot_render(tmp_path: Path, monkeypatch) -> None:
    import app.dashboard_web as dashboard_web
    from app.routing_report import PdfUnavailable

    def broken(html: str) -> bytes:
        raise PdfUnavailable("Falta Playwright en el entorno del broker")

    monkeypatch.setattr(dashboard_web, "render_pdf", broken)
    with _client(tmp_path) as client:
        response = client.get("/dashboard/routing/report?format=pdf")

    assert response.status_code == 503
    assert "Falta Playwright" in response.text


def test_routing_tab_offers_html_or_pdf_and_ai_option(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get("/dashboard/routing").text

    assert 'action="/dashboard/routing/report"' in page
    assert '<option value="html">' in page and '<option value="pdf">' in page
    assert "data-report-ai" in page


def test_unsupported_numbers_flags_figures_absent_from_the_data() -> None:
    from app.routing_report import unsupported_numbers

    data = "Resumen: 2182 llamadas, 92 % sin error. nemotron: 48 aciertos de 95 (51 %)."
    text = "De las 2.182 llamadas, el 92 % fue bien; nemotron falla el 49 % y tiene 2 o 3 problemas."
    assert unsupported_numbers(text, data) == ["49"]
