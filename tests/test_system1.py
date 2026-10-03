import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import BrokerConfig, LoggingConfig, PersistenceConfig, ProcessingConfig, ServerConfig
from app.logging_config import JsonLineFormatter
from app.main import create_app
from app.mcp import MCPError, MCPRegistry
from app.providers import OllamaProvider, ProviderError, RoutedModelProvider
from app.schemas import System1ModelJudgment, System1Request, TaskCreateRequest
from app.system1 import System1Service


def config():
    result = BrokerConfig()
    result.system1.enabled = True
    result.system1.provider_priority = ["laya_mcp", "ollama_system1"]
    result.system1.ollama.model = "test-system1"
    result.system1.routing.enabled = True
    result.system1.routing.shadow_mode = False
    result.routing.adaptive_selection = False
    result.task_affinity.enabled = False
    result.system1.routing.model_tiers = {
        "ollama/local/small": "simple", "ollama/local/medium": "medium", "ollama/local/large": "complex",
    }
    return result


def request(**updates):
    return System1Request.model_validate({
        "use_case": "ranking", "input": {"text": "private text"}, "decision_type": "choice",
        "options": ["simple", "medium", "complex"], "instructions": "Classify difficulty", **updates,
    })


def answer(value="simple", confidence=0.97, alternatives=None):
    return System1ModelJudgment.model_validate({
        "decision": value, "confidence": confidence,
        "alternatives": alternatives if alternatives is not None else [
            {"value": option, "confidence": 0.01} for option in ["simple", "medium", "complex"] if option != value
        ],
    }), "real-model"


def service(settings=None, side_effect=None):
    settings = settings or config()
    result = System1Service(settings, MCPRegistry(settings.mcp))
    result._invoke = AsyncMock(side_effect=side_effect, return_value=answer())
    return result


def run(coroutine):
    return asyncio.run(coroutine)


def test_laya_success_and_normalized_metadata():
    s = service()
    result = run(s.judge(request()))
    assert result.accepted and result.decision == "simple"
    assert result.provider == "laya_mcp" and result.model == "real-model"
    assert result.latency_ms >= 0 and result.attempts[0].latency_ms >= 0
    assert not result.fallback_used and not result.confidence_is_calibrated


def test_laya_error_falls_back_to_ollama():
    s = service(side_effect=[MCPError("unavailable"), answer()])
    result = run(s.judge(request()))
    assert result.accepted and result.provider == "ollama_system1"
    assert result.fallback_used and result.reason_code == "MCP_ERROR"
    assert len(result.attempts) == 2


def test_nimble_is_default_primary_and_laya_is_fallback():
    settings = config()
    settings.system1.provider_priority = BrokerConfig().system1.provider_priority
    assert settings.system1.provider_priority == ["ollama_system1", "laya_mcp"]
    s = service(settings)
    assert run(s.judge(request())).provider == "ollama_system1"
    s._invoke = AsyncMock(side_effect=[ProviderError("PROVIDER_UNAVAILABLE", "offline"), answer()])
    result = run(s.judge(request()))
    assert result.accepted and result.provider == "laya_mcp" and result.fallback_used
    assert [attempt.provider for attempt in result.attempts] == ["ollama_system1", "laya_mcp"]


@pytest.mark.parametrize("failure,reason", [
    (MCPError("failure"), "MCP_ERROR"), (ValueError("bad JSON"), "INVALID_OUTPUT"),
    (ProviderError("PROVIDER_UNAVAILABLE", "missing"), "PROVIDER_UNAVAILABLE"),
    (RuntimeError("unexpected"), "INTERNAL_PROVIDER_ERROR"),
])
def test_both_fail_no_corrupt_decision(failure, reason):
    s = service(side_effect=failure)
    result = run(s.judge(request()))
    assert not result.accepted and result.decision is None and result.fallback_used
    assert result.reason_code == reason


