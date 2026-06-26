import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.config import (
    BrokerConfig,
    DeepSeekConfig,
    OllamaConfig,
    OpenAICompatibleModelConfig,
    OpenAICompatibleProviderConfig,
    ProcessingConfig,
    ProvidersConfig,
)
from app.providers import DeepSeekProvider, OllamaProvider, OpenAICompatibleProvider, ProviderError, RoutedModelProvider
from app.schemas import ModelReference, TaskCreateRequest


class OllamaProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_generates_and_unloads_model(self) -> None:
        state = {"loaded": False, "unloads": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(
                    200,
                    json={"models": [{"name": "qwen:latest", "size": 1024, "details": {"family": "qwen"}}]},
                )
            if request.url.path == "/api/show":
                return httpx.Response(
                    200,
                    json={"model_info": {"qwen.context_length": 32768}, "capabilities": ["completion"]},
                )
            if request.url.path == "/api/ps":
                models = [{"name": "qwen:latest", "size_vram": 1024}] if state["loaded"] else []
                return httpx.Response(200, json={"models": models})
            if request.url.path == "/api/chat":
                body = json.loads(request.content)
                self.assertEqual(body["keep_alive"], -1)
                state["loaded"] = True
                return httpx.Response(
                    200,
                    json={"message": {"content": "respuesta"}, "prompt_eval_count": 3, "eval_count": 2},
                )
            if request.url.path == "/api/generate":
                self.assertEqual(json.loads(request.content)["keep_alive"], 0)
                state["loaded"] = False
                state["unloads"] += 1
                return httpx.Response(200, json={})
            return httpx.Response(404)

        config = BrokerConfig(
            processing=ProcessingConfig(provider_mode="real"),
            providers=ProvidersConfig(ollama=OllamaConfig(), deepseek=DeepSeekConfig(enabled=False)),
        )
        provider = OllamaProvider(config, transport=httpx.MockTransport(handler))
        request = TaskCreateRequest(idempotency_key="ollama:test", content={"prompt": "hola"})
        models = await provider.models()
        output = await provider.generate(request, "qwen:latest", "hola")
        await provider.close()

        self.assertEqual(models[0]["context_window"], 32768)
        self.assertEqual(output.content, "respuesta")
        self.assertEqual(state["unloads"], 1)

    async def test_rejects_unknown_model(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": []})
            return httpx.Response(200, json={})

        provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
        request = TaskCreateRequest(idempotency_key="ollama:missing", content={"prompt": "hola"})
        with self.assertRaisesRegex(ProviderError, "no disponible") as raised:
            await provider.generate(request, "missing", "hola")
        self.assertEqual(raised.exception.code, "MODEL_UNAVAILABLE")
        await provider.close()

    async def test_cancellation_still_unloads_model(self) -> None:
        state = {"loaded": False, "unloaded": False}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "small", "size": 1}]})
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"model_info": {"small.context_length": 8192}})
            if request.url.path == "/api/ps":
                return httpx.Response(
                    200,
                    json={"models": [{"name": "small", "size_vram": 1}] if state["loaded"] else []},
                )
            if request.url.path == "/api/chat":
                state["loaded"] = True
                await asyncio.sleep(10)
            if request.url.path == "/api/generate":
                state["loaded"] = False
                state["unloaded"] = True
                return httpx.Response(200, json={})
            return httpx.Response(500)

        provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
        request = TaskCreateRequest(idempotency_key="ollama:cancel", content={"prompt": "hola"})
        task = asyncio.create_task(provider.generate(request, "small", "hola"))
        while not state["loaded"]:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(state["unloaded"])
        await provider.close()


class DeepSeekProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_environment_credential_and_calculates_cost(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer secret")
            if request.url.path == "/models":
                return httpx.Response(200, json={"data": [{"id": "deepseek-chat"}]})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "cloud"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        config = DeepSeekConfig(
            enabled=True,
            input_cost_per_million=1,
            output_cost_per_million=2,
        )
        provider = DeepSeekProvider(config, transport=httpx.MockTransport(handler))
        request = TaskCreateRequest(
            idempotency_key="deepseek:test",
            content={"prompt": "hola"},
            model_requirements={"cloud_allowed": True, "allowed_providers": ["deepseek"]},
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret"}):
            models = await provider.models()
            output = await provider.generate(request, "deepseek-chat", "hola")
        await provider.close()

        self.assertEqual(models[0]["name"], "deepseek-chat")
        self.assertEqual(output.cost_usd, 0.00002)

    async def test_rejects_request_before_call_when_budget_is_insufficient(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(500)

        config = DeepSeekConfig(enabled=True, output_cost_per_million=10)
        provider = DeepSeekProvider(config, transport=httpx.MockTransport(handler))
        request = TaskCreateRequest(
            idempotency_key="deepseek:budget",
            content={"prompt": "hola"},
            generation={"max_output_tokens": 1000},
            model_requirements={
                "cloud_allowed": True,
                "allowed_providers": ["deepseek"],
                "max_cost_usd": 0.001,
            },
        )
        with self.assertRaises(ProviderError) as raised:
            await provider.generate(request, "deepseek-chat", "hola")
        self.assertEqual(raised.exception.code, "BUDGET_EXCEEDED")
        self.assertFalse(called)
        await provider.close()


class OpenAICompatibleProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_environment_credential_and_configured_models(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer nvidia-secret")
            self.assertEqual(request.url.path, "/v1/chat/completions")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "meta/llama-3.1-70b-instruct")
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "nvidia"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        config = OpenAICompatibleProviderConfig(
            id="nvidia",
            enabled=True,
            display_name="NVIDIA NIM",
            base_url="https://integrate.api.nvidia.com/v1",
            api_key_env="NVIDIA_API_KEY",
            models=[
                OpenAICompatibleModelConfig(
                    name="meta/llama-3.1-70b-instruct",
                    context_window=128000,
                    input_cost_per_million=1,
                    output_cost_per_million=2,
                )
            ],
        )
        provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
        request = TaskCreateRequest(
            idempotency_key="nvidia:test",
            content={"prompt": "hola"},
            model_requirements={"cloud_allowed": True, "allowed_providers": ["nvidia"]},
        )
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvidia-secret"}):
            models = await provider.models()
            output = await provider.generate(request, "meta/llama-3.1-70b-instruct", "hola")
        await provider.close()

        self.assertEqual(models[0]["provider"], "nvidia")
        self.assertEqual(models[0]["deployment"], "cloud")
        self.assertEqual(output.content, "nvidia")
        self.assertEqual(output.cost_usd, 0.00002)

    async def test_includes_provider_error_body_on_http_errors(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"message": "max_tokens is too large for this model"}},
            )

        config = OpenAICompatibleProviderConfig(
            id="nvidia",
            enabled=True,
            base_url="https://integrate.api.nvidia.com/v1",
            models=[OpenAICompatibleModelConfig(name="microsoft/phi-4-multimodal-instruct")],
        )
        provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
        request = TaskCreateRequest(
            idempotency_key="nvidia:error-body",
            content={"prompt": "hola"},
            model_requirements={"cloud_allowed": True, "allowed_providers": ["nvidia"]},
        )
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvidia-secret"}):
            with self.assertRaises(ProviderError) as raised:
                await provider.generate(request, "microsoft/phi-4-multimodal-instruct", "hola")
        await provider.close()

        self.assertEqual(raised.exception.code, "MODEL_ERROR")
        self.assertIn("HTTP 400", str(raised.exception))
        self.assertIn("max_tokens is too large", str(raised.exception))

    async def test_probe_all_models_marks_chat_compatibility(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "ok"}, {"id": "bad"}]})
            body = json.loads(request.content)
            if body["model"] == "ok":
                return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
            return httpx.Response(400, json={"error": {"message": "unsupported endpoint"}})

        config = OpenAICompatibleProviderConfig(
            id="nvidia",
            enabled=True,
            base_url="https://integrate.api.nvidia.com/v1",
            sync_models=True,
        )
        provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvidia-secret"}):
            results = await provider.probe_all_models()
        await provider.close()

        by_name = {item["name"]: item for item in results}
        self.assertEqual(by_name["ok"]["compatibility"], "compatible")
        self.assertEqual(by_name["bad"]["compatibility"], "incompatible")
        self.assertIn("unsupported endpoint", by_name["bad"]["compatibility_error"])

    async def test_probe_all_models_can_skip_already_compatible(self) -> None:
        called_models = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/chat/completions":
                body = json.loads(request.content)
                called_models.append(body["model"])
                return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
            return httpx.Response(500)

        config = OpenAICompatibleProviderConfig(
            id="nvidia",
            enabled=True,
            base_url="https://integrate.api.nvidia.com/v1",
            probe_delay_seconds=0,
            models=[
                OpenAICompatibleModelConfig(name="already-ok", compatibility="compatible"),
                OpenAICompatibleModelConfig(name="pending", compatibility="unknown"),
            ],
        )
        provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvidia-secret"}):
            results = await provider.probe_all_models()
        await provider.close()

        self.assertEqual(called_models, ["pending"])
        self.assertEqual([item["name"] for item in results], ["pending"])


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_honours_preferred_model_and_fallback_policy(self) -> None:
        class StubProvider:
            def __init__(self, models):
                self._models = models

            async def models(self):
                return self._models

            async def close(self):
                return None

        ollama = StubProvider(
            [
                {"name": "fallback", "provider": "ollama", "deployment": "local", "context_window": 100000},
                {"name": "preferred", "provider": "ollama", "deployment": "local", "context_window": 100000},
            ]
        )
        router = RoutedModelProvider(BrokerConfig(), ollama=ollama, deepseek=StubProvider([]))
        request = TaskCreateRequest(
            idempotency_key="route:preferred",
            content={"prompt": "hola"},
            model_requirements={"preferred_model": "preferred", "fallback_allowed": False},
        )
        selected = await router.select(request, 1, ["single"])
        self.assertEqual(selected[0].model, "preferred")

        missing = request.model_copy(
            update={
                "model_requirements": request.model_requirements.model_copy(
                    update={"preferred_model": "missing", "fallback_allowed": False}
                )
            }
        )
        with self.assertRaises(ProviderError) as raised:
            await router.select(missing, 1, ["single"])
        self.assertEqual(raised.exception.code, "MODEL_UNAVAILABLE")
        await router.close()

    async def test_exact_target_matches_provider_deployment_and_model(self) -> None:
        class StubProvider:
            def __init__(self, models):
                self._models = models

            async def models(self):
                return self._models

            async def close(self):
                return None

        ollama = StubProvider([
            {"name": "shared", "provider": "ollama", "deployment": "gpu-a", "context_window": 100000},
            {"name": "shared", "provider": "ollama", "deployment": "gpu-b", "context_window": 100000},
        ])
        router = RoutedModelProvider(BrokerConfig(), ollama=ollama, deepseek=StubProvider([]))
        request = TaskCreateRequest.model_validate({
            "idempotency_key": "route:exact",
            "content": {"prompt": "hola"},
            "model_requirements": {
                "target_model": {"provider": "ollama", "deployment": "gpu-b", "model": "shared"},
                "fallback_allowed": False,
            },
        })

        selected = await router.select(request, 1, ["single"])
        self.assertEqual(
            (selected[0].provider, selected[0].deployment, selected[0].model),
            ("ollama", "gpu-b", "shared"),
        )

        wrong = request.model_copy(update={
            "model_requirements": request.model_requirements.model_copy(update={
                "target_model": ModelReference(provider="ollama", deployment="gpu-c", model="shared"),
            }),
        })
        with self.assertRaises(ProviderError) as raised:
            await router.select(wrong, 1, ["single"])
        self.assertEqual(raised.exception.code, "MODEL_UNAVAILABLE")
        await router.close()

    async def test_serializes_all_llm_calls_globally(self) -> None:
        class StubProvider:
            def __init__(self) -> None:
                self.active = 0
                self.peak = 0

            async def models(self):
                return [{"name": "model", "provider": "ollama", "deployment": "local", "context_window": 100000}]

            async def generate(self, request, model, prompt):
                from app.providers import ModelOutput

                self.active += 1
                self.peak = max(self.peak, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                return ModelOutput("ok", 1, 1, 0.0, 1.0)

            async def close(self):
                return None

        ollama = StubProvider()
        router = RoutedModelProvider(BrokerConfig(), ollama=ollama, deepseek=StubProvider())
        request = TaskCreateRequest(idempotency_key="route:serial", content={"prompt": "hola"})
        model = (await router.select(request, 1, ["single"]))[0]
        await asyncio.gather(
            router.propose(request, model, 1),
            router.propose(request, model, 2),
        )
        self.assertEqual(ollama.peak, 1)

        slow_request = TaskCreateRequest.model_validate({
            "idempotency_key": "route:parallel",
            "content": {"prompt": "hola"},
            "execution": {"strategy": "mixture_of_agents", "preset": "slow"},
        })
        await asyncio.gather(
            router.propose(slow_request, model, 1),
            router.propose(slow_request, model, 2),
        )
        self.assertEqual(ollama.peak, 2)
        await router.close()

    async def test_excludes_ollama_cloud_tags_when_cloud_is_not_allowed(self) -> None:
        class StubProvider:
            async def models(self):
                return [
                    {"name": "remote:cloud", "provider": "ollama", "deployment": "cloud", "context_window": 100000},
                    {"name": "local", "provider": "ollama", "deployment": "local", "context_window": 100000},
                ]

            async def close(self):
                return None

        router = RoutedModelProvider(BrokerConfig(), ollama=StubProvider(), deepseek=StubProvider())
        request = TaskCreateRequest(idempotency_key="route:local", content={"prompt": "hola"})
        selected = await router.select(request, 1, ["single"])
        self.assertEqual(selected[0].model, "local")
        await router.close()

    async def test_routes_custom_openai_compatible_provider(self) -> None:
        class CustomStub:
            async def models(self):
                return [
                    {
                        "name": "nim",
                        "provider": "nvidia",
                        "deployment": "cloud",
                        "context_window": 100000,
                        "capabilities": ["completion"],
                    }
                ]

            async def generate(self, request, model, prompt):
                from app.providers import ModelOutput

                return ModelOutput("custom", 1, 1, 0.0, 1.0)

            async def close(self):
                return None

        router = RoutedModelProvider(
            BrokerConfig(providers=ProvidersConfig(ollama=OllamaConfig(enabled=False))),
            ollama=CustomStub(),
            deepseek=CustomStub(),
            custom={"nvidia": CustomStub()},
        )
        request = TaskCreateRequest(
            idempotency_key="route:custom",
            content={"prompt": "hola"},
            model_requirements={"cloud_allowed": True, "allowed_providers": ["nvidia"]},
        )
        selected = await router.select(request, 1, ["single"])
        output = await router.propose(request, selected[0], 1)
        await router.close()

        self.assertEqual(selected[0].provider, "nvidia")
        self.assertEqual(output.content, "custom")

    async def test_auto_selection_excludes_incompatible_custom_models(self) -> None:
        class CustomStub:
            async def models(self):
                return [
                    {
                        "name": "bad",
                        "provider": "nvidia",
                        "deployment": "api",
                        "context_window": 100000,
                        "capabilities": ["completion"],
                        "compatibility": "incompatible",
                    },
                    {
                        "name": "ok",
                        "provider": "nvidia",
                        "deployment": "api",
                        "context_window": 100000,
                        "capabilities": ["completion"],
                        "compatibility": "compatible",
                    },
                ]

            async def close(self):
                return None

        router = RoutedModelProvider(
            BrokerConfig(providers=ProvidersConfig(ollama=OllamaConfig(enabled=False))),
            ollama=CustomStub(),
            deepseek=CustomStub(),
            custom={"nvidia": CustomStub()},
        )
        request = TaskCreateRequest(
            idempotency_key="route:compatible-only",
            content={"prompt": "hola"},
            model_requirements={"cloud_allowed": True, "allowed_providers": ["nvidia"]},
        )
        selected = await router.select(request, 1, ["single"])
        await router.close()

        self.assertEqual(selected[0].model, "ok")


if __name__ == "__main__":
    unittest.main()
