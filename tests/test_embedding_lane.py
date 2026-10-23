"""Contrato 2.12: embeddings en lote y carril propio para embeddings."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import BrokerConfig, EmbeddingLaneConfig, PersistenceConfig, ProcessingConfig
from app.db import Database
from app.main import create_app
from app.providers.base import ProviderError
from app.providers.ollama import OllamaProvider
from app.providers.routing import RoutedModelProvider
from app.repository import TaskRepository, lane_for_request
from app.schemas import ModelReference, TaskCreateRequest, TaskKind
from app.task_classifier import classify_task_type


def batch_payload(key: str, inputs: list[str], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "idempotency_key": key,
        "inference_kind": "embedding",
        "content": {"inputs": inputs},
        # 2.12: sin json_schema. Antes había que mandar uno que no servía de nada.
        "output": {"format": "json"},
        "execution": {"strategy": "single"},
    }
    payload.update(overrides)
    return payload


def chat_payload(key: str, priority: int = 100) -> dict[str, Any]:
    return {"idempotency_key": key, "content": {"prompt": "hola"}, "priority": priority}


def make_client(tmp_path: Path, **lane: Any) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(
            auto_dispatch=False, provider_mode="bootstrap",
            embedding_lane=EmbeddingLaneConfig(**lane),
        ),
    )
    return TestClient(create_app(config))


def make_repository(tmp_path: Path) -> TaskRepository:
    db = Database(str(tmp_path / "broker.db"))
    db.init_schema()
    return TaskRepository(db)


# --- contrato -----------------------------------------------------------------


def test_inputs_and_prompt_together_are_rejected(tmp_path: Path) -> None:
    payload = batch_payload("e:both", ["uno"])
    payload["content"]["prompt"] = "dos"
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json=payload)
    assert response.status_code == 422
    assert "mutually exclusive" in json.dumps(response.json())


def test_inputs_only_with_embedding(tmp_path: Path) -> None:
    payload = batch_payload("e:chat", ["uno"], inference_kind="chat", output={"format": "markdown"})
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json=payload)
    assert response.status_code == 422


def test_batch_larger_than_max_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path, batch_max=3) as client:
        response = client.post("/api/v1/tasks", json=batch_payload("e:big", ["a", "b", "c", "d"]))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "EMBEDDING_BATCH_TOO_LARGE"
    assert response.json()["detail"]["batch_max"] == 3


def test_embedding_json_needs_no_schema_but_chat_json_still_does() -> None:
    TaskCreateRequest.model_validate({
        "idempotency_key": "e:single", "inference_kind": "embedding",
        "content": {"prompt": "texto"}, "output": {"format": "json"},
    })
    with pytest.raises(ValueError, match="json_schema is required"):
        TaskCreateRequest.model_validate({
            "idempotency_key": "c:json", "content": {"prompt": "texto"}, "output": {"format": "json"},
        })
    with pytest.raises(ValueError, match="content.prompt is required"):
        TaskCreateRequest.model_validate({"idempotency_key": "c:empty", "content": {}})


def test_capabilities_announce_batch_and_lane(tmp_path: Path) -> None:
    with make_client(tmp_path, max_parallel=2, batch_max=32) as client:
        body = client.get("/api/v1/capabilities").json()
    assert body["contract_version"] == "2.12"
    assert body["embedding_batch_max"] == 32
    assert body["embedding_lane"] == {"max_parallel": 2, "batch_max": 32}
    assert "embedding" in body["work_lanes"]


def test_capabilities_without_lane_do_not_announce_it(tmp_path: Path) -> None:
    with make_client(tmp_path, enabled=False) as client:
        body = client.get("/api/v1/capabilities").json()
    assert body["embedding_lane"] is None
    assert "embedding" not in body["work_lanes"]


def test_embeddings_have_their_own_task_type() -> None:
    request = TaskCreateRequest.model_validate(batch_payload("e:type", ["def f(): pass"]))
    assert classify_task_type(request) == "embedding"


# --- carril -------------------------------------------------------------------


def test_embedding_task_goes_to_its_lane_and_does_not_wait_for_inference(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    chat = TaskCreateRequest.model_validate(chat_payload("c:1"))
    embedding = TaskCreateRequest.model_validate(batch_payload("e:1", ["uno"]))
    repository.create_task(chat, queue_max_size=10)
    created, _ = repository.create_task(
        embedding, queue_max_size=10, kind=lane_for_request(embedding, embedding_lane=True),
    )
    assert created.kind == TaskKind.embedding
    # El chat ocupa el workflow único de inferencia...
    assert repository.claim_next_queued_task_id() is not None
    assert repository.claim_next_queued_task_id() is None
    # ...y el embedding arranca igualmente en su carril.
    assert repository.claim_next_embedding_task(1) == created.task_id
    # Con el carril lleno no entra otro.
    other = TaskCreateRequest.model_validate(batch_payload("e:2", ["dos"]))
    repository.create_task(other, queue_max_size=10, kind=TaskKind.embedding)
    assert repository.claim_next_embedding_task(1) is None
    assert repository.get_task_request(created.task_id).content.inputs == ["uno"]


def test_lane_respects_priority(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    for index in range(5):
        bulk = TaskCreateRequest.model_validate(batch_payload(f"e:bulk{index}", ["x"], priority=100))
        repository.create_task(bulk, queue_max_size=100, kind=TaskKind.embedding)
    urgent = TaskCreateRequest.model_validate(batch_payload("e:urgent", ["pregunta"], priority=1))
    created, _ = repository.create_task(urgent, queue_max_size=100, kind=TaskKind.embedding)
    assert repository.claim_next_embedding_task(1) == created.task_id


def test_lane_disabled_keeps_embeddings_in_inference(tmp_path: Path) -> None:
    request = TaskCreateRequest.model_validate(batch_payload("e:off", ["uno"]))
    assert lane_for_request(request, embedding_lane=False) == TaskKind.inference


def test_batch_end_to_end_result_identity_and_artifact(tmp_path: Path) -> None:
    texts = [f"texto {chr(65 + index)} " * (index + 1) for index in range(5)]
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json=batch_payload("e:e2e", texts))
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        assert client.post("/api/v1/dispatcher/embedding/tick").json()["task_id"] == task_id
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        assert task["status"] == "completed", task
        assert task["kind"] == "embedding"
        result = task["result"]
        assert result["embeddings"] == [[float(len(text)), float(ord(text[0])), 1.0] for text in texts]
        assert result["dimensions"] == 3
        identity = result["embedding_model"]
        assert identity["provider"] == "bootstrap" and identity["dimensions"] == 3
        assert {"model", "digest", "deployment"} <= set(identity)
        artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()["items"]
        final = [item for item in artifacts if item["final"]]
        assert [item["artifact_type"] for item in final] == ["embedding_output"]
        body = client.get(final[0]["download_url"]).json()
        assert len(body["embeddings"]) == len(texts)
        row = client.app.state.db.query_one(
            "SELECT output_json, task_type FROM model_invocations WHERE task_id = ?", (task_id,),
        )
        assert json.loads(row["output_json"]) == {"embeddings_count": 5, "dimensions": 3}
        assert row["task_type"] == "embedding"
        lane = client.get("/dashboard/fragments/embedding-lane")
        assert lane.status_code == 200
        assert "Carril de embeddings" in lane.text


def test_generic_tick_still_advances_embeddings(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        task_id = client.post("/api/v1/tasks", json=batch_payload("e:tick", ["uno"])).json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "completed"


# --- proveedor ----------------------------------------------------------------


def ollama_handler(state: dict[str, Any], *, context: int = 8192, max_chars: int | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{
                "name": "embed", "size": 1, "details": {"context_length": context},
                "capabilities": ["embedding"],
            }]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/embed":
            body = json.loads(request.content)
            state.setdefault("calls", []).append(body)
            texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
            if max_chars is not None and any(len(text) > max_chars for text in texts):
                return httpx.Response(400, json={"error": "the input length exceeds the context length"})
            return httpx.Response(200, json={
                "embeddings": [[float(len(text)), 1.0] for text in texts], "prompt_eval_count": 7,
            })
        return httpx.Response(200, json={})
    return handler


def embed_request(inputs: list[str]) -> TaskCreateRequest:
    return TaskCreateRequest.model_validate(batch_payload("e:p", inputs))


def test_ollama_batch_keeps_order_in_one_call() -> None:
    state: dict[str, Any] = {}
    provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(ollama_handler(state)))
    output = asyncio.run(provider.embed_batch(embed_request(["a", "bbb", "cc"]), "embed", ["a", "bbb", "cc"]))
    asyncio.run(provider.close())
    assert output.embeddings == ((1.0, 1.0), (3.0, 1.0), (2.0, 1.0))
    assert state["calls"] == [{"model": "embed", "input": ["a", "bbb", "cc"], "truncate": False, "keep_alive": -1}]


def test_ollama_batch_rejects_too_long_text_by_estimate_with_index() -> None:
    state: dict[str, Any] = {}
    provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(ollama_handler(state, context=10)))
    inputs = ["corto", "x" * 100, "corto"]
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.embed_batch(embed_request(inputs), "embed", inputs))
    asyncio.run(provider.close())
    assert raised.value.code == "EMBEDDING_INPUT_TOO_LONG"
    assert raised.value.details["index"] == 1
    assert "calls" not in state  # ni un vector calculado


def test_ollama_batch_locates_text_the_runtime_refused() -> None:
    state: dict[str, Any] = {}
    handler = ollama_handler(state, max_chars=50)
    provider = OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))
    inputs = ["uno", "dos", "y" * 60, "cuatro"]
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.embed_batch(embed_request(inputs), "embed", inputs))
    asyncio.run(provider.close())
    assert raised.value.code == "EMBEDDING_INPUT_TOO_LONG"
    assert raised.value.details["index"] == 2


def test_embedding_does_not_wait_for_the_serial_chat_slot() -> None:
    state: dict[str, Any] = {}
    config = BrokerConfig()
    ollama = OllamaProvider(config, transport=httpx.MockTransport(ollama_handler(state)))
    router = RoutedModelProvider(config, ollama=ollama)
    model = ModelReference(provider="ollama", deployment="local", model="embed")

    async def scenario() -> Any:
        # Un chat larguísimo tiene tomado el slot serie de inferencia.
        async with router._serial_inference_slot:
            return await asyncio.wait_for(router.propose(embed_request(["hola"]), model, 1), timeout=2)

    output = asyncio.run(scenario())
    asyncio.run(router.close())
    assert output.embeddings == ((4.0, 1.0),)


def test_unreachable_catalog_is_remembered_for_a_while() -> None:
    from app.config import OpenAICompatibleProviderConfig
    from app.providers.openai_compatible import OpenAICompatibleProvider

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("All connection attempts failed")

    config = OpenAICompatibleProviderConfig(
        id="caido", base_url="http://127.0.0.1:9/v1", enabled=True, sync_models=True, api_key_env=None,
    )
    provider = OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))
    for _ in range(3):
        with pytest.raises(ProviderError) as raised:
            asyncio.run(provider.models())
        assert raised.value.code == "PROVIDER_UNAVAILABLE"
    asyncio.run(provider.close())
    # Un solo intento de red: los demás reciben el fallo recordado al instante.
    assert calls["count"] == 1