@pytest.mark.parametrize("usage", [{"input_tokens": "bad"}, {"output_tokens": -1}])
def test_invalid_provider_usage_also_falls_back_safely(usage):
    judgment, model = answer()
    s = service(side_effect=[(judgment, model, usage), (judgment, model, usage)])
    result = run(s.judge(request()))
    assert not result.accepted and result.decision is None and result.reason_code == "INVALID_OUTPUT"


def test_disabled_never_calls_provider():
    settings = config()
    settings.system1.enabled = False
    s = service(settings)
    result = run(s.judge(request()))
    assert result.reason_code == "SYSTEM1_DISABLED" and result.decision is None
    s._invoke.assert_not_called()


@pytest.mark.parametrize("judgment,reason", [
    (answer(confidence=0.80), "LOW_CONFIDENCE"),
    (answer(confidence=0.95, alternatives=[{"value": "medium", "confidence": 0.83},
                                       {"value": "complex", "confidence": 0.02}]), "INSUFFICIENT_MARGIN"),
    (answer("not-allowed"), "INVALID_OUTPUT"),
    (answer(alternatives=[]), "INVALID_OUTPUT"),
    (answer(alternatives=[{"value": "medium", "confidence": 0.99},
                         {"value": "complex", "confidence": 0.01}]), "INVALID_OUTPUT"),
])
def test_confidence_margin_and_strict_output(judgment, reason):
    s = service(side_effect=[judgment, judgment])
    result = run(s.judge(request()))
    assert not result.accepted and result.reason_code == reason and result.decision is None


def test_configured_threshold_and_fallback_policy():
    settings = config()
    settings.system1.thresholds["ranking"].confidence = 0.98
    settings.system1.fallback_policy = "current"
    s = service(settings)
    result = run(s.judge(request()))
    assert result.reason_code == "LOW_CONFIDENCE" and len(result.attempts) == 1


def test_unknown_profile_cannot_lower_threshold():
    s = service()
    result = run(s.judge(request(threshold_profile="typo")))
    assert result.reason_code == "UNKNOWN_THRESHOLD_PROFILE"
    s._invoke.assert_not_called()


def test_target_pins_the_judge_without_fallback():
    s = service(side_effect=[MCPError("down"), answer()])
    result = run(s.judge(request(target={"provider": "laya_mcp", "model": "english"})))
    assert not result.accepted and result.reason_code == "MCP_ERROR"
    assert [(a.provider, a.model) for a in result.attempts] == [("laya_mcp", "english")]
    assert s._invoke.call_args.args[3] == "english"
    s._invoke = AsyncMock(return_value=answer())
    result = run(s.judge(request(target={"provider": "ollama_system1"})))
    assert result.accepted and result.provider == "ollama_system1"
    assert s._invoke.call_args.args[0] == "ollama_system1" and s._invoke.call_args.args[3] is None
    with pytest.raises(ValueError):
        request(target={"provider": "otro"})


def test_rejected_attempts_keep_their_raw_scores_for_calibration():
    low = answer(confidence=0.40, alternatives=[{"value": "medium", "confidence": 0.35}, {"value": "complex", "confidence": 0.25}])
    s = service(side_effect=[low])
    result = run(s.judge(request()))
    assert not result.accepted and result.decision is None and result.confidence is None
    attempt = result.attempts[0]
    assert (attempt.decision, attempt.confidence) == ("simple", 0.40)
    assert {item.value for item in attempt.alternatives} == {"medium", "complex"}
    # An output that broke the contract has no scores worth reporting.
    s = service(side_effect=[answer("not-allowed"), answer("not-allowed")])
    attempt = run(s.judge(request())).attempts[0]
    assert attempt.reason_code == "INVALID_OUTPUT" and attempt.confidence is None and attempt.decision is None


def test_unsure_primary_is_not_rescued_by_the_next_provider():
    s = service(side_effect=[answer(confidence=0.80), answer(confidence=0.99)])
    result = run(s.judge(request()))
    assert not result.accepted and result.reason_code == "LOW_CONFIDENCE"
    assert [attempt.provider for attempt in result.attempts] == ["laya_mcp"]


