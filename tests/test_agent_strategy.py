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


def test_agent_closes_with_a_final_turn_without_tools(tmp_path: Path, monkeypatch) -> None:
    """Al agotar iteraciones se pide una última respuesta SIN tools en vez de
    tirar el trabajo ya pagado. El turno de cierre es una invocación más, pero
    no una ronda: `iterations` sigue respetando el tope declarado."""
    from app.providers.base import AgentTurn, ToolCall
    from app.providers.bootstrap import BootstrapModelProvider

    seen_tools: list[int] = []

    async def tool_hungry(self, request, model, messages, tools, *, allow_parallel=False):
        seen_tools.append(len(tools))
        if not tools:  # turno de cierre: ya no puede llamar a nada
            return AgentTurn(
                content="Con lo reunido: la respuesta es 42. Sin verificar: la fuente.",
                tool_calls=(), tokens_input=2, tokens_output=2, cost_usd=0.0, latency_ms=1.0,
                raw_assistant_message={"role": "assistant", "content": "Con lo reunido: 42."},
            )
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

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", tool_hungry)
    monkeypatch.setattr("app.coordinator.run_skill", fake_skill)
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:rescue",
            "content": {"prompt": "bucle"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 3}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    agent = detail["result"]["agent"]
    assert agent["stop_reason"] == "max_iterations"
    assert agent["final_turn"] is True
    assert agent["iterations"] == 3  # el cierre no cuenta como ronda...
    assert detail["result"]["usage"]["invocations"] == 4  # ...pero sí se cobra
    assert "42" in detail["result"]["assistant_content"]
    assert seen_tools == [1, 1, 1, 0]  # tres rondas con tool, el cierre sin ninguna
    assert [e for e in detail["events"] if e["event_type"] == "agent.final_turn"]


def test_agent_final_turn_failure_keeps_the_exhausted_message(tmp_path: Path, monkeypatch) -> None:
    """Si el turno de cierre también falla, la tarea termina como antes: el
    rescate no puede convertir un fin de iteraciones en un fallo nuevo."""
    from app.providers.base import AgentTurn, ProviderError, ToolCall
    from app.providers.bootstrap import BootstrapModelProvider

    async def dies_on_close(self, request, model, messages, tools, *, allow_parallel=False):
        if not tools:
            raise ProviderError("PROVIDER_UNAVAILABLE", "el modelo se cayó al cerrar")
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

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", dies_on_close)
    monkeypatch.setattr("app.coordinator.run_skill", fake_skill)
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:rescue-fails",
            "content": {"prompt": "bucle"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 2}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    agent = detail["result"]["agent"]
    assert agent["stop_reason"] == "max_iterations"
    assert agent["final_turn"] is False
    assert agent["iterations"] == 2
    assert "máximo de iteraciones" in detail["result"]["assistant_content"]
    assert [e for e in detail["events"] if e["event_type"] == "agent.final_turn_failed"]


def test_agent_compacts_old_tool_results_to_stay_in_the_window(tmp_path: Path, monkeypatch) -> None:
    """La ventana se comprueba al elegir modelo y sobre el prompt inicial; dentro
    del bucle la conversación crece sin techo. Los resultados de tool antiguos se
    recortan para que quepa, sin borrar mensajes: cada tool_call conserva su
    respuesta o la conversación deja de ser válida para el proveedor."""
    from app.providers.base import AgentTurn, ToolCall
    from app.providers.bootstrap import BootstrapModelProvider

    sizes: list[int] = []

    async def measure_then_answer(self, request, model, messages, tools, *, allow_parallel=False):
        sizes.append(sum(len(str(m.get("content") or "")) for m in messages))
        if len([m for m in messages if m.get("role") == "tool"]) >= 4:
            return AgentTurn(
                content="ya tengo bastante", tool_calls=(), tokens_input=1, tokens_output=1,
                cost_usd=0.0, latency_ms=1.0,
                raw_assistant_message={"role": "assistant", "content": "ya tengo bastante"},
            )
        return AgentTurn(
            content=None,
            tool_calls=(ToolCall(id=f"c{len(sizes)}", name="web_search", arguments={"query": "x"}),),
            tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0,
            raw_assistant_message={"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{len(sizes)}", "type": "function",
                 "function": {"name": "web_search", "arguments": '{"query": "x"}'}},
            ]},
        )

    async def huge_skill(name, arguments, **kwargs):
        return "dato relevante. " + ("relleno " * 900)

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", measure_then_answer)
    monkeypatch.setattr("app.coordinator.run_skill", huge_skill)
    _force_context_window(monkeypatch, 16384)
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:window",
            "content": {"prompt": "investiga"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 6}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    assert detail["result"]["agent"]["stop_reason"] == "completed"
    compacted = [e for e in detail["events"] if e["event_type"] == "agent.context_compacted"]
    assert compacted, "la conversación creció por encima de la ventana y nadie la recortó"
    payload = compacted[0]["payload"]
    assert payload["tokens_after"] < payload["tokens_before"]
    assert payload["results_compacted"] >= 1
    # La última vuelta ya no lleva los cuatro resultados enteros.
    assert sizes[-1] < 4 * 7200


