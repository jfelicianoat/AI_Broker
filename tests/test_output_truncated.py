"""Respuestas cortadas por el tope de salida: ya no pasan como completas.

Regresión de task_42e80f83… (2026-10-25). Se pidieron 7500 tokens de salida;
el router eligió essentialai/rnj-1, que LM Studio cargaba a 8192 de ventana,
y el broker recortó el tope a 963 para que cupiera. El modelo paró por
longitud a mitad de una fórmula y la tarea quedó `completed`: nadie miraba
`finish_reason`, y el recorte no dejaba rastro en la tarea.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.config import BrokerConfig, OpenAICompatibleProviderConfig
from app.providers import ModelOutput, OllamaProvider, ProviderError, RoutedModelProvider
from app.providers.base import output_room_sufficient, output_truncated_error, served_generation
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas import ModelReference, TaskCreateRequest, TaskStatus
from tests.test_adaptive_routing import _CatalogStub, _local_entry, _ranking_config
from tests.test_outage_retry import FlakyProvider, _process, _setup

# Del tamaño del prompt real: ~13 KB de transcripción en español.
PROMPT = "La caída del 50 % exige una subida del 100 % para volver al capital inicial. " * 170


def _request(max_output_tokens: int = 7500, prompt: str = PROMPT) -> TaskCreateRequest:
    return TaskCreateRequest.model_validate({
        "idempotency_key": f"truncated:{uuid4().hex}",
        "content": {"prompt": prompt},
        "generation": {"max_output_tokens": max_output_tokens},
    })


def _lmstudio(state: dict, finish_reason: str) -> RoutedModelProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "essentialai/rnj-1"}]})
        if request.url.path == "/v1/chat/completions":
            state["max_tokens"] = json.loads(request.content)["max_tokens"]
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "## Fórmula\n\\[ \\text{Capital"},
                             "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 4158, "completion_tokens": 866},
            })
        return httpx.Response(404, json={})

    provider_config = OpenAICompatibleProviderConfig(**{
        "id": "lmstudio", "enabled": True, "base_url": "http://127.0.0.1:1234/v1",
        "api_key_env": None, "deployment": "local", "sync_models": True,
        "default_context_window": 8192, "probe_features": False,
    })
    config = BrokerConfig()
    return RoutedModelProvider(config, custom={
        "lmstudio": OpenAICompatibleProvider(provider_config, transport=httpx.MockTransport(handler)),
    })


class TestTheAdaptersReportTheCut:
    def test_the_original_case_fails_and_keeps_the_partial_text(self) -> None:
        state: dict = {}
        provider = _lmstudio(state, "length")
        model = ModelReference(provider="lmstudio", deployment="local", model="essentialai/rnj-1")
        with pytest.raises(ProviderError) as raised:
            asyncio.run(provider.propose(_request(), model, 1))
        asyncio.run(provider.close())
        error = raised.value
        assert error.code == "OUTPUT_TRUNCATED"
        # El recorte se dice en el mensaje: era la mitad de la explicación.
        assert "recortado desde 7500" in str(error)
        assert state["max_tokens"] < 7500
        # La respuesta cortada se conserva para auditarla.
        assert error.output is not None and "Capital" in (error.output.content or "")
        assert error.output.generation["max_output_tokens_requested"] == 7500
        assert error.output.technical_output()["stop_reason"] == "length"

    def test_a_finished_answer_still_gets_through(self) -> None:
        provider = _lmstudio({}, "stop")
        model = ModelReference(provider="lmstudio", deployment="local", model="essentialai/rnj-1")
        output = asyncio.run(provider.propose(_request(), model, 1))
        asyncio.run(provider.close())
        assert output.stop_reason is None

    @pytest.mark.parametrize(("done_reason", "fails"), [("length", True), ("stop", False)])
    def test_ollama_reads_done_reason(self, done_reason: str, fails: bool) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{
                    "name": "largo", "size": 1, "capabilities": ["completion"],
                    "details": {"context_length": 128000},
                }]})
            if request.url.path == "/api/chat":
                return httpx.Response(200, json={
                    "message": {"content": "Primera parte de una respuesta que sigue"},
                    "done_reason": done_reason, "prompt_eval_count": 10, "eval_count": 7500,
                })
            return httpx.Response(200, json={"models": []})

        config = BrokerConfig()
        provider = RoutedModelProvider(
            config, ollama=OllamaProvider(config, transport=httpx.MockTransport(handler)),
        )
        model = ModelReference(provider="ollama", deployment="local", model="largo")
        if fails:
            with pytest.raises(ProviderError) as raised:
                asyncio.run(provider.propose(_request(prompt="Hola"), model, 1))
            assert raised.value.code == "OUTPUT_TRUNCATED"
            # Sin recorte por ventana, el mensaje no se lo inventa.
            assert "recortado" not in str(raised.value)
        else:
            asyncio.run(provider.propose(_request(prompt="Hola"), model, 1))
        asyncio.run(provider.close())


class TestRoomForTheAnswer:
    def test_a_window_that_leaves_a_fraction_of_the_output_is_not_enough(self) -> None:
        request = _request()
        assert not output_room_sufficient(request, 8192)
        assert output_room_sufficient(request, 32768)

    def test_served_generation_only_mentions_a_real_cap(self) -> None:
        request = _request(max_output_tokens=500, prompt="Hola")
        assert "max_output_tokens_requested" not in served_generation(request, request)
        capped = request.model_copy(update={
            "generation": request.generation.model_copy(update={"max_output_tokens": 100}),
        })
        assert served_generation(request, capped)["max_output_tokens_requested"] == 500

    def test_the_router_prefers_a_model_with_room(self) -> None:
        # El de 8k gana por nombre en el desempate; sin el filtro saldría él.
        pequeno = {**_local_entry("a-ventana-8k"), "context_window": 8192}
        grande = {**_local_entry("b-ventana-32k"), "context_window": 32768}
        router = RoutedModelProvider(
            _ranking_config(), ollama=_CatalogStub([pequeno, grande]), deepseek=_CatalogStub([]),
        )
        selected = asyncio.run(router.select(_request(), 1, ["single"]))
        assert selected[0].model == "b-ventana-32k"

    def test_with_no_roomy_model_the_ones_that_fit_still_serve(self) -> None:
        pequeno = {**_local_entry("solo-8k"), "context_window": 8192}
        router = RoutedModelProvider(
            _ranking_config(), ollama=_CatalogStub([pequeno]), deepseek=_CatalogStub([]),
        )
        selected = asyncio.run(router.select(_request(), 1, ["single"]))
        assert selected[0].model == "solo-8k"


class TruncatedFirstModel(FlakyProvider):
    async def select(self, request, count, roles):
        names = ["corto", "largo", "otro"]
        return [
            ModelReference(provider="ollama", deployment="local", model=names[index % 3], role=role)
            for index, role in enumerate(roles)
        ]

    async def propose(self, request, model, ordinal):
        self.calls += 1
        if model.model == "corto":
            output = ModelOutput(
                content="mitad de", tokens_input=1, tokens_output=963, cost_usd=0.0, latency_ms=1.0,
                generation={"max_output_tokens": 963, "max_output_tokens_requested": 7500},
                stop_reason="length",
            )
            raise output_truncated_error("ollama", "corto", output)
        return ModelOutput(content="respuesta entera.", tokens_input=1, tokens_output=1,
                           cost_usd=0.0, latency_ms=1.0)


def test_single_fails_over_and_leaves_the_cut_visible(tmp_path: Path) -> None:
    coordinator, repository, db, task_id = _setup(tmp_path, TruncatedFirstModel())
    _process(coordinator, repository, task_id)

    state = repository.get_task(task_id)
    assert state.status == TaskStatus.completed
    assert state.result["model_used"]["model"] == "largo"
    events = {row["event_type"]: row["payload_json"] for row in db.query_all(
        "SELECT event_type, payload_json FROM events WHERE task_id = ?", (task_id,))}
    assert "single.failover" in events
    assert json.loads(events["generation.output_capped"])["max_output_tokens_requested"] == 7500
    failed = db.query_all(
        "SELECT error_code, output_json FROM model_invocations WHERE task_id = ? AND status = 'failed'",
        (task_id,),
    )
    assert [row["error_code"] for row in failed] == ["OUTPUT_TRUNCATED"]
    assert "mitad de" in failed[0]["output_json"]