def test_misspelt_use_case_cannot_lower_threshold():
    s = service()
    result = run(s.judge(request(use_case="rankin")))
    assert result.reason_code == "UNKNOWN_USE_CASE" and result.decision is None
    s._invoke.assert_not_called()
    # A use case of the caller's own asks for the default threshold by name.
    assert run(s.judge(request(use_case="my_own_case", threshold_profile="default"))).accepted


def test_native_judgment_does_not_queue_behind_a_generation():
    def respond(req):
        if req.url.path == "/v1/systemone":
            return httpx.Response(200, json={"model": "test-system1", "answers": {"decision": {
                "type": "choice", "choice": "simple", "probabilities": {"simple": 0.97, "medium": 0.02, "complex": 0.01}}}})
        return httpx.Response(200, json={"models": []})
    async def scenario(capabilities):
        settings = config()
        settings.system1.provider_priority = ["ollama_system1"]
        settings.system1.timeout_seconds = 0.3
        settings.resources.gpu_layer_offload = False
        ollama = OllamaProvider(settings, transport=httpx.MockTransport(respond))
        ollama.models = AsyncMock(return_value=[{
            "name": "test-system1", "deployment": "local", "capabilities": capabilities,
            "context_window": 32768, "size_bytes": 0}])
        busy = asyncio.Semaphore(1)
        await busy.acquire()  # a generation holds the serial inference slot
        try:
            pinned = request(target={"provider": "ollama_system1"})
            return (await System1Service(settings, MCPRegistry(settings.mcp), ollama, busy).judge(pinned)).reason_code
        finally:
            await ollama.close()
    assert run(scenario(["decision", "completion"])) is None
    # An ordinary model generates its judgment, so it still waits its turn.
    assert run(scenario(["completion"])) == "TIMEOUT"


def test_missing_instructions_does_not_guess_business_logic():
    s = service()
    result = run(s.judge(request(instructions=None)))
    assert result.reason_code == "MISSING_INSTRUCTIONS"
    s._invoke.assert_not_called()