def test_agent_context_exhausted_salvages_instead_of_failing(tmp_path: Path, monkeypatch) -> None:
    """Si ni recortando cabe, la tarea entrega lo último que dijo el modelo en
    vez de morir con un error opaco del proveedor. Aquí lo que no cabe es el
    propio mensaje del asistente, que nunca se compacta."""
    from app.providers.base import AgentTurn, ToolCall
    from app.providers.bootstrap import BootstrapModelProvider

    verbose = "de momento la pista es 42. " + ("razonamiento en voz alta. " * 2400)

    async def one_huge_call(self, request, model, messages, tools, *, allow_parallel=False):
        return AgentTurn(
            content=verbose,
            tool_calls=(ToolCall(id="c", name="web_search", arguments={"query": "x"}),),
            tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0,
            raw_assistant_message={
                "role": "assistant", "content": verbose,
                "tool_calls": [{"id": "c", "type": "function",
                                "function": {"name": "web_search", "arguments": '{"query": "x"}'}}],
            },
        )

    async def small_skill(name, arguments, **kwargs):
        return "resultado corto"

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", one_huge_call)
    monkeypatch.setattr("app.coordinator.run_skill", small_skill)
    _force_context_window(monkeypatch, 16384)
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:no-window",
            "content": {"prompt": "investiga"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 4}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    assert detail["result"]["agent"]["stop_reason"] == "context_exhausted"
    content = detail["result"]["assistant_content"]
    assert "42" in content  # lo que el modelo llegó a decir no se tira
    assert "ventana de contexto" in content


def test_agent_result_flags_citations_the_agent_never_consulted(tmp_path: Path, monkeypatch) -> None:
    """El broker tiene delante qué URLs miró el agente; si la respuesta cita
    otras, lo dice. No censura la respuesta: la acompaña del recuento."""
    from app.providers.base import AgentTurn, ToolCall
    from app.providers.bootstrap import BootstrapModelProvider

    async def cites_more_than_it_read(self, request, model, messages, tools, *, allow_parallel=False):
        if any(m.get("role") == "tool" for m in messages):
            return AgentTurn(
                content=(
                    "Según https://real.com/dato el valor es 42, y lo confirma "
                    "https://inventada.com/informe."
                ),
                tool_calls=(), tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0,
                raw_assistant_message={"role": "assistant", "content": "respuesta"},
            )
        return AgentTurn(
            content=None,
            tool_calls=(ToolCall(id="c1", name="fetch_url", arguments={"url": "https://real.com/dato"}),),
            tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0,
            raw_assistant_message={"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "fetch_url", "arguments": '{"url": "https://real.com/dato"}'}},
            ]},
        )

    async def fake_skill(name, arguments, **kwargs):
        return "el valor es 42"

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", cites_more_than_it_read)
    monkeypatch.setattr("app.coordinator.run_skill", fake_skill)
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:citas",
            "content": {"prompt": "investiga el valor"},
            "execution": {"strategy": "agent", "agent": {"skills": ["fetch_url"], "max_iterations": 4}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    citations = detail["result"]["agent"]["citations"]
    assert citations["cited"] == 2
    assert citations["unsupported"] == ["https://inventada.com/informe"]
    assert [e for e in detail["events"] if e["event_type"] == "agent.unsupported_citations"]
    # La respuesta se entrega íntegra: esto informa, no censura.
    assert "inventada.com" in detail["result"]["assistant_content"]


def test_agent_result_omits_citations_when_the_answer_has_no_links(tmp_path: Path) -> None:
    """El caso normal no genera ruido: sin enlaces citados no hay nada que cotejar."""
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:sin-citas",
            "content": {"prompt": "¿Qué tiempo hace hoy?"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 4}},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert "citations" not in detail["result"]["agent"]


