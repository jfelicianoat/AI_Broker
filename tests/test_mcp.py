"""Cliente MCP sobre stdio, contra un servidor de verdad (un subproceso).

No se simula el transporte: el servidor de `fixtures/mcp_echo_server.py` se
lanza como proceso hijo y se habla con él por stdin/stdout, que es donde están
los fallos que importan (framing, ruido en stdout, procesos que no arrancan).
"""
import asyncio
import sys
from pathlib import Path

import pytest

from app.config import MCPConfig, MCPServerConfig
from app.mcp import MCPError, MCPRegistry, qualified_name, split_qualified_name

_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def _config(**overrides) -> MCPConfig:
    server = MCPServerConfig(
        id="echo",
        command=sys.executable,
        args=[str(_SERVER)],
        data_boundary="local",
        **overrides,
    )
    return MCPConfig(enabled=True, servers=[server])


def _run(coroutine):
    return asyncio.run(coroutine)


def test_qualified_names_round_trip() -> None:
    name = qualified_name("mi-servidor", "buscar")
    assert name == "mcp__mi-servidor__buscar"
    assert split_qualified_name(name) == ("mi-servidor", "buscar")
    # Un nombre de skill del broker no se confunde con uno de MCP.
    assert split_qualified_name("web_search") is None
    assert split_qualified_name("mcp__incompleto") is None


def test_discovers_tools_and_calls_one() -> None:
    async def scenario() -> None:
        registry = MCPRegistry(_config())
        try:
            tools = await registry.tools_for(["echo"])
            names = {tool.name for tool in tools}
            assert names == {"echo", "explota"}

            # Lo que ve el modelo va con prefijo, para que un servidor no pueda
            # publicar `web_search` y suplantar a la skill del broker.
            definition = next(t for t in tools if t.name == "echo").definition()
            assert definition["function"]["name"] == "mcp__echo__echo"
            assert definition["function"]["parameters"]["required"] == ["text"]

            result = await registry.call("echo", "echo", {"text": "hola"})
            assert result == "eco: hola"
        finally:
            await registry.aclose()

    _run(scenario())


def test_a_tool_error_is_text_for_the_model_not_an_exception() -> None:
    """Un error DE la herramienta es un resultado que el modelo debe leer y
    reintentar; solo un fallo de protocolo o de proceso es excepción."""
    async def scenario() -> None:
        registry = MCPRegistry(_config())
        try:
            result = await registry.call("echo", "explota", {})
            assert result.startswith("ERROR:")
            assert "la herramienta falló" in result
        finally:
            await registry.aclose()

    _run(scenario())


def test_an_unknown_tool_raises() -> None:
    async def scenario() -> None:
        registry = MCPRegistry(_config())
        try:
            with pytest.raises(MCPError) as error:
                await registry.call("echo", "no_existe", {})
            assert "desconocida" in str(error.value)
        finally:
            await registry.aclose()

    _run(scenario())


def test_a_server_that_cannot_start_does_not_break_the_task() -> None:
    """Una integración opcional rota no puede impedir que la tarea corra: el
    servidor aporta cero herramientas y el resto sigue."""
    async def scenario() -> None:
        broken = MCPServerConfig(
            id="roto", command="no-existe-este-binario-jamas", data_boundary="local",
        )
        config = MCPConfig(enabled=True, servers=[broken, _config().servers[0]])
        registry = MCPRegistry(config)
        try:
            tools = await registry.tools_for(["roto", "echo"])
            assert {tool.server_id for tool in tools} == {"echo"}
        finally:
            await registry.aclose()

    _run(scenario())


def test_the_registry_reports_which_servers_send_data_out() -> None:
    config = MCPConfig(enabled=True, servers=[
        MCPServerConfig(id="local-fs", command="x", data_boundary="local"),
        MCPServerConfig(id="buscador", command="x", data_boundary="egress"),
        MCPServerConfig(id="apagado", command="x", data_boundary="egress", enabled=False),
    ])
    registry = MCPRegistry(config)
    assert registry.egress_server_ids() == ["buscador"]
    assert registry.data_boundary("local-fs") == "local"
    assert registry.data_boundary("apagado") is None


def test_server_ids_must_be_unique() -> None:
    with pytest.raises(ValueError):
        MCPConfig(servers=[
            MCPServerConfig(id="dup", command="x", data_boundary="local"),
            MCPServerConfig(id="dup", command="y", data_boundary="egress"),
        ])


def test_the_data_boundary_has_no_default() -> None:
    """Deducirla del transporte mentiría con un servidor stdio que por dentro
    llama a internet: se declara o el servidor no existe."""
    with pytest.raises(ValueError):
        MCPServerConfig(id="sin-frontera", command="x")


# --------------------------------------------------------------- integración