def test_timeout_and_cancellation():
    async def scenario():
        settings = config()
        settings.system1.timeout_seconds = 0.01
        s = service(settings)
        async def hangs(*args):
            await asyncio.sleep(10)
        s._invoke = AsyncMock(side_effect=hangs)
        result = await s.judge(request())
        assert result.reason_code == "TIMEOUT" and len(result.attempts) == 2
        task = asyncio.create_task(s.judge(request()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())


def catalog():
    return [{"provider": "ollama", "deployment": "local", "name": name,
             "capabilities": ["completion"], "context_window": 32768, "compatibility": "compatible"}
            for name in ["large", "medium", "small"]]


def task(**updates):
    return TaskCreateRequest.model_validate({
        "idempotency_key": "system1-test", "content": {"prompt": "What is two plus two?"},
        "execution": {"strategy": "single"}, **updates,
    })


@pytest.mark.parametrize("level,chosen", [("simple", "small"), ("medium", "medium"), ("complex", "large")])
def test_routing_chooses_required_capability(level, chosen):
    s = service(side_effect=[answer(level)])
    result = run(s.route(task(), catalog()))
    assert result[0]["name"] == chosen
    if level == "complex":
        assert [entry["name"] for entry in result] == ["large"]


@pytest.mark.parametrize("side_effect", [MCPError("offline"), answer(confidence=0.80),
                                       answer(confidence=0.95, alternatives=[
                                           {"value": "medium", "confidence": 0.85},
                                           {"value": "complex", "confidence": 0.02}])])
def test_routing_preserves_previous_on_failure(side_effect):
    effects = side_effect if isinstance(side_effect, Exception) else [side_effect, side_effect]
    s = service(side_effect=effects)
    entries = catalog()
    assert run(s.route(task(), entries)) is entries


def test_shadow_mode_records_proposed_and_actual():
    settings = config()
    settings.system1.routing.shadow_mode = True
    s = service(settings)
    entries = catalog()
    assert run(s.route(task(), entries)) is entries
    event = s.metrics()["recent"][-1]
    assert event["proposed_model"] == "small" and event["model"] == "large"
    assert event["shadow_mode"]


def test_unknown_tiers_do_not_invent_model_capabilities():
    settings = config()
    settings.system1.routing.model_tiers = {}
    s = service(settings)
    entries = catalog()
    assert run(s.route(task(), entries)) is entries
    s._invoke.assert_not_called()


def test_deterministic_filters_and_explicit_selection_precede_system1():
    async def scenario():
        settings = config()
        provider = RoutedModelProvider(settings)
        s = service(settings)
        provider.system1 = s
        entries = catalog()
        entries[2]["capabilities"] = ["embedding"]
        provider.models = AsyncMock(return_value=entries)
        provider.loaded_local_models = AsyncMock(return_value=frozenset())
        try:
            selected = await provider.select(task(), 1, ["single"])
            assert selected[0].model != "small"  # the judgment cannot restore this candidate
            entries[2]["capabilities"] = ["completion"]
            targeted = task(model_requirements={"target_model": {
                "provider": "ollama", "deployment": "local", "model": "large"}})
            s._invoke.reset_mock()
            selected = await provider.select(targeted, 1, ["single"])
            assert selected[0].model == "large"
            s._invoke.assert_not_called()
            settings.system1.enabled = False
            selected = await provider.select(task(), 1, ["single"])
            assert selected[0].model == "large"
            s._invoke.assert_not_called()
        finally:
            await provider.close()
    run(scenario())


def test_telemetry_is_json_and_does_not_log_input(caplog):
    s = service(side_effect=[MCPError("down"), answer()])
    with caplog.at_level(logging.INFO, logger="ai_broker.system1"):
        run(s.judge(request()))
    formatted = [json.loads(JsonLineFormatter().format(record)) for record in caplog.records]
    result = next(record for record in formatted if record["event"] == "system1.decision")
    assert result["provider"] == "ollama_system1" and result["latency_ms"] >= 0
    assert result["fallback_used"] and result["confidence"] == 0.97
    assert "private text" not in json.dumps(formatted)


@pytest.mark.parametrize("update", [
    {"options": ["same", "same"]}, {"decision_type": "binary"},
    {"decision_type": "score", "options": [], "rubric": []},
    {"criteria": {"unrequested": "bogus"}},
])
def test_request_rejects_invalid_shapes(update):
    with pytest.raises(ValueError):
        request(**update)


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.1, "0.97", True])
def test_confidence_rejects_non_scores(confidence):
    with pytest.raises(ValueError):
        answer(confidence=confidence)


def test_ollama_reuses_json_generation_and_lifecycle():
    calls = []
    def respond(req):
        calls.append(req)
        if req.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": json.dumps({
                "decision": "simple", "scores": {"simple": 0.97, "medium": 0.02, "complex": 0.01}})},
                "prompt_eval_count": 32, "eval_count": 18})
        return httpx.Response(200, json={"models": []})
    async def scenario():
        settings = config()
        settings.system1.provider_priority = ["ollama_system1"]
        settings.resources.gpu_layer_offload = False
        ollama = OllamaProvider(settings, transport=httpx.MockTransport(respond))
        ollama.models = AsyncMock(return_value=[{
            "name": "test-system1", "provider": "ollama", "deployment": "local",
            "context_window": 32768, "capabilities": ["completion"], "size_bytes": 0}])
        s = System1Service(settings, MCPRegistry(settings.mcp), ollama)
        try:
            result = await s.judge(request(target={"provider": "ollama_system1"}))
            assert not result.accepted and result.reason_code == "SELF_REPORTED_SCORE" and result.decision is None
            attempt = result.attempts[0]
            assert (attempt.decision, attempt.confidence, attempt.score_source) == ("simple", 0.97, "self_reported")
            assert (attempt.tokens_input, attempt.tokens_output) == (32, 18)
            chat = next(req for req in calls if req.url.path == "/api/chat")
            payload = json.loads(chat.content)
            assert payload["model"] == "test-system1" and payload["format"]["type"] == "object"
            assert payload["options"]["temperature"] == 0
        finally:
            await ollama.close()
    run(scenario())


