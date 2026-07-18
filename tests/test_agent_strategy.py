"""Estrategia agent: loop de tools con persistencia, guardarraíles y skills.

El bootstrap simula un modelo que pide una skill y luego responde; las skills
se ejercitan con transporte simulado, sin red real."""
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.skills import run_skill


def _client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def test_agent_task_runs_tool_loop_and_persists_events(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:loop",
            "content": {"prompt": "¿Qué tiempo hace hoy?"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 4}},
        })
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    result = detail["result"]
    assert result["agent"]["stop_reason"] == "completed"
    assert result["agent"]["iterations"] == 2  # una ronda pide tool, la segunda concluye
    assert "Respuesta final del agente" in result["assistant_content"]
    tool_events = [e for e in detail["events"] if e["event_type"] == "agent.tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["payload"]["skill"] == "web_search"


def test_agent_strategy_rejects_json_output_in_contract(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:json",
            "content": {"prompt": "x"},
            "output": {"format": "json", "json_schema": {"type": "object"}},
            "execution": {"strategy": "agent"},
        })
    assert response.status_code == 422


def test_agent_max_iterations_guardrail_stops_the_loop(tmp_path: Path, monkeypatch) -> None:
    # Un modelo que SIEMPRE pide tool nunca concluye: el tope debe cortar.
    from app.providers.base import AgentTurn, ToolCall
    from app.providers.bootstrap import BootstrapModelProvider

    async def always_calls_tool(self, request, model, messages, tools, *, allow_parallel=False):
        return AgentTurn(
            content=None,
            tool_calls=(ToolCall(id="c", name="web_search", arguments={"query": "loop"}),),
            tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0,
            raw_assistant_message={"role": "assistant", "content": None, "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "web_search", "arguments": '{"query": "loop"}'}},
            ]},
        )

    async def fake_skill(name, arguments, **kwargs):
        return "resultado simulado"

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", always_calls_tool)
    monkeypatch.setattr("app.coordinator.run_skill", fake_skill)
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:runaway",
            "content": {"prompt": "bucle"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 3}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    assert detail["result"]["agent"]["stop_reason"] == "max_iterations"
    assert detail["result"]["agent"]["iterations"] == 3


def test_fetch_url_rejects_private_hosts() -> None:
    import asyncio

    for url in ("http://localhost:8080/dashboard", "http://127.0.0.1/x", "http://192.168.1.1/"):
        result = asyncio.run(run_skill("fetch_url", {"url": url}))
        assert "ERROR de fetch_url" in result
        assert "no público" in result or "resolver" in result


