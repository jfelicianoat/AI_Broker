import json
from pathlib import Path

from app.schemas import (
    ExecutionStrategy,
    SelectionMode,
    TaskCreateRequest,
)


def test_task_contract_defaults_to_single() -> None:
    payload = TaskCreateRequest.model_validate({"idempotency_key": "contract:single", "content": {"prompt": "Resume este texto"}})

    assert payload.execution.strategy == ExecutionStrategy.single
    assert payload.execution.selection.mode == SelectionMode.auto
    assert payload.model_requirements.allowed_providers == ["ollama"]


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