def test_laya_translates_real_wrapped_result(monkeypatch):
    settings = config()
    settings.mcp.enabled = True
    registry = MCPRegistry(settings.mcp)
    monkeypatch.setattr(type(registry), "enabled", property(lambda self: True))
    registry.data_boundary = lambda server: "local"
    registry.call_structured = AsyncMock(return_value={"result": json.dumps({
        "answers": {"decision": {"type": "choice", "choice": "simple", "probabilities": {
            "simple": 0.97, "medium": 0.02, "complex": 0.01}, "confidence": 0.8}},
        "routing": {"repo": "real-checkpoint"}})})
    s = System1Service(settings, registry)
    result = run(s.judge(request()))
    assert result.accepted and result.confidence == 0.97 and result.model == "real-checkpoint"


@pytest.mark.parametrize("kind,typed,expected", [
    ("choice", {"type": "choice", "choice": "simple", "probabilities": {
        "simple": 0.97, "medium": 0.02, "complex": 0.01}, "confidence": 0.8}, "simple"),
    ("binary", {"type": "noul", "noul": 0.01}, False),
    ("score", {"type": "score", "score": 0.98, "probabilities": {"0": 0.02, "1": 0.98}}, 1.0),
])
def test_ollama_native_decisions_use_systemone_and_normalize(kind, typed, expected):
    calls = []
    def respond(req):
        calls.append(req)
        if req.url.path == "/v1/systemone":
            return httpx.Response(200, json={"model": "test-system1", "answers": {"decision": typed},
                                            "usage": {"input_tokens": 40, "output_tokens": 1}})
        return httpx.Response(200, json={"models": []})
    async def scenario():
        settings = config()
        settings.system1.provider_priority = ["ollama_system1"]
        settings.resources.gpu_layer_offload = False
        ollama = OllamaProvider(settings, transport=httpx.MockTransport(respond))
        ollama.models = AsyncMock(return_value=[{
            "name": "test-system1", "deployment": "local", "capabilities": ["decision", "completion"],
            "context_window": 32768, "size_bytes": 0}])
        judgment_request = request() if kind == "choice" else request(
            decision_type=kind, options=[], rubric=["none", "high"] if kind == "score" else [])
        try:
            s = System1Service(settings, MCPRegistry(settings.mcp), ollama)
            result = await s.judge(judgment_request)
            assert result.accepted and result.decision == expected
            assert result.attempts[0].tokens_input == 40 and result.attempts[0].tokens_output == 1
            native = next(req for req in calls if req.url.path == "/v1/systemone")
            payload = json.loads(native.content)
            assert payload["state"] == judgment_request.input
            assert payload["questions"]["decision"]["type"] == ("noul" if kind == "binary" else kind)
            assert all(req.url.path != "/api/chat" for req in calls)
            assert not ollama.lifecycle._leases
        finally:
            await ollama.close()
    run(scenario())


def test_privacy_filters_system1_providers_before_sending_data(monkeypatch):
    settings = config()
    registry = MCPRegistry(settings.mcp)
    monkeypatch.setattr(type(registry), "enabled", property(lambda self: True))
    registry.data_boundary = lambda server: "egress"
    registry.call_structured = AsyncMock()
    ollama = AsyncMock()
    ollama.models.return_value = [{"name": "test-system1", "deployment": "cloud", "capabilities": ["completion"]}]
    s = System1Service(settings, registry, ollama)
    result = run(s.judge(request()))
    assert not result.accepted and all(attempt.reason_code == "CLOUD_NOT_ALLOWED" for attempt in result.attempts)
    registry.call_structured.assert_not_called()
    ollama.generate.assert_not_called()


def test_auxiliary_optout_skips_semantic_judgment():
    s = service()
    entries = catalog()
    assert run(s.route(task(auxiliary_invocations=False), entries)) is entries
    s._invoke.assert_not_called()


