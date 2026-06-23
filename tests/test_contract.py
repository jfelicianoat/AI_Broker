from app.schemas import (
    ExecutionStrategy,
    SelectionMode,
    TaskCreateRequest,
)


def test_task_contract_defaults_to_single() -> None:
    payload = TaskCreateRequest.model_validate({"content": {"prompt": "Resume este texto"}})

    assert payload.execution.strategy == ExecutionStrategy.single
    assert payload.execution.selection.mode == SelectionMode.auto
    assert payload.model_requirements.allowed_providers == ["ollama"]


def test_legacy_execution_mode_alias_is_accepted() -> None:
    payload = TaskCreateRequest.model_validate(
        {
            "content": {"prompt": "Analiza"},
            "execution": {"mode": "single"},
        }
    )

    assert payload.execution.strategy == ExecutionStrategy.single


def test_manual_selection_requires_proposers_and_arbiter() -> None:
    try:
        TaskCreateRequest.model_validate(
            {
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

