import json
from pathlib import Path

import jsonschema
from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.schemas import (
    ExecutionStrategy,
    SelectionMode,
    TaskCreateRequest,
)

TASK_STATE_SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "broker_task_state_response.schema.json")
    .read_text(encoding="utf-8")
)


def test_task_contract_defaults_to_single() -> None:
    payload = TaskCreateRequest.model_validate({"idempotency_key": "contract:single", "content": {"prompt": "Resume este texto"}})

    assert payload.execution.strategy == ExecutionStrategy.single
    assert payload.execution.selection.mode == SelectionMode.auto
    # Sin declaración del cliente no hay restricción por proveedor: el default
    # ["ollama"] anterior era un cerrojo silencioso que dejaba fuera al cloud
    # aunque cloud_allowed fuese true.
    assert payload.model_requirements.allowed_providers is None


def test_classification_derives_the_cloud_permission_when_client_is_silent() -> None:
    """data_classification es el mando único de privacidad: de él se deriva
    cloud_allowed cuando la app cliente no se pronuncia."""
    for classification, expected in (
        ("public", True),
        ("internal", True),
        ("confidential", False),
        ("local_only", False),
    ):
        payload = TaskCreateRequest.model_validate({
            "idempotency_key": f"contract:derive:{classification}",
            "content": {"prompt": "Resume este texto"},
            "risk": {"data_classification": classification},
        })
        assert payload.model_requirements.cloud_allowed is expected, classification


def test_explicit_cloud_allowed_false_survives_a_permissive_classification() -> None:
    payload = TaskCreateRequest.model_validate({
        "idempotency_key": "contract:explicit-local",
        "content": {"prompt": "Resume este texto"},
        "model_requirements": {"cloud_allowed": False},
        "risk": {"data_classification": "public"},
    })

    assert payload.model_requirements.cloud_allowed is False


def test_classification_wins_over_an_explicit_cloud_request() -> None:
    """La frontera no se cede: pedir cloud en una tarea confidencial es
    contradictorio y se resuelve a favor de la privacidad."""
    payload = TaskCreateRequest.model_validate({
        "idempotency_key": "contract:contradiction",
        "content": {"prompt": "Resume este texto"},
        "model_requirements": {"cloud_allowed": True},
        "risk": {"data_classification": "confidential"},
    })

    assert payload.model_requirements.cloud_allowed is False


def test_confidential_target_model_must_be_local() -> None:
    try:
        TaskCreateRequest.model_validate({
            "idempotency_key": "contract:confidential-target",
            "content": {"prompt": "Privado"},
            "model_requirements": {
                "cloud_allowed": True,
                "allowed_providers": ["deepseek"],
                "target_model": {"provider": "deepseek", "deployment": "api", "model": "chat"},
            },
            "risk": {"data_classification": "confidential"},
        })
    except Exception as exc:
        assert "confidential target_model" in str(exc)
    else:
        raise AssertionError("confidential cloud target should fail")


def test_legacy_execution_mode_alias_is_accepted() -> None:
    payload = TaskCreateRequest.model_validate(
        {
            "idempotency_key": "contract:legacy",
            "content": {"prompt": "Analiza"},
            "execution": {"mode": "single"},
        }
    )

    assert payload.execution.strategy == ExecutionStrategy.single


def test_manual_selection_requires_proposers_and_arbiter() -> None:
    try:
        TaskCreateRequest.model_validate(
            {
                "idempotency_key": "contract:manual",
                "content": {"prompt": "Analiza"},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "selection": {"mode": "manual"},
                },
            }
        )
    except Exception as exc:
        assert "manual selection requires proposers" in str(exc)
    else:
        raise AssertionError("manual selection without proposers should fail")


def test_local_only_forces_ollama_provider() -> None:
    payload = TaskCreateRequest.model_validate(
        {
            "idempotency_key": "contract:local",
            "content": {"prompt": "Privado"},
            "model_requirements": {
                "cloud_allowed": False,
                "allowed_providers": ["ollama"],
            },
            "risk": {"data_classification": "local_only"},
        }
    )

    assert payload.model_requirements.cloud_allowed is False
    assert payload.model_requirements.allowed_providers == ["ollama"]


def test_local_only_allows_lmstudio_local_provider() -> None:
    payload = TaskCreateRequest.model_validate(
        {
            "idempotency_key": "contract:local-lmstudio",
            "content": {"prompt": "Privado"},
            "model_requirements": {
                "cloud_allowed": False,
                "allowed_providers": ["lmstudio"],
                "target_model": {
                    "provider": "lmstudio",
                    "deployment": "local",
                    "model": "local-model",
                },
                "fallback_allowed": False,
            },
            "risk": {"data_classification": "local_only"},
        }
    )

    assert payload.model_requirements.cloud_allowed is False
    assert payload.model_requirements.allowed_providers == ["lmstudio"]