def test_native_decision_model_not_reintroduced_as_response_model():
    async def scenario():
        settings = config()
        provider = RoutedModelProvider(settings)
        entries = catalog()
        entries[2]["capabilities"].append("decision")
        provider.models = AsyncMock(return_value=entries)
        provider.loaded_local_models = AsyncMock(return_value=frozenset())
        provider.system1 = service(settings)
        try:
            assert (await provider.select(task(), 1, ["single"]))[0].model != "small"
        finally:
            await provider.close()
    run(scenario())


def test_cost_restriction_runs_before_system1():
    async def scenario(shadow):
        settings = config()
        settings.system1.routing.shadow_mode = shadow
        settings.system1.routing.model_tiers["cloud-unknown/cloud/tiny"] = "simple"
        provider = RoutedModelProvider(settings)
        provider.system1 = service(settings)
        unpriced = {**catalog()[0], "provider": "cloud-unknown", "deployment": "cloud", "name": "tiny"}
        provider.models = AsyncMock(return_value=[unpriced, *catalog()])
        provider.loaded_local_models = AsyncMock(return_value=frozenset())
        budget = {"cloud_allowed": True, "max_cost_usd": 0.5}
        try:
            automatic = await provider.select(task(model_requirements=budget), 1, ["single"])
            targeted = await provider.select(task(model_requirements={**budget, "fallback_allowed": False, "target_model": {
                "provider": "cloud-unknown", "deployment": "cloud", "model": "tiny"}}), 1, ["single"])
            return automatic[0].model, targeted[0].model
        finally:
            await provider.close()
    # The judgment says "simple"; the unpriced cloud model cannot prove it fits.
    assert run(scenario(shadow=False)) == ("small", "tiny")
    # Shadow mode and explicit targets keep exactly the previous selection.
    assert run(scenario(shadow=True)) == ("tiny", "tiny")


def test_laya_refuses_input_it_would_truncate(monkeypatch):
    settings = config()
    settings.system1.provider_priority = ["laya_mcp"]
    registry = MCPRegistry(settings.mcp)
    monkeypatch.setattr(type(registry), "enabled", property(lambda self: True))
    registry.data_boundary = lambda server: "local"
    registry.call_structured = AsyncMock()
    s = System1Service(settings, registry)
    result = run(s.judge(request(input={"text": "relleno " * 200 + "demuestra el teorema"})))
    assert not result.accepted and result.decision is None and result.reason_code == "INPUT_TOO_LARGE"
    registry.call_structured.assert_not_called()


def test_operator_instructions_are_bounded_at_load_time():
    with pytest.raises(ValueError):
        BrokerConfig.model_validate({"system1": {"routing": {"instructions": "x" * 4001}}})
    with pytest.raises(ValueError):
        BrokerConfig.model_validate({"system1": {"thresholds": {"ranking": {"instructions": "x" * 4001}}}})


def test_api_contract_and_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEM1_TEST_TOKEN", "test-token")
    settings = BrokerConfig(
        server=ServerConfig(host="127.0.0.1", admin_token_env="SYSTEM1_TEST_TOKEN"),
        processing=ProcessingConfig(provider_mode="bootstrap", auto_dispatch=False),
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        logging=LoggingConfig(directory=str(tmp_path / "logs"), console_enabled=False),
    )
    settings.system1.enabled = True
    app = create_app(settings)
    app.state.system1._invoke = AsyncMock(return_value=answer())
    with TestClient(app) as client:
        assert client.post("/api/v1/system1/judge", json=request().model_dump()).status_code == 403
        response = client.post("/api/v1/system1/judge", json=request().model_dump(),
                               headers={"X-Admin-Token": "test-token"})
        assert response.status_code == 200 and response.json()["accepted"]
        assert client.get("/api/v1/system1/metrics").status_code == 403
        assert client.get("/api/v1/system1/metrics", headers={"X-Admin-Token": "test-token"}).status_code == 200
        # A malformed body without credential answers the credential, not the contract's field names.
        malformed = client.post("/api/v1/system1/judge", json={})
        assert malformed.status_code == 403 and "fields" not in malformed.text
        assert client.post("/api/v1/system1/judge", json={}, headers={"X-Admin-Token": "test-token"}).status_code == 422
        assert client.get("/api/v1/models/context").status_code == 422  # public route keeps its 422