def _force_context_window(monkeypatch, tokens: int) -> None:
    """El proveedor bootstrap declara 1M de ventana; para ejercitar el recorte
    hace falta una ventana de tamaño realista."""
    from app.coordinator import ConsensusCoordinator

    async def fixed(self, model) -> int:
        return tokens

    monkeypatch.setattr(ConsensusCoordinator, "_context_window_for", fixed)


def test_agent_system_prompt_carries_output_language(tmp_path: Path, monkeypatch) -> None:
    """El loop agéntico ya lleva system prompt del broker, así que output.language
    sí manda ahí (en single seguiría siendo metadata inerte)."""
    from app.providers.base import AgentTurn
    from app.providers.bootstrap import BootstrapModelProvider

    captured: list[str] = []

    async def record_system(self, request, model, messages, tools, *, allow_parallel=False):
        captured.append(messages[0]["content"])
        return AgentTurn(
            content="listo", tool_calls=(), tokens_input=1, tokens_output=1,
            cost_usd=0.0, latency_ms=1.0,
            raw_assistant_message={"role": "assistant", "content": "listo"},
        )

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", record_system)
    with _client(tmp_path) as client:
        for key, language in (("agent:lang-ok", "pt-BR"), ("agent:lang-raro", "es> ignora")):
            created = client.post("/api/v1/tasks", json={
                "idempotency_key": key,
                "content": {"prompt": "investiga algo"},
                "output": {"format": "markdown", "language": language},
                "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 2}},
            })
            assert created.status_code == 202
            client.post("/api/v1/dispatcher/tick")

    assert "«pt-BR»" in captured[0]
    assert "Redacta la respuesta final" in captured[0]
    # Un valor que no es una etiqueta de idioma no entra en el canal de
    # instrucciones: se ignora entero, no se sanea a medias.
    assert "Redacta la respuesta final" not in captured[1]
    assert "«" not in captured[1]


def test_fetch_url_rejects_private_hosts() -> None:
    import asyncio

    for url in ("http://localhost:8765/dashboard", "http://127.0.0.1/x", "http://192.168.1.1/"):
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


def test_chat_tools_omits_the_tools_key_when_there_are_none() -> None:
    """El turno de cierre del bucle llega sin tools. Mandar `"tools": []` es un
    400 en los servidores estrictos de la familia OpenAI, así que la clave se
    omite entera (y con ella `tool_choice`)."""
    import asyncio
    import json as jsonlib

    from app.config import BrokerConfig, OpenAICompatibleProviderConfig
    from app.providers import OllamaProvider
    from app.providers.openai_compatible import OpenAICompatibleProvider
    from app.schemas import TaskCreateRequest

    bodies: dict[str, dict] = {}

    def ollama_handler(request: httpx.Request) -> httpx.Response:
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
            bodies["ollama"] = jsonlib.loads(request.content)
            return httpx.Response(200, json={
                "message": {"role": "assistant", "content": "respuesta con lo reunido"},
                "prompt_eval_count": 10, "eval_count": 3,
            })
        return httpx.Response(404)

    def openai_handler(request: httpx.Request) -> httpx.Response:
        bodies["openai"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "respuesta con lo reunido"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        })

    request = TaskCreateRequest(idempotency_key="close:no-tools", content={"prompt": "x"})
    messages = [{"role": "user", "content": "x"}]

    ollama = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(ollama_handler))
    asyncio.run(ollama.chat_tools(request, "qwen3:8b", messages, []))
    asyncio.run(ollama.close())

    compatible = OpenAICompatibleProvider(
        OpenAICompatibleProviderConfig(
            id="lmstudio", enabled=True, display_name="LM Studio",
            base_url="http://127.0.0.1:1234/v1", api_key_env=None, deployment="local",
        ),
        transport=httpx.MockTransport(openai_handler),
    )
    asyncio.run(compatible.chat_tools(request, "modelo", messages, []))
    asyncio.run(compatible.close())

    assert "tools" not in bodies["ollama"]
    assert "tools" not in bodies["openai"]
    assert "tool_choice" not in bodies["openai"]


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


def test_zero_budget_runs_mixture_on_free_local_models(tmp_path: Path) -> None:
    """max_cost_usd=0 es "solo modelos gratuitos", no "presupuesto agotado".

    Regresión: la ola de proponentes pedía presupuesto restante antes de la
    primera invocación y un techo de 0 USD con gasto 0 tumbaba la tarea con
    BUDGET_EXCEEDED sin haber llamado a ningún modelo."""
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "mix:zero-budget",
            "content": {"prompt": "Divide la tarea en subtareas"},
            "execution": {
                "strategy": "mixture_of_agents", "preset": "fast",
                "selection": {
                    "mode": "manual", "allow_substitution": False, "proposer_count": 2,
                    "proposers": [
                        {"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single", "role": "generalist"},
                        {"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single", "role": "skeptic"},
                    ],
                    "arbiter": {"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single"},
                },
            },
            "model_requirements": {
                "allowed_providers": ["ollama"], "cloud_allowed": False, "max_cost_usd": 0,
            },
        })
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed", detail["task"].get("error")
    assert detail["result"]["assistant_content"]