def _app_client(tmp_path: Path, **mcp_overrides):
    from fastapi.testclient import TestClient

    from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
    from app.main import create_app

    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        mcp=MCPConfig(**{"enabled": True, "servers": _config().servers, **mcp_overrides}),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def test_the_agent_calls_an_mcp_tool_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """El bucle agéntico ofrece la herramienta al modelo con su nombre
    cualificado, la ejecuta contra el servidor y devuelve su texto."""
    from app.providers.base import AgentTurn, ToolCall
    from app.providers.bootstrap import BootstrapModelProvider

    offered: list[list[str]] = []

    async def calls_the_mcp_tool(self, request, model, messages, tools, *, allow_parallel=False):
        offered.append([tool["function"]["name"] for tool in tools])
        if any(message.get("role") == "tool" for message in messages):
            last = next(m for m in reversed(messages) if m.get("role") == "tool")
            return AgentTurn(
                content=f"la herramienta dijo: {last['content']}",
                tool_calls=(), tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0,
                raw_assistant_message={"role": "assistant", "content": "listo"},
            )
        return AgentTurn(
            content=None,
            tool_calls=(ToolCall(id="c1", name="mcp__echo__echo", arguments={"text": "hola"}),),
            tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0,
            raw_assistant_message={"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "mcp__echo__echo", "arguments": '{"text": "hola"}'}},
            ]},
        )

    monkeypatch.setattr(BootstrapModelProvider, "agent_turn", calls_the_mcp_tool)
    with _app_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "mcp:e2e",
            "content": {"prompt": "usa la herramienta"},
            "execution": {"strategy": "agent", "agent": {
                "skills": ["calculator"], "mcp_servers": ["echo"], "max_iterations": 4,
            }},
        })
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    assert "eco: hola" in detail["result"]["assistant_content"]
    # Al modelo se le ofrecieron la skill del broker y la herramienta MCP.
    assert "calculator" in offered[0]
    assert "mcp__echo__echo" in offered[0]
    tool_events = [e for e in detail["events"] if e["event_type"] == "agent.tool_call"]
    assert tool_events[0]["payload"]["skill"] == "mcp__echo__echo"


def test_an_unknown_or_disabled_server_is_rejected_when_creating(tmp_path: Path) -> None:
    with _app_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={
            "idempotency_key": "mcp:desconocido",
            "content": {"prompt": "x"},
            "execution": {"strategy": "agent", "agent": {"mcp_servers": ["no-existe"]}},
        })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MCP_SERVER_UNKNOWN"


def test_mcp_disabled_is_rejected_when_creating(tmp_path: Path) -> None:
    with _app_client(tmp_path, enabled=False) as client:
        response = client.post("/api/v1/tasks", json={
            "idempotency_key": "mcp:apagado",
            "content": {"prompt": "x"},
            "execution": {"strategy": "agent", "agent": {"mcp_servers": ["echo"]}},
        })
    assert response.status_code == 409
    assert response.json()["detail"] == "MCP_DISABLED"


def test_the_data_boundary_blocks_an_egress_server(tmp_path: Path) -> None:
    """La misma regla que ya rige para web_search: bajo frontera local no se
    admite una herramienta que saque contenido de la máquina, y en MCP quién lo
    hace lo declara el operador porque el esquema no puede saberlo."""
    servers = [
        MCPServerConfig(id="echo", command=sys.executable, args=[str(_SERVER)], data_boundary="local"),
        MCPServerConfig(id="buscador", command=sys.executable, args=[str(_SERVER)], data_boundary="egress"),
    ]
    with _app_client(tmp_path, servers=servers) as client:
        rejected = client.post("/api/v1/tasks", json={
            "idempotency_key": "mcp:frontera",
            "content": {"prompt": "privado"},
            "risk": {"data_classification": "local_only"},
            "execution": {"strategy": "agent", "agent": {
                "skills": ["calculator"], "mcp_servers": ["buscador"],
            }},
        })
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "MCP_SERVER_NOT_LOCAL"

        # El servidor local sí pasa con la misma clasificación.
        accepted = client.post("/api/v1/tasks", json={
            "idempotency_key": "mcp:frontera-ok",
            "content": {"prompt": "privado"},
            "risk": {"data_classification": "local_only"},
            "execution": {"strategy": "agent", "agent": {
                "skills": ["calculator"], "mcp_servers": ["echo"],
            }},
        })
        assert accepted.status_code == 202


def test_mcp_servers_alone_satisfy_the_agent_tool_requirement() -> None:
    """Una tarea agéntica sin skills del broker pero con MCP es válida: las
    herramientas de terceros son motivo suficiente para tener bucle."""
    from app.schemas import TaskCreateRequest

    request = TaskCreateRequest.model_validate({
        "idempotency_key": "mcp:solo",
        "content": {"prompt": "x"},
        "execution": {"strategy": "agent", "agent": {"skills": [], "mcp_servers": ["echo"]}},
    })
    assert request.execution.agent.mcp_servers == ["echo"]