@pytest.mark.parametrize("alternatives", [[], [{"value": True, "confidence": 0.0}]])
def test_binary_judgment_without_its_opposite_is_not_accepted(alternatives):
    lone = System1ModelJudgment.model_validate({"decision": True, "confidence": 1.0, "alternatives": alternatives}), "m"
    s = service(side_effect=[lone, lone])
    result = run(s.judge(request(decision_type="binary", options=[])))
    assert not result.accepted and result.reason_code == "INVALID_OUTPUT"
    assert result.attempts[0].confidence is None


def test_system2_teacher_judges_only_when_pinned_and_is_never_accepted():
    calls = []
    def respond(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"message": {"content": json.dumps({
            "decision": "true", "scores": {"true": 1.0, "false": 0.0}})}})
    async def scenario(target):
        settings = config()
        settings.system1.provider_priority = ["ollama_system1"]
        settings.resources.gpu_layer_offload = False
        ollama = OllamaProvider(settings, transport=httpx.MockTransport(respond))
        ollama.models = AsyncMock(return_value=[{"name": "lfm2:24b", "provider": "ollama", "deployment": "local",
                                                  "context_window": 32768, "capabilities": ["completion"], "size_bytes": 0}])
        settings.system1.ollama.model = "lfm2:24b"
        try:
            return await System1Service(settings, MCPRegistry(settings.mcp), ollama).judge(
                request(decision_type="binary", options=[], target=target))
        finally:
            await ollama.close()
    # Configured as the automatic judge, a generative model is refused unseen.
    result = run(scenario(None))
    assert result.reason_code == "MODEL_CAPABILITY_MISMATCH" and "/api/chat" not in calls
    # Pinned on purpose it labels, its numbers are kept, and nothing is accepted.
    result = run(scenario({"provider": "ollama_system1", "model": "lfm2:24b"}))
    attempt = result.attempts[0]
    assert not result.accepted and result.reason_code == "SELF_REPORTED_SCORE"
    assert (attempt.decision, attempt.confidence, attempt.score_source) == (True, 1.0, "self_reported")


def test_native_scores_are_tagged_native():
    s = service(side_effect=[answer(confidence=0.40, alternatives=[
        {"value": "medium", "confidence": 0.35}, {"value": "complex", "confidence": 0.25}])])
    assert run(s.judge(request())).attempts[0].score_source == "native"


@pytest.mark.parametrize("content,reason", [
    ('{"decision": "simple", "scores": {"simple": 0.9, "medium": 0.1}}', "INVALID_OUTPUT"),        # a label missing
    ('{"decision": "simple", "scores": {"simple": 0.2, "medium": 0.7, "complex": 0.1}}', "INVALID_OUTPUT"),  # not top1
    ('{"decision": "simple", "scores": {"simple": 0.8, "medium": 0.1, "complex": 0.1}}', "SELF_REPORTED_SCORE"),
])
def test_generated_scores_cover_every_label_and_pick_the_top(content, reason):
    def respond(req):
        return httpx.Response(200, json={"message": {"content": content}})
    async def scenario():
        settings = config()
        settings.system1.provider_priority = ["ollama_system1"]
        settings.resources.gpu_layer_offload = False
        ollama = OllamaProvider(settings, transport=httpx.MockTransport(respond))
        ollama.models = AsyncMock(return_value=[{"name": "teacher", "provider": "ollama", "deployment": "local",
                                                  "context_window": 32768, "capabilities": ["completion"], "size_bytes": 0}])
        try:
            return await System1Service(settings, MCPRegistry(settings.mcp), ollama).judge(
                request(target={"provider": "ollama_system1", "model": "teacher"}))
        finally:
            await ollama.close()
    assert run(scenario()).reason_code == reason