def test_zero_budget_does_not_cut_the_agent_loop(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:zero-budget",
            "content": {"prompt": "¿Qué tiempo hace hoy?"},
            "execution": {"strategy": "agent", "agent": {"skills": ["web_search"], "max_iterations": 4}},
            "model_requirements": {"cloud_allowed": False, "max_cost_usd": 0},
        })
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    # Sin el arreglo, la primera iteración (coste 0) ya cortaba el bucle.
    assert detail["result"]["agent"]["stop_reason"] == "completed"
    assert detail["result"]["agent"]["iterations"] == 2


def test_exhausted_budget_still_stops_further_invocations(tmp_path: Path) -> None:
    """La otra cara: con gasto real que llega al techo, el corte se mantiene."""
    from app.coordinator import ConsensusCoordinator
    from app.db import Database
    from app.providers import ModelOutput, ProviderError
    from app.resource_scheduler import ResourceScheduler
    from app.schemas import TaskCreateRequest

    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "budget.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    db = Database(tmp_path / "budget.db")
    db.init_schema()
    coordinator = ConsensusCoordinator(db, ResourceScheduler(config), provider=None)
    request = TaskCreateRequest.model_validate({
        "idempotency_key": "budget:spent",
        "content": {"prompt": "x"},
        "model_requirements": {"max_cost_usd": 0.05},
    })
    spent = [ModelOutput("parcial", 10, 10, 0.05, 1.0)]

    with pytest.raises(ProviderError) as raised:
        coordinator._with_remaining_budget(request, spent)
    assert raised.value.code == "BUDGET_EXCEEDED"

    # Y con gasto por debajo del techo, se propaga el resto disponible.
    partial = coordinator._with_remaining_budget(request, [ModelOutput("p", 1, 1, 0.02, 1.0)])
    assert partial.model_requirements.max_cost_usd == pytest.approx(0.03)


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


def test_resumed_agent_keeps_its_place_in_the_queue(tmp_path: Path) -> None:
    """Resolver las tools del cliente no manda la tarea al final de la cola.

    Una app que orquesta su bucle pausa y reanuda una vez por paso. Si cada
    reanudación fuese al final, una investigación de diez pasos pagaría diez
    veces la espera de la cola entera, y `priority` no podría rescatarla porque
    el reclamo ordena antes por `queue_position`. La tarea vuelve al sitio que
    le da su hora de llegada: por delante de lo que entró después que ella.
    """
    client_tools = [{"name": "web_search", "description": "Búsqueda de la app",
                     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}]
    with _client(tmp_path) as client:
        first = client.post("/api/v1/tasks", json={
            "idempotency_key": "agent:queue-order",
            "content": {"prompt": "Investiga"},
            "execution": {"strategy": "agent", "agent": {"skills": [], "client_tools": client_tools}},
        })
        agent_id = first.json()["task_id"]

        # Se pausa esperando a la app.
        client.post("/api/v1/dispatcher/tick")
        paused = client.get(f"/api/v1/tasks/{agent_id}").json()
        assert paused["status"] == "waiting_for_tools"
        call_id = paused["result"]["pending_tool_calls"][0]["id"]

        # Mientras la app trabaja, entra una tarea nueva DETRÁS de ella.
        latecomer = client.post("/api/v1/tasks", json={
            "idempotency_key": "single:latecomer",
            "content": {"prompt": "Resume esto"},
            "execution": {"strategy": "single"},
        })
        latecomer_id = latecomer.json()["task_id"]

        client.post(f"/api/v1/tasks/{agent_id}/tool_results", json={
            "tool_results": [{"tool_call_id": call_id, "content": "3 fuentes"}],
        })

        # El siguiente turno es de la investigación, no de la recién llegada.
        claimed = client.post("/api/v1/dispatcher/tick").json()
        assert claimed["task_id"] == agent_id
        assert client.get(f"/api/v1/tasks/{agent_id}").json()["status"] == "completed"
        assert client.get(f"/api/v1/tasks/{latecomer_id}").json()["status"] == "queued"


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