def test_web_search_parses_duckduckgo_lite_results() -> None:
    import asyncio

    html = """
    <table><tr><td>
      <a href="https://ej.com/a" class="result-link">Primer &amp; resultado</a>
    </td></tr><tr><td class="result-snippet">Un fragmento largo.</td></tr>
    <tr><td>
      <a href="https://ej.com/b" class="result-link">Segundo</a>
    </td></tr><tr><td class="result-snippet">Otro fragmento.</td></tr></table>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "lite.duckduckgo.com"
        return httpx.Response(200, text=html)

    result = asyncio.run(run_skill("web_search", {"query": "algo"}, transport=httpx.MockTransport(handler)))
    assert "Primer & resultado" in result
    assert "https://ej.com/a" in result
    assert "Un fragmento largo" in result


def test_run_skill_unknown_name_raises() -> None:
    import asyncio

    from app.skills import SkillError

    with pytest.raises(SkillError):
        asyncio.run(run_skill("no_existe", {}))


def test_ollama_chat_tools_parses_native_tool_calls() -> None:
    import asyncio

    from app.config import BrokerConfig
    from app.providers import OllamaProvider
    from app.schemas import TaskCreateRequest

    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": "qwen3:8b", "size": 1, "context_length": 8192,
                 "capabilities": ["completion", "tools"]},
            ]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={})
        if request.url.path == "/api/chat":
            state["calls"] += 1
            # Ollama devuelve arguments como objeto (no string JSON).
            return httpx.Response(200, json={
                "message": {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": "web_search",
                                                          "arguments": {"query": "clima"}}}]},
                "prompt_eval_count": 10, "eval_count": 3,
            })
        return httpx.Response(404)

    provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
    request = TaskCreateRequest(idempotency_key="ol:tools", content={"prompt": "clima hoy"})
    tools = [{"type": "function", "function": {"name": "web_search",
              "parameters": {"type": "object", "properties": {}}}}]
    messages = [{"role": "user", "content": "clima hoy"}]
    turn = asyncio.run(provider.chat_tools(request, "qwen3:8b", messages, tools))
    asyncio.run(provider.close())

    assert state["calls"] == 1
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "web_search"
    assert turn.tool_calls[0].arguments == {"query": "clima"}
    assert turn.tokens_input == 10


def test_agent_precheck_blocks_model_without_tools_before_enqueue(tmp_path: Path, monkeypatch) -> None:
    import json as _json

    from app.providers.bootstrap import BootstrapModelProvider

    async def catalog_without_tools(self):
        return [{
            "name": "sin-tools", "provider": "ollama", "deployment": "bootstrap",
            "status": "available", "context_window": 8192, "capabilities": ["completion"],
            "compatibility": "compatible", "compatibility_checked_at": None,
            "compatibility_error": None, "features": {"tools": False},
        }]

    monkeypatch.setattr(BootstrapModelProvider, "models", catalog_without_tools)
    with _client(tmp_path) as client:
        token = client.get("/dashboard/prompt-tester")
        csrf = client.cookies.get("ai_broker_dashboard_csrf")
        model_value = _json.dumps({"provider": "ollama", "deployment": "bootstrap", "model": "sin-tools"})
        response = client.post("/dashboard/actions/prompt-tester", data={
            "action": "enqueue", "csrf_token": csrf, "input_mode": "prompt",
            "prompt": "haz algo", "strategy": "agent", "agent_model": model_value,
            "agent_max_iterations": "4", "agent_skill_web_search": "on",
        })
        queue = client.get("/api/v1/queue").json()

    assert token.status_code == 200
    assert response.status_code == 200
    assert "no soporta tools" in response.text
    assert queue["pending"] == []


def test_calculator_skill_evaluates_safely() -> None:
    import asyncio

    assert asyncio.run(run_skill("calculator", {"expression": "(1234 * 5.5) / 3"})) == "2262.333333333333"
    assert asyncio.run(run_skill("calculator", {"expression": "2 ** 10"})) == "1024"
    assert asyncio.run(run_skill("calculator", {"expression": "17 % 5"})) == "2"
    # División por cero: error legible, no excepción.
    assert "división por cero" in asyncio.run(run_skill("calculator", {"expression": "1/0"}))
    # Nada de nombres, atributos ni llamadas: bloqueado.
    assert "no permitida" in asyncio.run(run_skill("calculator", {"expression": "__import__('os')"}))
    assert "no permitida" in asyncio.run(run_skill("calculator", {"expression": "x + 1"}))
    # Exponente desbocado acotado.
    assert "demasiado grande" in asyncio.run(run_skill("calculator", {"expression": "2 ** 99999"}))


def test_current_datetime_skill_returns_iso() -> None:
    import asyncio
    import json as _json

    result = _json.loads(asyncio.run(run_skill("current_datetime", {})))
    assert "utc" in result and "local" in result and "weekday" in result
    assert result["utc"].endswith("+00:00")


def test_mixture_proposers_use_skills_and_record_tool_events(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "mix:skills",
            "content": {"prompt": "Compara dos opciones con datos actuales"},
            "execution": {
                "strategy": "mixture_of_agents", "preset": "fast",
                "proposer_skills": ["web_search"],
                "selection": {
                    "mode": "manual", "allow_substitution": False, "proposer_count": 2,
                    "proposers": [
                        {"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single", "role": "generalist"},
                        {"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single", "role": "skeptic"},
                    ],
                    "arbiter": {"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single"},
                },
            },
            "model_requirements": {"allowed_providers": ["ollama"]},
        })
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    # Cada proponente usó una skill: dos eventos con su rol.
    tool_events = [e for e in detail["events"] if e["event_type"] == "agent.tool_call"]
    roles = sorted(e["payload"]["role"] for e in tool_events)
    assert roles == ["generalist", "skeptic"]
    # La síntesis del árbitro sigue produciendo el resultado final.
    assert detail["result"]["assistant_content"]


def test_proposer_skills_rejected_outside_mixture(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={
            "idempotency_key": "single:proposer-skills",
            "content": {"prompt": "x"},
            "execution": {"strategy": "single", "proposer_skills": ["web_search"]},
        })
    assert response.status_code == 422


def _json_dumps(value):
    import json as _json
    return _json.dumps(value)


def test_client_tool_passthrough_pause_and_resume(tmp_path: Path) -> None:
    client_tools = [{"name": "consulta_crm", "description": "Consulta el CRM del cliente",
                     "parameters": {"type": "object", "properties": {"input": {"type": "string"}}}}]
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:passthrough",
            "content": {"prompt": "Busca el pedido en el CRM"},
            "execution": {"strategy": "agent", "agent": {
                "skills": [], "max_iterations": 6, "client_tools": client_tools,
            }},
        })
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]

        # Primer tramo: el agente pide la tool del cliente y la tarea se pausa.
        client.post("/api/v1/dispatcher/tick")
        paused = client.get(f"/api/v1/tasks/{task_id}").json()
        assert paused["status"] == "waiting_for_tools"
        pending = paused["result"]["pending_tool_calls"]
        assert len(pending) == 1
        assert pending[0]["name"] == "consulta_crm"
        call_id = pending[0]["id"]

        # La tarea en espera no bloquea la cola ni se despacha sola.
        assert client.post("/api/v1/dispatcher/tick").json()["task_id"] is None

        # El cliente entrega el resultado y la tarea vuelve a la cola.
        resumed = client.post(f"/api/v1/tasks/{task_id}/tool_results", json={
            "tool_results": [{"tool_call_id": call_id, "content": "Pedido #42, entregado"}],
        })
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "queued"

        # Segundo tramo: el agente ya tiene el dato y concluye.
        client.post("/api/v1/dispatcher/tick")
        done = client.get(f"/api/v1/tasks/{task_id}").json()
        assert done["status"] == "completed"
        assert "Respuesta final del agente" in done["result"]["assistant_content"]
        # La contabilidad suma los dos tramos.
        assert done["result"]["usage"]["invocations"] >= 2


def test_tool_results_rejected_when_ids_mismatch(tmp_path: Path) -> None:
    client_tools = [{"name": "consulta_crm", "description": "CRM",
                     "parameters": {"type": "object", "properties": {}}}]
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:badresume",
            "content": {"prompt": "x"},
            "execution": {"strategy": "agent", "agent": {"skills": [], "client_tools": client_tools}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        bad = client.post(f"/api/v1/tasks/{task_id}/tool_results", json={
            "tool_results": [{"tool_call_id": "id-que-no-existe", "content": "x"}],
        })
    assert bad.status_code == 409
    assert "no coinciden" in bad.json()["detail"]


def test_tool_results_rejected_when_task_not_waiting(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:notwaiting",
            "content": {"prompt": "hola"},
        })
        task_id = created.json()["task_id"]
        response = client.post(f"/api/v1/tasks/{task_id}/tool_results", json={
            "tool_results": [{"tool_call_id": "x", "content": "y"}],
        })
    assert response.status_code == 409
    assert "no está esperando" in response.json()["detail"]
