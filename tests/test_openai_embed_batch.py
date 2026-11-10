"""Lote de embeddings contra un endpoint OpenAI-compatible (`embed_batch`).

Lo que importa del contrato: el orden sale del `index` de cada vector y no de
su posición en `data`, un lote incompleto es un error y no un resultado corto,
y el modelo tiene que existir y declarar la capacidad antes de gastar nada.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.config import OpenAICompatibleProviderConfig
from app.providers import OpenAICompatibleProvider, ProviderError
from app.schemas import TaskCreateRequest

CATALOG = [
    {"name": "emb", "capabilities": ["embedding"], "context_window": 8192},
    {"name": "chat", "capabilities": ["completion"], "context_window": 8192},
]


def _request(**requirements: Any) -> TaskCreateRequest:
    return TaskCreateRequest.model_validate({
        "idempotency_key": "embed:batch",
        "content": {"prompt": "x"},
        "model_requirements": requirements,
    })


def _embed(handler, model: str = "emb", inputs: list[str] | None = None, **requirements: Any) -> Any:
    seen: list[dict[str, Any]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(request)

    provider = OpenAICompatibleProvider(
        OpenAICompatibleProviderConfig(
            id="local-emb", enabled=True, base_url="http://127.0.0.1:9999/v1",
            deployment="local", api_key_env=None,
        ),
        transport=httpx.MockTransport(recording),
    )

    async def models() -> list[dict[str, Any]]:
        return CATALOG

    provider.models = models  # type: ignore[method-assign]

    async def go() -> Any:
        try:
            return await provider.embed_batch(_request(**requirements), model, inputs or ["uno", "dos"])
        finally:
            await provider.close()

    return asyncio.run(go()), seen


def test_vectors_are_ordered_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"index": 1, "embedding": [2.0, 2.0]}, {"index": 0, "embedding": [1.0, 1.0]}],
            "usage": {"total_tokens": 4},
        })

    output, seen = _embed(handler)
    assert seen[0] == {"model": "emb", "input": ["uno", "dos"]}
    assert output.embeddings == ((1.0, 1.0), (2.0, 2.0))
    assert output.tokens_input == 4


def test_short_batch_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    with pytest.raises(ProviderError) as raised:
        _embed(handler)
    assert raised.value.code == "INVALID_PROVIDER_RESPONSE"


@pytest.mark.parametrize(("model", "code"), [("nope", "MODEL_UNAVAILABLE"), ("chat", "MODEL_CAPABILITY_MISMATCH")])
def test_model_must_exist_and_embed(model: str, code: str) -> None:
    with pytest.raises(ProviderError) as raised:
        _embed(lambda request: httpx.Response(500), model=model)
    assert raised.value.code == code


def test_http_and_transport_errors_are_translated() -> None:
    with pytest.raises(ProviderError):
        _embed(lambda request: httpx.Response(503, json={"error": "ocupado"}))

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("apagado", request=request)

    with pytest.raises(ProviderError):
        _embed(offline)


def test_budget_is_checked_before_calling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OpenAICompatibleProvider, "_costs", lambda self, model: (1_000_000.0, 0.0))
    with pytest.raises(ProviderError) as raised:
        _embed(lambda request: httpx.Response(500), max_cost_usd=0.000001)
    assert raised.value.code == "BUDGET_EXCEEDED"
