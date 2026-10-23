"""El contexto que el broker atribuye a un modelo de LM Studio es el que tiene
de verdad: LM Studio carga bajo demanda con su `defaultContextLength` (8192 en
el PC de IA) y rechaza con 400 lo que no quepa."""
from __future__ import annotations

import asyncio

import httpx

from app.config import OpenAICompatibleModelConfig, OpenAICompatibleProviderConfig
from app.providers.openai_compatible import OpenAICompatibleProvider


def _provider(state: dict, **overrides) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        state.setdefault("paths", []).append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "cargado"}, {"id": "en_disco"}]})
        if request.url.path == "/api/v0/models":
            if state.get("native_down"):
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"data": [
                {"id": "cargado", "state": "loaded", "loaded_context_length": 65536,
                 "max_context_length": 262144},
                {"id": "en_disco", "state": "not-loaded", "max_context_length": 262144},
            ]})
        return httpx.Response(404, json={})

    config = OpenAICompatibleProviderConfig(**{
        "id": "lmstudio", "enabled": True, "base_url": "http://127.0.0.1:1234/v1",
        "api_key_env": None, "deployment": "local", "sync_models": True,
        "default_context_window": 32768, "jit_context_window": 8192,
        "lmstudio_loaded_context": True, **overrides,
    })
    return OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))


def _contexts(provider: OpenAICompatibleProvider) -> dict[str, tuple[int, str]]:
    async def run():
        catalog = await provider.models()
        await provider.close()
        return {entry["name"]: (entry["context_window"], entry["context_window_source"]) for entry in catalog}

    return asyncio.run(run())


def test_loaded_context_wins_and_unloaded_models_get_the_jit_context() -> None:
    contexts = _contexts(_provider({}))
    # Cargado a mano con 64k: eso es lo que hay en memoria, aunque se
    # declarasen 32k. Sin cargar: lo cargaría con 8k.
    assert contexts == {"cargado": (65536, "loaded"), "en_disco": (8192, "jit_default")}


def test_without_the_native_api_the_jit_context_still_applies() -> None:
    contexts = _contexts(_provider({"native_down": True}))
    assert contexts == {"cargado": (8192, "jit_default"), "en_disco": (8192, "jit_default")}


def test_options_off_keep_the_declared_context_and_skip_the_native_api() -> None:
    state: dict = {}
    contexts = _contexts(_provider(state, jit_context_window=None, lmstudio_loaded_context=False))
    assert contexts == {"cargado": (32768, "default"), "en_disco": (32768, "default")}
    assert "/api/v0/models" not in state["paths"]


def test_jit_context_never_raises_a_smaller_declared_context() -> None:
    provider = _provider({}, lmstudio_loaded_context=False, models=[
        OpenAICompatibleModelConfig(name="en_disco", context_window=4096),
    ])
    assert _contexts(provider)["en_disco"] == (4096, "configured")
