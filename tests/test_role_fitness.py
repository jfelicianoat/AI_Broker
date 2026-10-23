"""Aptitud por rol de consenso y error de quórum con el contrato completo."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import BrokerConfig, ModelQuarantineConfig
from app.db import Database
from app.model_quarantine import ArbiterRecord, RoleUnfitness, load_arbiter_record, load_role_unfitness, role_group
from app.providers import ModelOutput, ProviderError
from app.providers.routing import RoutedModelProvider
from app.schemas import TaskCreateRequest
from tests.test_model_quarantine import BrokenProposerProvider, _db, _now, _run


def _invocation(db: Database, model: str, role: str, status: str, code: str | None, index: int) -> None:
    stamp = _now(index)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_invocations "
            "(id, task_id, role, provider, deployment, model, status, error_code, "
            " created_at, updated_at, task_type) "
            "VALUES (?, 't', ?, 'ollama', 'local', ?, ?, ?, ?, ?, 'prose')",
            (f"inv_{model}_{role}_{index}", role, model, status, code, stamp, stamp),
        )


def test_role_groups() -> None:
    assert role_group("skeptic") == "proposer"
    assert role_group("arbiter") == "arbiter"
    assert role_group("single") is None


def test_a_model_that_echoes_as_proposer_is_unfit_only_for_that_role(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Como lfm2:24b: casi perfecto en single, eco del prompt como proponente.
    for index in range(20):
        _invocation(db, "lfm2:24b", "single", "completed", None, index)
    for index in range(20, 26):
        failed = index % 2 == 0
        _invocation(db, "lfm2:24b", "generalist", "failed" if failed else "completed",
                    "PROMPT_ECHOED" if failed else None, index)
    unfit = load_role_unfitness(db, ModelQuarantineConfig())
    assert set(unfit) == {("ollama", "local", "lfm2:24b", "proposer")}
    entry = unfit[("ollama", "local", "lfm2:24b", "proposer")]
    assert (entry.attempts, entry.failures) == (6, 3)
    assert "PROMPT_ECHOED" in entry.reason


def test_circumstantial_failures_and_few_samples_do_not_count(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for index in range(6):
        _invocation(db, "grande:70b", "skeptic", "failed", "VRAM_INSUFFICIENT", index)
    for index in range(10, 14):
        _invocation(db, "nuevo:8b", "arbiter", "failed", "PROMPT_ECHOED", index)
    assert load_role_unfitness(db, ModelQuarantineConfig()) == {}


def _router(unfit: dict) -> RoutedModelProvider:
    router = RoutedModelProvider(BrokerConfig(), role_fitness_loader=lambda: unfit)
    return router


CATALOG = [
    {"provider": "ollama", "deployment": "local", "name": name}
    for name in ["lfm2:24b", "qwen3:30b", "gemma4:12b"]
]
UNFIT = {("ollama", "local", "lfm2:24b", "proposer"): RoleUnfitness(6, 3, "PROMPT_ECHOED")}


def test_router_skips_unfit_proposer_but_keeps_it_for_single() -> None:
    router = _router(UNFIT)
    proposers = router._without_unfit_for_role(CATALOG, ["generalist", "specialist"])
    assert [entry["name"] for entry in proposers] == ["qwen3:30b", "gemma4:12b"]
    assert router._without_unfit_for_role(CATALOG, ["single"]) == CATALOG
    # Lo apartado como proponente no afecta al árbitro.
    assert router._without_unfit_for_role(CATALOG, ["arbiter"]) == CATALOG
    asyncio.run(router.close())


def test_router_fails_open_when_nobody_would_remain() -> None:
    unfit = {
        ("ollama", "local", entry["name"], "proposer"): RoleUnfitness(6, 6, "PROMPT_ECHOED")
        for entry in CATALOG
    }
    router = _router(unfit)
    assert router._without_unfit_for_role(CATALOG, ["generalist"]) == CATALOG
    asyncio.run(router.close())


def test_select_applies_role_fitness_unless_a_model_is_preferred() -> None:
    class Stub:
        async def models(self):
            return [
                {"name": entry["name"], "provider": "ollama", "deployment": "local",
                 "context_window": 32768, "capabilities": ["completion"]}
                for entry in CATALOG
            ]

        async def close(self):
            return None

    router = RoutedModelProvider(BrokerConfig(), ollama=Stub(), deepseek=Stub(), custom={},
                                 role_fitness_loader=lambda: UNFIT)
    request = TaskCreateRequest.model_validate({"idempotency_key": "rf:1", "content": {"prompt": "hola"}})
    chosen = asyncio.run(router.select(request, 3, ["generalist", "specialist", "skeptic"]))
    assert "lfm2:24b" not in {model.model for model in chosen}
    preferred = TaskCreateRequest.model_validate({
        "idempotency_key": "rf:2", "content": {"prompt": "hola"},
        "model_requirements": {"preferred_model": "lfm2:24b"},
    })
    chosen = asyncio.run(router.select(preferred, 1, ["generalist"]))
    assert chosen[0].model == "lfm2:24b"
    asyncio.run(router.close())


class TwoBrokenProposers(BrokenProposerProvider):
    async def propose(self, request, model, ordinal):
        if model.model == "bueno_1":
            raise ProviderError("PROMPT_ECHOED", "devolvió su propio prompt")
        if model.model == self.broken:
            raise ProviderError("PROVIDER_UNAVAILABLE", "caído", retryable=True)
        return ModelOutput(content="propuesta", tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0)


class AllDefinitive(BrokenProposerProvider):
    async def propose(self, request, model, ordinal):
        if model.model == "bueno_2":
            return ModelOutput(content="propuesta", tokens_input=1, tokens_output=1, cost_usd=0.0, latency_ms=1.0)
        raise ProviderError("PROMPT_ECHOED", "devolvió su propio prompt")


def test_quorum_error_carries_message_and_retryable(tmp_path: Path) -> None:
    state, _ = _run(tmp_path, TwoBrokenProposers())
    error = state["error"]
    assert state["status"] == "failed"
    assert error["code"] == "CONSENSUS_QUORUM_NOT_REACHED"
    assert "1 de 3" in error["message"] and "PROMPT_ECHOED" in error["message"]
    # Una de las bajas fue transitoria: reintentar tiene sentido.
    assert error["retryable"] is True


def test_quorum_error_is_not_retryable_when_every_casualty_was_definitive(tmp_path: Path) -> None:
    state, _ = _run(tmp_path, AllDefinitive())
    assert state["error"]["code"] == "CONSENSUS_QUORUM_NOT_REACHED"
    assert state["error"]["retryable"] is False


def _arbiter(db: Database, model: str, task_type: str, status: str, code: str | None, index: int) -> None:
    stamp = _now(index)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_invocations "
            "(id, task_id, role, provider, deployment, model, status, error_code, "
            " created_at, updated_at, task_type) "
            "VALUES (?, 't', 'arbiter', 'ollama', 'local', ?, ?, ?, ?, ?, ?)",
            (f"arb_{model}_{task_type}_{index}", model, status, code, stamp, stamp, task_type),
        )


def test_arbiter_record_is_per_task_type_and_ignores_circumstantial_failures(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _arbiter(db, "ornith:35b", "long_context", "failed", "PROMPT_ECHOED", 1)
    _arbiter(db, "ornith:35b", "prose", "completed", None, 2)
    _arbiter(db, "qwen3.6:35b", "long_context", "failed", "VRAM_INSUFFICIENT", 3)
    records = load_arbiter_record(db, ModelQuarantineConfig())
    assert records[("ollama", "local", "ornith:35b", "long_context")].mostly_failing
    assert not records[("ollama", "local", "ornith:35b", "prose")].mostly_failing
    assert ("ollama", "local", "qwen3.6:35b", "long_context") not in records


def test_an_arbiter_that_echoes_long_texts_goes_behind_a_smaller_clean_one() -> None:
    from app.task_classifier import task_type_scope

    catalog = [
        {"provider": "ollama", "deployment": "local", "name": "ornith:35b", "parameter_size": "35B"},
        {"provider": "ollama", "deployment": "local", "name": "gemma4:12b", "parameter_size": "12B"},
    ]
    records = {("ollama", "local", "ornith:35b", "long_context"): ArbiterRecord(attempts=1, failures=1)}
    router = RoutedModelProvider(BrokerConfig(), arbiter_record_loader=lambda: records)
    request = TaskCreateRequest.model_validate({"idempotency_key": "arb:1", "content": {"prompt": "hola"}})
    with task_type_scope("long_context"):
        assert [e["name"] for e in router._rank_arbiters(catalog, request)] == ["gemma4:12b", "ornith:35b"]
    # En prosa no tiene ese historial: vuelve a mandar el tamaño.
    with task_type_scope("prose"):
        assert [e["name"] for e in router._rank_arbiters(catalog, request)] == ["ornith:35b", "gemma4:12b"]
    asyncio.run(router.close())