def test_exact_target_external_deployment_requires_cloud_allowed() -> None:
    # Regresión fail-closed: "api" (u otro deployment no local) cuenta como
    # externo aunque el proveedor no esté en la lista de nombres cloud conocidos.
    try:
        TaskCreateRequest.model_validate({
            "idempotency_key": "contract:target-api-deployment",
            "content": {"prompt": "Privado"},
            "model_requirements": {
                "cloud_allowed": False,
                "allowed_providers": ["nvidia"],
                "target_model": {"provider": "nvidia", "deployment": "api", "model": "yi-large"},
            },
        })
    except Exception as exc:
        assert "cloud_allowed" in str(exc)
    else:
        raise AssertionError("api deployment target without cloud_allowed should fail")


def test_exact_target_must_respect_provider_and_local_only_boundaries() -> None:
    try:
        TaskCreateRequest.model_validate({
            "idempotency_key": "contract:target-provider",
            "content": {"prompt": "Privado"},
            "model_requirements": {
                "allowed_providers": ["ollama"],
                "target_model": {"provider": "deepseek", "deployment": "api", "model": "chat"},
            },
        })
    except Exception as exc:
        assert "target_model.provider" in str(exc) or "cloud_allowed" in str(exc)
    else:
        raise AssertionError("target provider outside allowlist should fail")

    try:
        TaskCreateRequest.model_validate({
            "idempotency_key": "contract:target-local-only",
            "content": {"prompt": "Privado"},
            "model_requirements": {
                "cloud_allowed": True,
                "allowed_providers": ["deepseek"],
                "target_model": {"provider": "deepseek", "deployment": "api", "model": "chat"},
            },
            "risk": {"data_classification": "local_only"},
        })
    except Exception as exc:
        assert "local_only target_model" in str(exc)
    else:
        raise AssertionError("local_only cloud target should fail")


def test_shared_v2_single_fixture_matches_broker_schema() -> None:
    fixture = Path(__file__).parent / "fixtures" / "broker_v2_single_request.json"
    payload = TaskCreateRequest.model_validate(json.loads(fixture.read_text(encoding="utf-8")))
    assert payload.execution.strategy == ExecutionStrategy.single
    assert payload.idempotency_key == "contract:capture-001:1:single"


def _client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def test_task_state_response_honours_the_published_schema(tmp_path: Path) -> None:
    """El estado de tarea es contrato, no JSON libre.

    Una app cliente que orquesta su propio bucle —ChatyGPT -- lee `progress`
    y, cuando el agente se pausa, `result.pending_tool_calls`. Aquí se valida
    la respuesta REAL del endpoint contra el esquema publicado en
    `fixtures/broker_task_state_response.schema.json`, en los tres momentos que
    la app atraviesa: encolada, pausada esperando sus herramientas y terminada.
    """
    client_tools = [{
        "name": "web_search",
        "description": "Búsqueda web que ejecuta la app cliente",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }]
    seen: list[dict] = []
    with _client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "contract:task-state",
            "content": {"prompt": "Investiga el estado del arte"},
            # skills vacías: la app aporta su propia búsqueda y por eso puede
            # llamarla `web_search` sin chocar con la skill integrada.
            "execution": {"strategy": "agent", "agent": {
                "skills": [], "max_iterations": 6, "client_tools": client_tools,
            }},
        })
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]

        seen.append(client.get(f"/api/v1/tasks/{task_id}").json())
        client.post("/api/v1/dispatcher/tick")
        paused = client.get(f"/api/v1/tasks/{task_id}").json()
        seen.append(paused)

        assert paused["status"] == "waiting_for_tools"
        pending = paused["result"]["pending_tool_calls"]
        assert pending[0]["name"] == "web_search"

        client.post(f"/api/v1/tasks/{task_id}/tool_results", json={
            "tool_results": [{"tool_call_id": pending[0]["id"], "content": "3 fuentes"}],
        })
        client.post("/api/v1/dispatcher/tick")
        done = client.get(f"/api/v1/tasks/{task_id}").json()
        seen.append(done)
        assert done["status"] == "completed"

    for state in seen:
        jsonschema.validate(state, TASK_STATE_SCHEMA)


def test_client_tool_may_take_the_name_of_a_disabled_skill() -> None:
    payload = TaskCreateRequest.model_validate({
        "idempotency_key": "contract:client-tool-name",
        "content": {"prompt": "Investiga"},
        "execution": {"strategy": "agent", "agent": {
            "skills": ["calculator"],
            "client_tools": [{"name": "web_search", "description": "La mía", "parameters": {}}],
        }},
    })

    assert [tool.name for tool in payload.execution.agent.client_tools] == ["web_search"]


def test_client_tool_cannot_shadow_a_skill_enabled_in_the_same_task() -> None:
    """Dos definiciones del mismo nombre en la misma llamada al modelo: el
    modelo no podría saber cuál pide y el broker ejecutaría una u otra según el
    orden. Se rechaza en el contrato, no en el bucle."""
    try:
        TaskCreateRequest.model_validate({
            "idempotency_key": "contract:client-tool-shadow",
            "content": {"prompt": "Investiga"},
            "execution": {"strategy": "agent", "agent": {
                "skills": ["web_search"],
                "client_tools": [{"name": "web_search", "description": "La mía", "parameters": {}}],
            }},
        })
    except Exception as exc:
        assert "enabled skill name" in str(exc)
    else:
        raise AssertionError("a client tool shadowing an enabled skill should fail")
