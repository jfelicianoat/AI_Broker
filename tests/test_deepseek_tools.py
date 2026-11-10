"""DeepSeek: ronda agéntica (chat_tools) y respuestas sin contenido.

Lo que la batería general de proveedores no cubría: el formato de tools de
OpenAI que se manda y se parsea, que sin tools no se envían las claves (la API
responde 400 a `"tools": []`), y los dos diagnósticos de una respuesta vacía.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.config import DeepSeekConfig
from app.providers import DeepSeekProvider, ProviderError
from app.schemas import TaskCreateRequest

_KEY_ENV = "AI_BROKER_TEST_DEEPSEEK_KEY"


@pytest.fixture(autouse=True)
def _credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_KEY_ENV, "secret")


def _request(**extra: Any) -> TaskCreateRequest:
    payload: dict[str, Any] = {
        "idempotency_key": "deepseek:tools",
        "content": {"prompt": "hola"},
        "model_requirements": {"cloud_allowed": True, "allowed_providers": ["deepseek"]},
    }
    payload.update(extra)
    return TaskCreateRequest.model_validate(payload)


def _provider(reply: dict[str, Any], seen: list[dict[str, Any]]) -> DeepSeekProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return DeepSeekProvider(
        DeepSeekConfig(enabled=True, api_key_env=_KEY_ENV),
        transport=httpx.MockTransport(handler),
    )


def _run(coro_factory) -> Any:
    async def scenario() -> Any:
        return await coro_factory()

    return asyncio.run(scenario())


def test_chat_tools_sends_tools_and_parses_calls() -> None:
    seen: list[dict[str, Any]] = []
    reply = {
        "choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "buscar", "arguments": '{"q": "gatos"}'}},
            {"function": {"name": "roto", "arguments": "{no json"}},
            {"function": {"name": "lista", "arguments": "[1, 2]"}},
        ]}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    provider = _provider(reply, seen)
    tools = [{"type": "function", "function": {"name": "buscar", "parameters": {}}}]

    async def go():
        try:
            return await provider.chat_tools(_request(), "deepseek-chat", [{"role": "user", "content": "x"}], tools)
        finally:
            await provider.close()

    turn = _run(go)
    assert seen[0]["tools"] == tools and seen[0]["tool_choice"] == "auto"
    assert [(call.id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("c1", "buscar", {"q": "gatos"}),
        ("call_1", "roto", {}),  # argumentos ilegibles: diccionario vacío, no excepción
        ("call_2", "lista", {}),  # JSON válido pero no objeto
    ]
    assert turn.content is None
    assert (turn.tokens_input, turn.tokens_output) == (7, 3)


def test_chat_tools_without_tools_omits_the_keys_and_returns_text() -> None:
    seen: list[dict[str, Any]] = []
    provider = _provider({"choices": [{"message": {"content": "listo"}}]}, seen)

    async def go():
        try:
            return await provider.chat_tools(_request(), "deepseek-chat", [{"role": "user", "content": "x"}], [])
        finally:
            await provider.close()

    turn = _run(go)
    assert "tools" not in seen[0] and "tool_choice" not in seen[0]
    assert turn.content == "listo" and turn.tool_calls == ()


@pytest.mark.parametrize("status", [429, 500])
def test_chat_tools_translates_http_errors(status: int) -> None:
    provider = DeepSeekProvider(
        DeepSeekConfig(enabled=True, api_key_env=_KEY_ENV),
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": "x"})),
    )

    async def go():
        try:
            await provider.chat_tools(_request(), "deepseek-chat", [], [])
        finally:
            await provider.close()

    with pytest.raises(ProviderError):
        _run(go)


def test_chat_tools_translates_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=request)

    provider = DeepSeekProvider(
        DeepSeekConfig(enabled=True, api_key_env=_KEY_ENV),
        transport=httpx.MockTransport(handler),
    )

    async def go():
        try:
            await provider.chat_tools(_request(), "deepseek-chat", [], [])
        finally:
            await provider.close()

    with pytest.raises(ProviderError):
        _run(go)


def test_generate_sends_system_and_json_format() -> None:
    seen: list[dict[str, Any]] = []
    provider = _provider({"choices": [{"message": {"content": '{"ok": true}'}}]}, seen)

    async def go():
        try:
            return await provider.generate(
                _request(output={"format": "json", "json_schema": {"type": "object"}}), "deepseek-chat", "dame json", system="eres un robot",
            )
        finally:
            await provider.close()

    output = _run(go)
    assert seen[0]["messages"][0] == {"role": "system", "content": "eres un robot"}
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert output.content == '{"ok": true}'


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"content": ""}, "INVALID_PROVIDER_RESPONSE"),
        ({"content": None, "reasoning_content": "pensando mucho..."}, None),
    ],
)
def test_generate_diagnoses_empty_answers(message: dict[str, Any], expected: str | None) -> None:
    seen: list[dict[str, Any]] = []
    provider = _provider({"choices": [{"message": message, "finish_reason": "length"}]}, seen)

    async def go():
        try:
            return await provider.generate(_request(), "deepseek-reasoner", "hola")
        finally:
            await provider.close()

    try:
        output = _run(go)
    except ProviderError as error:
        if expected is not None:
            assert error.code == expected
        return
    # Si el razonamiento se rescata como contenido, al menos no es una respuesta vacía.
    assert output.content


def test_known_features_distinguish_reasoner() -> None:
    assert DeepSeekProvider._known_features("deepseek-reasoner")["tools"] is False
    assert DeepSeekProvider._known_features("deepseek-chat")["tools"] is True
