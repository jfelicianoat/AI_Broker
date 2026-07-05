from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.providers import ModelOutput, OllamaProvider, ProviderError, RoutedModelProvider
from app.schemas import ModelReference, TaskCreateRequest


def embedding_request(**overrides) -> TaskCreateRequest:
    payload = {
        "idempotency_key": "phase4:embedding",
        "inference_kind": "embedding",
        "content": {"prompt": "Texto exacto para vectorizar"},
        "output": {
            "format": "json",
            "json_schema": {
                "type": "object",
                "required": ["embedding"],
                "properties": {"embedding": {"type": "array", "items": {"type": "number"}}},
            },
        },
        "execution": {"strategy": "single"},
    }
    payload.update(overrides)
    return TaskCreateRequest.model_validate(payload)


class PhaseFourContractTests(unittest.TestCase):
    def test_embedding_requires_single_json_and_attachments_are_rejected(self) -> None:
        request = embedding_request()
        self.assertEqual(request.inference_kind.value, "embedding")

        with self.assertRaisesRegex(Exception, "embedding only supports single"):
            embedding_request(execution={"strategy": "mixture_of_agents"})
        with self.assertRaisesRegex(Exception, "attachments are not supported"):
            TaskCreateRequest.model_validate({
                "idempotency_key": "phase4:attachment",
                "content": {"prompt": "hola", "attachments": [{"type": "text", "content": "dato"}]},
            })


class PhaseFourOllamaTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_mapping_preserves_prompt_and_json_schema_without_interpreting_output(self) -> None:
        state = {"loaded": False, "chat_body": None}
        schema = {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{
                    "name": "chat", "size": 1, "details": {"context_length": 32768},
                    "capabilities": ["completion"],
                }]})
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"name": "chat", "size_vram": 1}] if state["loaded"] else []})
            if request.url.path == "/api/chat":
                state["chat_body"] = json.loads(request.content)
                state["loaded"] = True
                return httpx.Response(200, json={
                    "message": {"content": '{"value":"literal"}'},
                    "prompt_eval_count": 4,
                    "eval_count": 3,
                })
            if request.url.path == "/api/generate":
                state["loaded"] = False
                return httpx.Response(200, json={})
            return httpx.Response(404)

        provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
        prompt = "No cambies <este> contenido ni su puntuación."
        request = TaskCreateRequest.model_validate({
            "idempotency_key": "phase4:json",
            "content": {"prompt": prompt},
            "output": {"format": "json", "json_schema": schema},
            "generation": {"temperature": 0.1, "max_output_tokens": 100},
        })
        output = await provider.generate(request, "chat", prompt)
        await provider.close()

        self.assertEqual(state["chat_body"]["messages"], [{"role": "user", "content": prompt}])
        self.assertEqual(state["chat_body"]["format"], schema)
        self.assertEqual(output.content, '{"value":"literal"}')

    async def test_context_limit_fails_before_provider_inference_without_truncation(self) -> None:
        chat_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_called
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{
                    "name": "tiny", "size": 1, "details": {"context_length": 10},
                    "capabilities": ["completion"],
                }]})
            if request.url.path == "/api/chat":
                chat_called = True
            return httpx.Response(200, json={"models": []})

        provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
        request = TaskCreateRequest.model_validate({
            "idempotency_key": "phase4:context",
            "content": {"prompt": "contenido que no cabe"},
            "generation": {"max_output_tokens": 20},
        })
        with self.assertRaises(ProviderError) as raised:
            await provider.generate(request, "tiny", request.content.prompt)
        await provider.close()
        self.assertEqual(raised.exception.code, "CONTEXT_LIMIT_EXCEEDED")
        self.assertEqual(raised.exception.details["reason"], "prompt_context_exceeded")
        self.assertEqual(raised.exception.details["context_window"], 10)
        self.assertEqual(raised.exception.details["max_output_tokens_allowed"], 0)
        self.assertFalse(chat_called)

    async def test_embedding_uses_embed_endpoint_and_returns_one_numeric_vector(self) -> None:
        state = {"loaded": False, "body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{
                    "name": "embed", "size": 1, "details": {"context_length": 8192},
                    "capabilities": ["embedding"],
                }]})
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"name": "embed", "size_vram": 1}] if state["loaded"] else []})
            if request.url.path == "/api/embed":
                state["body"] = json.loads(request.content)
                state["loaded"] = True
                return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]], "prompt_eval_count": 5})
            if request.url.path == "/api/generate":
                state["loaded"] = False
                return httpx.Response(200, json={})
            return httpx.Response(404)

        provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
        request = embedding_request()
        output = await provider.embed(request, "embed", request.content.prompt)
        await provider.close()
        self.assertEqual(state["body"], {
            "model": "embed", "input": request.content.prompt, "truncate": False, "keep_alive": -1,
        })
        self.assertEqual(output.embedding, (0.1, 0.2, 0.3))
        self.assertEqual(output.tokens_output, 0)


class PhaseFourRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_fallback_is_explicit_and_can_be_disabled(self) -> None:
        class Stub:
            async def models(self):
                return [
                    {"name": "preferred", "provider": "ollama", "deployment": "local", "context_window": 10,
                     "capabilities": ["completion"]},
                    {"name": "fallback", "provider": "ollama", "deployment": "local", "context_window": 10000,
                     "capabilities": ["completion"]},
                ]

            async def close(self):
                return None

        router = RoutedModelProvider(BrokerConfig(), ollama=Stub(), deepseek=Stub())
        request = TaskCreateRequest.model_validate({
            "idempotency_key": "phase4:fallback",
            "content": {"prompt": "contenido"},
            "generation": {"max_output_tokens": 20},
            "model_requirements": {"preferred_model": "preferred", "fallback_allowed": True},
        })
        selected = await router.select(request, 1, ["single"])
        self.assertEqual(selected[0].model, "fallback")
        strict = request.model_copy(update={
            "model_requirements": request.model_requirements.model_copy(update={"fallback_allowed": False}),
        })
        with self.assertRaises(ProviderError) as raised:
            await router.select(strict, 1, ["single"])
        self.assertEqual(raised.exception.code, "CONTEXT_LIMIT_EXCEEDED")
        await router.close()


class PhaseFourPersistenceTests(unittest.TestCase):
    def test_embedding_result_and_invocation_are_persisted_before_terminal_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = BrokerConfig(
                persistence=PersistenceConfig(database=str(Path(temporary) / "broker.db")),
                processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
            )
            with TestClient(create_app(config)) as client:
                response = client.post("/api/v1/tasks", json=embedding_request().model_dump(mode="json"))
                self.assertEqual(response.status_code, 202)
                task_id = response.json()["task_id"]
                client.post("/api/v1/dispatcher/tick")
                task = client.get(f"/api/v1/tasks/{task_id}").json()
                self.assertEqual(task["status"], "completed")
                self.assertEqual(task["result"]["embedding"], [0.25, 0.5, 0.75])
                self.assertEqual(task["result"]["inference_kind"], "embedding")
                row = client.app.state.db.query_one(
                    "SELECT output_json FROM model_invocations WHERE task_id = ?", (task_id,)
                )
                self.assertEqual(json.loads(row["output_json"])["embedding"], [0.25, 0.5, 0.75])

    def test_json_chat_result_remains_an_opaque_string(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = BrokerConfig(
                persistence=PersistenceConfig(database=str(Path(temporary) / "broker.db")),
                processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
            )
            app = create_app(config)

            class LiteralProvider:
                async def select(self, request, count, roles):
                    return [ModelReference(provider="ollama", deployment="test", model="literal", role=roles[0])]

                async def propose(self, request, model, ordinal):
                    return ModelOutput('{"claim":"do not parse"}', 2, 4, 0.0, 1.0)

            app.state.coordinator.provider = LiteralProvider()
            with TestClient(app) as client:
                created = client.post("/api/v1/tasks", json={
                    "idempotency_key": "phase4:opaque",
                    "content": {"prompt": "devuelve JSON"},
                    "output": {"format": "json", "json_schema": {"type": "object"}},
                    "model_requirements": {"preferred_model": "wanted", "fallback_allowed": True},
                }).json()
                client.post("/api/v1/dispatcher/tick")
                result = client.get(created["status_url"]).json()["result"]
                self.assertEqual(result["assistant_content"], '{"claim":"do not parse"}')
                self.assertIsInstance(result["assistant_content"], str)
                self.assertTrue(result["fallback_used"])


if __name__ == "__main__":
    unittest.main()
