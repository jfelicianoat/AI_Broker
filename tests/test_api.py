import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard_web as dashboard_web
import app.main as main_module
import app.providers as providers_module
from app.config import (
    BrokerConfig,
    LoggingConfig,
    OllamaConfig,
    OpenAICompatibleModelConfig,
    OpenAICompatibleProviderConfig,
    PersistenceConfig,
    ProcessingConfig,
    ProvidersConfig,
    ServerConfig,
    load_config,
)
from app.dashboard_web import load_dashboard_resources
from app.main import create_app
from app.maintenance import create_state_backup, restore_state_backup, verify_state_backup
from app.providers import BootstrapModelProvider, ModelOutput, ProviderError
from app.resource_scheduler import ResourceScheduler


class ConcurrencyProbeProvider(BootstrapModelProvider):
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def propose(self, request, model, ordinal):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            return await super().propose(request, model, ordinal)
        finally:
            self.active -= 1


class TimeoutProbeProvider(BootstrapModelProvider):
    def __init__(self) -> None:
        self.cancelled = False

    async def propose(self, request, model, ordinal):
        try:
            await asyncio.sleep(5)
            return await super().propose(request, model, ordinal)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class TimelineProbeProvider(BootstrapModelProvider):
    async def propose(self, request, model, ordinal):
        await asyncio.sleep(0.05)
        return ModelOutput(
            content=f"Propuesta {ordinal}",
            tokens_input=10,
            tokens_output=20,
            cost_usd=0.0,
            latency_ms=50.0,
        )

    async def synthesize(self, request, model, proposals):
        await asyncio.sleep(0.01)
        return ModelOutput(
            content="Sintesis final",
            tokens_input=15,
            tokens_output=25,
            cost_usd=0.0,
            latency_ms=10.0,
        )


class RetryableFirstProposerProvider(TimelineProbeProvider):
    async def propose(self, request, model, ordinal):
        if model.role == "generalist":
            raise ProviderError("MODEL_ERROR", "fallo retryable", retryable=True)
        return await super().propose(request, model, ordinal)


class FailingSynthesisProvider(TimelineProbeProvider):
    async def synthesize(self, request, model, proposals):
        raise ProviderError("MODEL_ERROR", "fallo controlado", retryable=True)


class PromptTooLargeProvider(BootstrapModelProvider):
    async def propose(self, request, model, ordinal):
        raise ProviderError(
            "CONTEXT_LIMIT_EXCEEDED",
            "El prompt ya supera la ventana del modelo",
            details={
                "reason": "prompt_context_exceeded",
                "prompt_tokens_estimate": 1200,
                "context_window": 1000,
                "max_output_tokens_requested": 4000,
                "max_output_tokens_allowed": 0,
            },
        )


class UnavailableResourceProvider(BootstrapModelProvider):
    async def resource_snapshot(self):
        raise ProviderError("PROVIDER_UNAVAILABLE", "offline", retryable=True)


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


def dashboard_csrf(client: TestClient, path: str = "/dashboard") -> str:
    response = client.get(path)
    assert response.status_code == 200
    token = client.cookies.get("ai_broker_dashboard_csrf")
    assert token
    return token


def test_create_and_read_task(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={"idempotency_key": "test:create", "content": {"prompt": "Hola"}})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["execution_strategy"] == "single"

        task_response = client.get(body["status_url"])
        assert task_response.status_code == 200
        task = task_response.json()
        assert task["task_id"] == body["task_id"]
        assert task["progress"]["phase"] == "queued"


def test_invalid_contract_has_structured_error(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/tasks", json={"idempotency_key": "test:invalid", "content": {"prompt": ""}})

        assert response.status_code == 422
        assert response.json()["code"] == "CONTRACT_VALIDATION_FAILED"


def test_cancel_is_idempotent(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={"idempotency_key": "test:cancel", "content": {"prompt": "Hola"}}).json()

        first = client.delete(created["cancel_url"])
        second = client.delete(created["cancel_url"])

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["status"] == "cancelled"
        assert second.json()["status"] == "cancelled"


def test_health_ready(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json()["dependencies"]["sqlite"]["status"] == "healthy"


def test_models_endpoint_is_available(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/models")

        assert response.status_code == 200
        assert response.json()["models"][0]["deployment"] == "bootstrap"


def test_model_availability_endpoint_marks_dispatchable_models(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/models/availability")
        filtered = client.get("/api/v1/models/availability?only_dispatchable=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["online"] == 1
    assert payload["counts"]["dispatchable"] == 1
    assert payload["items"][0]["availability"] == "online"
    assert payload["items"][0]["dispatchable"] is True
    assert payload["items"][0]["provider_status"] == "healthy"
    assert payload["items"][0]["model"] == "bootstrap-single"
    assert filtered.json()["items"][0]["model"] == "bootstrap-single"


def test_model_availability_endpoint_marks_incompatible_and_unknown_models(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-model-availability-custom.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="real"),
        providers=ProvidersConfig(
            custom=[
                OpenAICompatibleProviderConfig(
                    id="nvidia",
                    enabled=True,
                    base_url="https://integrate.api.nvidia.com/v1",
                    models=[
                        OpenAICompatibleModelConfig(
                            name="chat-ok",
                            context_window=128000,
                            compatibility="compatible",
                        ),
                        OpenAICompatibleModelConfig(
                            name="vision-only",
                            context_window=128000,
                            compatibility="incompatible",
                            compatibility_error="No compatible con chat completions",
                        ),
                        OpenAICompatibleModelConfig(
                            name="pending",
                            context_window=128000,
                            compatibility="unknown",
                        ),
                    ],
                )
            ]
        ),
    )
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/models/availability?provider=nvidia")
        dispatchable = client.get("/api/v1/models/availability?provider=nvidia&only_dispatchable=true")

    assert response.status_code == 200
    by_model = {item["model"]: item for item in response.json()["items"]}
    assert by_model["chat-ok"]["availability"] == "online"
    assert by_model["chat-ok"]["dispatchable"] is True
    assert by_model["vision-only"]["availability"] == "incompatible"
    assert by_model["vision-only"]["dispatchable"] is False
    assert by_model["pending"]["availability"] == "unknown"
    assert by_model["pending"]["dispatchable"] is False
    assert [item["model"] for item in dispatchable.json()["items"]] == ["chat-ok"]


def test_model_availability_endpoint_marks_embedding_models_dispatchable(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-model-availability-embedding.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="real"),
        providers=ProvidersConfig(
            ollama=OllamaConfig(enabled=False),
            custom=[
                OpenAICompatibleProviderConfig(
                    id="nvidia",
                    enabled=True,
                    base_url="https://integrate.api.nvidia.com/v1",
                    models=[
                        OpenAICompatibleModelConfig(
                            name="nvidia/nv-embedqa-e5-v5",
                            context_window=8192,
                            capabilities=["embedding"],
                            compatibility="compatible",
                        )
                    ],
                )
            ],
        ),
    )
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/models/availability?provider=nvidia&capability=embedding")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["model"] == "nvidia/nv-embedqa-e5-v5"
    assert item["availability"] == "online"
    assert item["dispatchable"] is True


def test_model_context_endpoint_returns_llm_context_window(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get(
            "/api/v1/models/context",
            params={
                "provider": "ollama",
                "deployment": "bootstrap",
                "model": "bootstrap-single",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "ollama"
    assert payload["deployment"] == "bootstrap"
    assert payload["model"] == "bootstrap-single"
    assert payload["context_window"] == 1_000_000
    assert payload["context_window_known"] is True
    assert "completion" in payload["capabilities"]
    assert payload["features"]["modalities"]["text_input"] == "supported"
    assert payload["features"]["modalities"]["embedding_output"] == "supported"
    assert payload["features"]["tools"]["web_search"] == "unknown"
    assert payload["features"]["tools"]["computer_use"] == "unknown"
    assert payload["features"]["understanding"]["coding"] == "unknown"
    assert payload["features"]["generation"]["structured_outputs"] == "unknown"
    assert payload["features"]["operations"]["fine_tuning"] == "unknown"
    assert payload["features"]["deployment"]["local_execution"] == "unsupported"
    assert payload["features"]["deployment"]["offline_capable"] == "unknown"
    assert payload["features"]["broker_support"]["mixture_proposer"] == "supported"
    assert payload["feature_notes"]


def test_model_context_endpoint_infers_multimodal_features(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-model-context-custom.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="real"),
        providers=ProvidersConfig(
            custom=[
                OpenAICompatibleProviderConfig(
                    id="nvidia",
                    enabled=True,
                    base_url="https://integrate.api.nvidia.com/v1",
                    models=[
                        OpenAICompatibleModelConfig(
                            name="microsoft/phi-4-multimodal-instruct",
                            context_window=262144,
                            compatibility="compatible",
                        )
                    ],
                )
            ]
        ),
    )
    with TestClient(create_app(config)) as client:
        response = client.get(
            "/api/v1/models/context",
            params={
                "provider": "nvidia",
                "deployment": "cloud",
                "model": "microsoft/phi-4-multimodal-instruct",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["context_window"] == 262144
    assert payload["features"]["modalities"]["image_input"] == "supported"
    assert payload["features"]["modalities"]["multimodal_input"] == "supported"
    assert payload["features"]["understanding"]["ocr"] == "supported"
    assert payload["features"]["understanding"]["chart_understanding"] == "supported"
    assert payload["features"]["reasoning"]["mixture_compatible"] == "supported"
    assert payload["features"]["tools"]["deep_research"] == "unknown"
    assert payload["features"]["deployment"]["cloud_execution"] == "supported"
    assert any("image_input inferido" in item for item in payload["feature_notes"])


def test_model_context_endpoint_returns_404_for_unknown_model(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get(
            "/api/v1/models/context",
            params={
                "provider": "ollama",
                "deployment": "bootstrap",
                "model": "missing",
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "MODEL_NOT_FOUND"


def test_capabilities_publish_slow_and_runtime_limits(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-capabilities.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            max_parallel_invocations=2,
        ),
    )
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "2.1"
    assert body["presets"]["mixture_of_agents"] == ["fast", "slow"]
    assert body["scheduling_by_preset"]["fast"] == ["sequential"]
    assert "parallel" in body["scheduling_by_preset"]["slow"]
    assert body["max_active_workflows"] == 1
    assert body["max_parallel_invocations"] == 2


def test_dashboard_read_models_are_paged_filterable_and_source_backed(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "dashboard:first",
                "content": {
                    "prompt": "Primera tarea",
                    "metadata": {"origin": "prompt_tester"},
                },
            },
        ).json()
        client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "dashboard:second",
                "content": {
                    "prompt": "Segunda tarea",
                    "metadata": {"origin": "orchestrator"},
                },
            },
        )
        client.post("/api/v1/dispatcher/tick")

        summary = client.get("/api/v1/dashboard/summary?window_hours=24")
        page = client.get("/api/v1/dashboard/tasks?page=1&page_size=1")
        filtered = client.get("/api/v1/dashboard/tasks?origin=prompt_tester")
        detail = client.get(f"/api/v1/dashboard/tasks/{first['task_id']}")
        usage = client.get("/api/v1/usage")
        resources = client.get("/api/v1/dashboard/resources")

    assert summary.status_code == 200
    assert summary.json()["queued"] == 1
    assert summary.json()["completed"] == 1
    assert summary.json()["invocations"] == 1
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["page_size"] == 1
    assert page.json()["total_pages"] == 2
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["origin"] == "prompt_tester"
    assert detail.status_code == 200
    assert detail.json()["request"]["content"]["prompt"] == "Primera tarea"
    assert len(detail.json()["invocations"]) == 1
    assert detail.json()["events"]
    assert usage.status_code == 200
    assert usage.json()["providers"]["ollama"]["invocations"] == 1.0
    assert resources.status_code == 200
    assert resources.json()["provider"] == "bootstrap"
    assert resources.json()["used_vram_bytes"] == 0


def test_operational_dashboard_renders_and_queue_actions_work(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "ui:first", "content": {"prompt": "Primera"}},
        ).json()
        second = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "ui:second", "content": {"prompt": "Segunda"}},
        ).json()

        token = dashboard_csrf(client)
        page = client.get("/dashboard")
        fragment = client.get("/dashboard/fragments/queue")
        moved = client.post(
            f"/dashboard/actions/queue/{second['task_id']}/up",
            headers={"X-CSRF-Token": token},
        )
        reordered = client.get("/api/v1/queue").json()["pending"]
        cancelled = client.post(
            f"/dashboard/actions/tasks/{first['task_id']}/cancel",
            headers={"X-CSRF-Token": token},
        )
        css = client.get("/static/dashboard.css")
        script = client.get("/static/dashboard.js")

    assert page.status_code == 200
    assert "Panel operativo" in page.text
    assert "Cola de tareas" in page.text
    assert "Historial" in page.text
    assert "Coste real" in page.text
    assert "data-refresh-pauseable" in page.text
    assert fragment.status_code == 200
    assert first["task_id"] in fragment.text
    assert f"Cancelar tarea {first['task_id']}?" in fragment.text
    assert moved.status_code == 204
    assert moved.headers["hx-trigger"] == "dashboard-refresh"
    assert reordered[0]["task_id"] == second["task_id"]
    assert cancelled.status_code == 204
    assert css.status_code == 200
    assert "--teal" in css.text
    assert script.status_code == 200
    assert "refreshDashboard" in script.text
    assert "refreshPaused" in script.text


def test_dashboard_actions_require_csrf_and_same_origin(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "ui:csrf", "content": {"prompt": "Protegida"}},
        ).json()
        token = dashboard_csrf(client)

        missing = client.post(f"/dashboard/actions/tasks/{created['task_id']}/cancel")
        bad_origin = client.post(
            f"/dashboard/actions/tasks/{created['task_id']}/cancel",
            headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
        )
        ok = client.post(
            f"/dashboard/actions/tasks/{created['task_id']}/cancel",
            headers={"X-CSRF-Token": token, "Origin": "http://testserver"},
        )

    assert missing.status_code == 403
    assert missing.json()["detail"] == "CSRF_VALIDATION_FAILED"
    assert bad_origin.status_code == 403
    assert bad_origin.json()["detail"] == "ORIGIN_VALIDATION_FAILED"
    assert ok.status_code == 204


def test_dashboard_configuration_updates_runtime_and_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "broker_config.yaml"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-config.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config, config_path=config_path)) as client:
        token = dashboard_csrf(client)
        page = client.get("/dashboard")
        response = client.post(
            "/dashboard/actions/config",
            data={
                "csrf_token": token,
                "task_timeout_seconds": "900",
                "max_parallel_invocations": "3",
                "queue_max_size": "250",
                "local_vram_budget_gb": "48",
                "vram_safety_margin_gb": "4",
                "max_loaded_local_models": "auto",
                "allow_execution_waves": "on",
            },
        )

    saved = load_config(config_path)
    assert page.status_code == 200
    assert "Configuracion" in page.text
    assert response.status_code == 200
    assert "Configuracion guardada" in response.text
    assert config.processing.task_timeout_seconds == 900
    assert config.processing.max_parallel_invocations == 3
    assert config.processing.queue_max_size == 250
    assert config.resources.local_vram_budget_gb == 48
    assert config.resources.vram_safety_margin_gb == 4
    assert config.resources.max_loaded_local_models == "auto"
    assert saved.processing.task_timeout_seconds == 900
    assert saved.resources.allow_execution_waves is True


def test_dashboard_configuration_can_be_reviewed_without_saving(tmp_path: Path) -> None:
    config_path = tmp_path / "broker_config.yaml"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-config-review.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config, config_path=config_path)) as client:
        token = dashboard_csrf(client)
        response = client.post(
            "/dashboard/actions/config",
            data={
                "csrf_token": token,
                "config_action": "validate",
                "task_timeout_seconds": "900",
                "max_parallel_invocations": "3",
                "queue_max_size": "250",
                "local_vram_budget_gb": "48",
                "vram_safety_margin_gb": "4",
                "max_loaded_local_models": "auto",
                "allow_execution_waves": "on",
            },
        )

    assert response.status_code == 200
    assert "Revision lista; no se ha guardado nada" in response.text
    assert "Timeout global por tarea" in response.text
    assert config.processing.task_timeout_seconds != 900
    assert not config_path.exists()


def test_dashboard_configuration_adds_custom_api_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "broker_config.yaml"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-custom-provider.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config, config_path=config_path)) as client:
        token = dashboard_csrf(client)
        response = client.post(
            "/dashboard/actions/config",
            data={
                "csrf_token": token,
                "task_timeout_seconds": "900",
                "max_parallel_invocations": "auto",
                "queue_max_size": "250",
                "local_vram_budget_gb": "48",
                "vram_safety_margin_gb": "4",
                "max_loaded_local_models": "auto",
                "allow_execution_waves": "on",
                "custom_provider_1_enabled": "on",
                "custom_provider_1_id": "nvidia",
                "custom_provider_1_display_name": "NVIDIA NIM",
                "custom_provider_1_base_url": "https://integrate.api.nvidia.com/v1",
                "custom_provider_1_api_key_env": "NVIDIA_API_KEY",
                "custom_provider_1_deployment": "cloud",
                "custom_provider_1_auto_start": "on",
                "custom_provider_1_timeout_seconds": "300",
                "custom_provider_1_default_context_window": "128000",
                "custom_provider_1_input_cost_per_million": "0",
                "custom_provider_1_output_cost_per_million": "0",
                "custom_provider_1_models": "meta/llama-3.1-70b-instruct|128000|0|0",
            },
        )

    saved = load_config(config_path)
    assert response.status_code == 200
    assert config.providers.custom[0].id == "nvidia"
    assert config.providers.custom[0].enabled is True
    assert saved.providers.custom[0].base_url == "https://integrate.api.nvidia.com/v1"
    assert saved.providers.custom[0].auto_start is True
    assert saved.providers.custom[0].models[0].name == "meta/llama-3.1-70b-instruct"


def test_lmstudio_auto_start_uses_configured_port(monkeypatch) -> None:
    calls = []

    async def fake_run_process(args, *, timeout_seconds):
        calls.append((args, timeout_seconds))
        if args == ["lms", "server", "status"]:
            return {"returncode": 0, "stdout": "The server is not running.", "stderr": ""}
        return {"returncode": 0, "stdout": "Success! Server is now running on port 1234", "stderr": ""}

    monkeypatch.setattr(main_module, "_run_process", fake_run_process)
    logger = main_module.logging.getLogger("test.lmstudio")
    asyncio.run(main_module._ensure_lmstudio_server("http://127.0.0.1:1234/v1", logger))

    assert calls == [
        (["lms", "server", "status"], 10),
        (["lms", "server", "start", "--port", "1234"], 30),
    ]


def test_dashboard_provider_probe_persists_model_compatibility(tmp_path: Path, monkeypatch) -> None:
    class FakeProbeProvider:
        def __init__(self, config):
            self.config = config

        async def models(self):
            return [
                {
                    "name": "chat-ok",
                    "context_window": 128000,
                    "capabilities": ["completion"],
                    "compatibility": "unknown",
                },
                {
                    "name": "vision-only",
                    "context_window": 128000,
                    "capabilities": ["completion"],
                    "compatibility": "unknown",
                },
                {
                    "name": "not-probed-yet",
                    "context_window": 64000,
                    "capabilities": ["completion"],
                    "compatibility": "unknown",
                },
            ]

        async def probe_all_models(self, progress_callback=None):
            if progress_callback is not None:
                await progress_callback(
                    {
                        "phase": "running",
                        "completed": 1,
                        "total": 2,
                        "current_model": "vision-only",
                        "last_result": {"name": "chat-ok", "compatibility": "compatible"},
                    }
                )
            return [
                {
                    "name": "chat-ok",
                    "compatibility": "compatible",
                    "compatibility_checked_at": "2026-06-26T20:00:00+00:00",
                    "compatibility_error": None,
                },
                {
                    "name": "vision-only",
                    "compatibility": "incompatible",
                    "compatibility_checked_at": "2026-06-26T20:00:01+00:00",
                    "compatibility_error": "HTTP 400: unsupported endpoint",
                },
            ]

        async def close(self):
            return None

    monkeypatch.setattr(dashboard_web, "OpenAICompatibleProvider", FakeProbeProvider)
    monkeypatch.setattr(providers_module, "OpenAICompatibleProvider", FakeProbeProvider)
    config_path = tmp_path / "broker_config.yaml"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-probe-provider.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config, config_path=config_path)) as client:
        token = dashboard_csrf(client)
        response = client.post(
            "/dashboard/actions/providers/nvidia/probe",
            data={
                "csrf_token": token,
                "probe_progress_id": "probe-test-id",
                "task_timeout_seconds": "900",
                "max_parallel_invocations": "auto",
                "queue_max_size": "250",
                "local_vram_budget_gb": "48",
                "vram_safety_margin_gb": "4",
                "max_loaded_local_models": "auto",
                "allow_execution_waves": "on",
                "custom_provider_1_enabled": "on",
                "custom_provider_1_id": "nvidia",
                "custom_provider_1_display_name": "NVIDIA NIM",
                "custom_provider_1_base_url": "https://integrate.api.nvidia.com/v1",
                "custom_provider_1_api_key_env": "NVIDIA_API_KEY",
                "custom_provider_1_deployment": "api",
                "custom_provider_1_timeout_seconds": "300",
                "custom_provider_1_default_context_window": "128000",
                "custom_provider_1_probe_max_output_tokens": "1",
                "custom_provider_1_probe_delay_seconds": "0",
                "custom_provider_1_probe_max_models": "50",
                "custom_provider_1_probe_skip_compatible": "on",
                "custom_provider_1_probe_skip_checked": "on",
                "custom_provider_1_input_cost_per_million": "0",
                "custom_provider_1_output_cost_per_million": "0",
                "custom_provider_1_sync_models": "on",
            },
        )
        progress = client.get(
            "/dashboard/actions/providers/nvidia/probe/progress",
            params={"progress_id": "probe-test-id"},
        )

    saved = load_config(config_path)
    by_name = {item.name: item for item in saved.providers.custom[0].models}
    assert response.status_code == 200
    assert progress.status_code == 200
    assert progress.json()["phase"] == "completed"
    assert progress.json()["completed"] == 2
    assert by_name["chat-ok"].compatibility == "compatible"
    assert by_name["vision-only"].compatibility == "incompatible"
    assert by_name["not-probed-yet"].compatibility == "unknown"
    assert by_name["not-probed-yet"].context_window == 64000
    assert saved.providers.custom[0].probe_skip_compatible is True
    assert saved.providers.custom[0].probe_skip_checked is True
    assert "No compatible mixture" in response.text


def test_models_dashboard_can_probe_one_custom_model(tmp_path: Path, monkeypatch) -> None:
    class FakeProbeProvider:
        def __init__(self, config):
            self.config = config

        async def models(self):
            return [
                {
                    "name": item.name,
                    "provider": self.config.id,
                    "deployment": self.config.deployment,
                    "context_window": item.context_window,
                    "capabilities": list(item.capabilities),
                    "compatibility": item.compatibility,
                    "compatibility_checked_at": item.compatibility_checked_at,
                    "compatibility_error": item.compatibility_error,
                }
                for item in self.config.models
            ]

        async def probe_chat_compatibility(self, model):
            return {
                "name": model,
                "compatibility": "compatible",
                "compatibility_checked_at": "2026-07-01T12:00:00+00:00",
                "compatibility_error": None,
            }

        async def close(self):
            return None

    monkeypatch.setattr(dashboard_web, "OpenAICompatibleProvider", FakeProbeProvider)
    monkeypatch.setattr(providers_module, "OpenAICompatibleProvider", FakeProbeProvider)
    config_path = tmp_path / "broker_config.yaml"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-model-probe.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="real"),
        providers=ProvidersConfig(
            ollama=OllamaConfig(enabled=False),
            custom=[
                OpenAICompatibleProviderConfig(
                    id="nvidia",
                    enabled=True,
                    base_url="https://integrate.api.nvidia.com/v1",
                    deployment="api",
                    probe_delay_seconds=0,
                    models=[
                        OpenAICompatibleModelConfig(
                            name="chat-pending",
                            context_window=128000,
                            capabilities=["completion"],
                            compatibility="unknown",
                        ),
                        OpenAICompatibleModelConfig(
                            name="leave-alone",
                            context_window=64000,
                            capabilities=["completion"],
                            compatibility="unknown",
                        ),
                    ],
                )
            ],
        ),
    )
    with TestClient(create_app(config, config_path=config_path)) as client:
        token = dashboard_csrf(client, "/dashboard/models")
        page = client.get("/dashboard/models")
        response = client.post(
            "/dashboard/actions/models/probe",
            headers={"Accept": "application/json"},
            data={
                "csrf_token": token,
                "provider": "nvidia",
                "model": "chat-pending",
            },
        )

    saved = load_config(config_path)
    by_name = {item.name: item for item in saved.providers.custom[0].models}
    assert page.status_code == 200
    assert 'action="/dashboard/actions/models/probe"' in page.text
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["compatibility"] == "compatible"
    assert response.json()["compatibility_text"] == "Compatible mixture"
    assert by_name["chat-pending"].compatibility == "compatible"
    assert by_name["chat-pending"].compatibility_checked_at == "2026-07-01T12:00:00+00:00"
    assert by_name["leave-alone"].compatibility == "unknown"


def test_dashboard_javascript_reads_form_action_attribute() -> None:
    script = Path("app/static/dashboard.js").read_text(encoding="utf-8")

    assert 'form.getAttribute("action")' in script
    assert "form.action" not in script
    assert 'form.getAttribute("method")' in script
    assert "form.method" not in script


def test_dashboard_task_detail_renders_results_and_errors(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-detail.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        completed = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "detail:ok", "content": {"prompt": "Resume"}},
        ).json()
        client.post("/api/v1/dispatcher/tick")
        completed_page = client.get(f"/dashboard/tasks/{completed['task_id']}")

        failed = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "detail:fail",
                "content": {"prompt": "No valido"},
                "execution": {"strategy": "mixture_of_agents", "preset": "standard"},
            },
        ).json()
        client.post("/api/v1/dispatcher/tick")
        failed_page = client.get(f"/dashboard/tasks/{failed['task_id']}")

    assert completed_page.status_code == 200
    assert "Resultado final" in completed_page.text
    assert "Proveedor bootstrap" in completed_page.text
    assert failed_page.status_code == 200
    assert "CONSENSUS_PRESET_NOT_IMPLEMENTED" in failed_page.text


def test_dashboard_task_detail_identifies_failed_model(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-model-error.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = FailingSynthesisProvider()
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "detail:model-error",
                "content": {"prompt": "Compara alternativas"},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "preset": "slow",
                    "scheduling": "parallel",
                    "max_proposers": 2,
                    "selection": {"mode": "auto", "proposer_count": 2},
                },
            },
        ).json()
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{created['task_id']}").json()
        page = client.get(f"/dashboard/tasks/{created['task_id']}")

    assert detail["task"]["status"] == "failed"
    assert detail["error"]["stage"] == "synthesizing"
    assert detail["error"]["role"] == "arbiter"
    assert detail["error"]["model"] == "bootstrap-1"
    assert page.status_code == 200
    assert "Modelo fallido" in page.text
    assert "bootstrap/bootstrap-1" in page.text


def test_dashboard_serves_security_headers(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_state_backup_verify_and_restore_roundtrip(tmp_path: Path) -> None:
    database = tmp_path / "broker.db"
    artifacts = tmp_path / "tasks"
    backup = tmp_path / "backup.zip"
    restored_database = tmp_path / "restored" / "broker.db"
    restored_artifacts = tmp_path / "restored" / "tasks"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(database)),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "backup:task", "content": {"prompt": "Persistir"}},
        ).json()
        client.post("/api/v1/dispatcher/tick")
        task_id = created["task_id"]

    artifacts.joinpath(task_id, "manual.txt").parent.mkdir(parents=True, exist_ok=True)
    artifacts.joinpath(task_id, "manual.txt").write_text("artifact", encoding="utf-8")

    result = create_state_backup(
        database_path=database,
        artifacts_root=artifacts,
        output_path=backup,
    )
    manifest = verify_state_backup(backup)
    restore_state_backup(
        backup_path=backup,
        database_path=restored_database,
        artifacts_root=restored_artifacts,
    )

    assert result.files >= 2
    assert manifest["format"] == "ai-broker-backup-v1"
    assert restored_database.exists()
    assert restored_artifacts.joinpath(task_id, "manual.txt").read_text(encoding="utf-8") == "artifact"


def test_operational_logging_rotates_and_does_not_log_prompt_body(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-log.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        logging=LoggingConfig(
            directory=str(log_dir),
            filename="broker.log",
            max_bytes=1024,
            backup_count=2,
            console_enabled=False,
        ),
    )
    secret_prompt = "NO_DEBE_APARECER_EN_LOGS"
    with TestClient(create_app(config)) as client:
        for _ in range(40):
            client.get("/health/live")
        client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "logs:prompt", "content": {"prompt": secret_prompt}},
        )

    log_files = list(log_dir.glob("broker.log*"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in log_files)
    assert (log_dir / "broker.log").exists()
    assert any(path.name != "broker.log" for path in log_files)
    assert "http.request" in combined
    assert "/health/live" in combined
    assert secret_prompt not in combined


def test_windows_service_and_readiness_scripts_are_present() -> None:
    root = Path(__file__).parents[1]
    install = root / "scripts" / "install_windows_service.ps1"
    uninstall = root / "scripts" / "uninstall_windows_service.ps1"
    firewall = root / "scripts" / "configure_firewall_lan.ps1"
    readiness = root / "scripts" / "check_readiness.py"
    runner = root / "scripts" / "run_broker.py"

    assert install.exists()
    assert uninstall.exists()
    assert firewall.exists()
    assert readiness.exists()
    assert runner.exists()
    assert "nssm" in install.read_text(encoding="utf-8").lower()
    assert "SupportsShouldProcess" in firewall.read_text(encoding="utf-8")
    assert "/health/ready" in readiness.read_text(encoding="utf-8")
    runner_source = runner.read_text(encoding="utf-8")
    # El runner debe construir la app con el mismo --config (antes el factory
    # de Uvicorn recargaba la config por defecto e ignoraba el path) y pasar
    # la instancia a uvicorn.run: proceso único sin necesitar workers=1.
    assert "config_path=args.config" in runner_source
    assert '"app.main:create_app"' not in runner_source


def test_dashboard_resources_degrade_without_breaking_the_panel() -> None:
    config = BrokerConfig(processing=ProcessingConfig(provider_mode="bootstrap", auto_dispatch=False))
    resources = asyncio.run(
        load_dashboard_resources(UnavailableResourceProvider(), ResourceScheduler(config), config)
    )

    assert resources.status == "unavailable"
    assert resources.used_vram_bytes == 0
    assert resources.detail == "PROVIDER_UNAVAILABLE: snapshot de recursos no disponible"


def test_models_dashboard_has_dedicated_screen_and_navigation(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        dashboard = client.get("/dashboard")
        models = client.get("/dashboard/models")
        tester = client.get("/dashboard/prompt-tester")

    assert dashboard.status_code == 200
    assert models.status_code == 200
    assert tester.status_code == 200
    assert 'href="/dashboard/models">Modelos' in dashboard.text
    assert 'href="/dashboard/models">Modelos' in tester.text
    assert 'href="#resources-panel">Modelos' not in dashboard.text
    assert "Catalogo operativo" in models.text
    assert "Runtime local" in models.text


def test_prompt_tester_validates_json_without_creating_task(tmp_path: Path) -> None:
    model_value = json.dumps({"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single"})
    with make_client(tmp_path) as client:
        token = dashboard_csrf(client, "/dashboard/prompt-tester")
        page = client.get("/dashboard/prompt-tester")
        invalid = client.post(
            "/dashboard/actions/prompt-tester",
            data={
                "action": "enqueue",
                "csrf_token": token,
                "input_mode": "json",
                "prompt": "{\"broken\":",
                "strategy": "single",
                "single_model": model_value,
            },
        )
        queue = client.get("/api/v1/queue").json()

    assert page.status_code == 200
    assert "Probador de Prompts" in page.text
    assert invalid.status_code == 200
    assert "JSON de entrada invalido" in invalid.text
    assert queue["pending"] == []


def test_prompt_tester_enqueues_exact_single_model(tmp_path: Path) -> None:
    model_value = json.dumps({"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single"})
    with make_client(tmp_path) as client:
        token = dashboard_csrf(client, "/dashboard/prompt-tester")
        response = client.post(
            "/dashboard/actions/prompt-tester",
            data={
                "action": "enqueue",
                "csrf_token": token,
                "input_mode": "prompt",
                "prompt": "<script>alert('x')</script>",
                "strategy": "single",
                "single_model": model_value,
                "fallback_allowed": "",
            },
        )
        queue = client.get("/api/v1/queue").json()
        detail = client.get(f"/api/v1/dashboard/tasks/{queue['pending'][0]['task_id']}").json()

    assert response.status_code == 200
    assert "Tarea encolada" in response.text
    assert "Impacto operativo validado" in response.text
    assert "single/fast - 1 invocacion" in response.text
    assert "Cloud bloqueado - fallback bloqueado" in response.text
    assert f"/dashboard/tasks/{queue['pending'][0]['task_id']}" in response.text
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in response.text
    assert "<script>alert('x')</script>" not in response.text
    assert detail["request"]["content"]["metadata"]["origin"] == "prompt_tester"
    assert detail["request"]["model_requirements"]["target_model"] == {
        "provider": "ollama",
        "deployment": "bootstrap",
        "model": "bootstrap-single",
        "role": None,
        "required": False,
    }
    assert detail["request"]["model_requirements"]["fallback_allowed"] is False


def test_prompt_tester_enqueues_manual_mixture(tmp_path: Path) -> None:
    model_value = json.dumps({"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single"})
    with make_client(tmp_path) as client:
        token = dashboard_csrf(client, "/dashboard/prompt-tester")
        response = client.post(
            "/dashboard/actions/prompt-tester",
            data={
                "action": "enqueue",
                "csrf_token": token,
                "input_mode": "prompt",
                "prompt": "Compara opciones",
                "strategy": "mixture_of_agents",
                "preset": "slow",
                "scheduling": "parallel",
                "proposer_model_1": model_value,
                "proposer_role_1": "architect",
                "proposer_model_2": model_value,
                "proposer_role_2": "reviewer",
                "arbiter_model": model_value,
            },
        )
        queue = client.get("/api/v1/queue").json()
        detail = client.get(f"/api/v1/dashboard/tasks/{queue['pending'][0]['task_id']}").json()

    assert response.status_code == 200
    request = detail["request"]
    assert request["execution"]["strategy"] == "mixture_of_agents"
    assert request["execution"]["preset"] == "slow"
    assert request["execution"]["scheduling"] == "parallel"
    assert request["execution"]["selection"]["mode"] == "manual"
    assert request["execution"]["selection"]["allow_substitution"] is False
    assert [item["role"] for item in request["execution"]["selection"]["proposers"]] == ["architect", "reviewer"]
    assert request["execution"]["selection"]["arbiter"]["model"] == "bootstrap-single"


def test_prompt_tester_rejects_cloud_models_when_cloud_is_not_allowed(tmp_path: Path) -> None:
    local_model = json.dumps({"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-single"})
    cloud_model = json.dumps({"provider": "ollama", "deployment": "cloud", "model": "remote:cloud"})
    with make_client(tmp_path) as client:
        token = dashboard_csrf(client, "/dashboard/prompt-tester")
        response = client.post(
            "/dashboard/actions/prompt-tester",
            data={
                "action": "enqueue",
                "csrf_token": token,
                "input_mode": "prompt",
                "prompt": "Compara opciones",
                "strategy": "mixture_of_agents",
                "preset": "slow",
                "scheduling": "parallel",
                "proposer_model_1": local_model,
                "proposer_role_1": "architect",
                "proposer_model_2": cloud_model,
                "proposer_role_2": "reviewer",
                "arbiter_model": local_model,
            },
        )
        queue = client.get("/api/v1/queue").json()

    assert response.status_code == 200
    assert "Marca Permitir cloud" in response.text
    assert queue["pending"] == []


def test_dispatcher_processes_single_task(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json={"idempotency_key": "test:single", "content": {"prompt": "Resume esto"}}).json()

        tick = client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

        assert tick.status_code == 200
        assert tick.json()["task_id"] == created["task_id"]
        assert task["status"] == "completed"
        assert task["result"]["usage"]["invocations"] == 1
        assert "result_markdown" in task["result"]


def test_dispatcher_processes_fast_consensus(tmp_path: Path) -> None:
    payload = {
        "idempotency_key": "test:consensus",
        "content": {"prompt": "Compara dos enfoques de routing"},
        "execution": {
            "strategy": "mixture_of_agents",
            "preset": "fast",
            "max_proposers": 3,
            "selection": {"mode": "auto", "proposer_count": 3},
        },
    }
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json=payload).json()

        tick = client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

        assert tick.status_code == 200
        assert task["status"] == "completed"
        assert task["result"]["consensus"]["proposers_completed"] == 3
        assert task["result"]["usage"]["invocations"] == 4
        assert task["result"]["scheduling"]["mode_used"] in {"parallel", "waves", "sequential"}
        assert Path(task["result"]["artifacts_root"]).exists()
        assert (Path(task["result"]["artifacts_root"]) / "synthesis" / "final.md").exists()


def test_slow_consensus_runs_proposers_in_parallel_but_fast_remains_serial(tmp_path: Path) -> None:
    def execute(preset: str, database_name: str) -> tuple[dict, int]:
        config = BrokerConfig(
            persistence=PersistenceConfig(database=str(tmp_path / database_name)),
            processing=ProcessingConfig(
                auto_dispatch=False,
                provider_mode="bootstrap",
                max_parallel_invocations=3,
            ),
        )
        probe = ConcurrencyProbeProvider()
        with TestClient(create_app(config)) as client:
            client.app.state.coordinator.provider = probe
            payload = {
                "idempotency_key": f"test:{preset}:parallelism",
                "content": {"prompt": "Compara tres alternativas"},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "preset": preset,
                    "scheduling": "adaptive",
                    "max_proposers": 3,
                    "selection": {"mode": "auto", "proposer_count": 3},
                },
            }
            created = client.post("/api/v1/tasks", json=payload)
            assert created.status_code == 202
            task_id = created.json()["task_id"]
            client.post("/api/v1/dispatcher/tick")
            task = client.get(f"/api/v1/tasks/{task_id}").json()
        return task, probe.max_active

    slow_task, slow_parallelism = execute("slow", "broker-slow.db")
    fast_task, fast_parallelism = execute("fast", "broker-fast.db")

    assert slow_task["status"] == "completed"
    assert slow_task["result"]["consensus"]["level"] == "slow"
    assert slow_task["result"]["scheduling"]["mode_used"] == "parallel"
    assert slow_task["result"]["scheduling"]["max_parallel_invocations_launched"] == 3
    assert slow_parallelism == 3

    assert fast_task["status"] == "completed"
    assert fast_task["result"]["consensus"]["level"] == "fast"
    assert fast_task["result"]["scheduling"]["mode_used"] == "sequential"
    assert fast_parallelism == 1


def test_slow_parallel_fails_before_launch_when_capacity_is_insufficient(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-slow-capacity.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            max_parallel_invocations=1,
        ),
    )
    probe = ConcurrencyProbeProvider()
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = probe
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "test:slow:insufficient",
                "content": {"prompt": "Compara tres alternativas"},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "preset": "slow",
                    "scheduling": "parallel",
                    "max_proposers": 3,
                    "selection": {"mode": "auto", "proposer_count": 3},
                },
            },
        ).json()

        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

    assert task["status"] == "failed"
    assert task["error"]["code"] == "PARALLEL_CAPACITY_INSUFFICIENT"
    assert probe.max_active == 0


def test_slow_waves_do_not_repeat_models_after_retryable_proposer_failure(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-waves-retryable.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            max_parallel_invocations=3,
        ),
    )
    provider = RetryableFirstProposerProvider()
    proposers = [
        {"provider": "ollama", "deployment": "test", "model": "m1", "role": "generalist"},
        {"provider": "ollama", "deployment": "test", "model": "m2", "role": "specialist"},
        {"provider": "ollama", "deployment": "test", "model": "m3", "role": "skeptic"},
        {"provider": "ollama", "deployment": "test", "model": "m4", "role": "analyst"},
        {"provider": "ollama", "deployment": "test", "model": "m5", "role": "reviewer"},
    ]
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = provider
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "waves:retryable-proposer",
                "content": {"prompt": "Compara cinco perspectivas"},
                "model_requirements": {"allowed_providers": ["ollama"]},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "preset": "slow",
                    "scheduling": "adaptive",
                    "max_proposers": 5,
                    "selection": {
                        "mode": "manual",
                        "proposer_count": 5,
                        "proposers": proposers,
                        "arbiter": {"provider": "ollama", "deployment": "test", "model": "arbiter"},
                    },
                },
            },
        ).json()
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{created['task_id']}").json()
        page = client.get(f"/dashboard/tasks/{created['task_id']}")

    invoked_proposers = [
        item["model"]
        for item in detail["invocations"]
        if item["role"] != "arbiter"
    ]
    skipped = detail["result"]["skipped_proposers"]
    assert detail["task"]["status"] == "completed"
    assert invoked_proposers == ["m2", "m3", "m4", "m5"]
    assert len(invoked_proposers) == len(set(invoked_proposers))
    assert detail["result"]["consensus"]["proposers_failed"] == 1
    assert skipped[0]["model"] == "m1"
    assert skipped[0]["role"] == "generalist"
    assert "m1" in detail["result"]["consensus"]["warnings"][0]
    assert "Modelos omitidos" in page.text
    assert "ollama/test/m1" in page.text


def test_comparison_dashboard_renders_measured_slow_overlap(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-comparison.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            max_parallel_invocations=3,
        ),
    )
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = TimelineProbeProvider()
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "comparison:slow",
                "content": {"prompt": "Compara alternativas"},
                "execution": {
                    "strategy": "mixture_of_agents",
                    "preset": "slow",
                    "scheduling": "parallel",
                    "max_proposers": 3,
                    "selection": {"mode": "auto", "proposer_count": 3},
                },
            },
        ).json()

        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{created['task_id']}").json()
        page = client.get(f"/dashboard/comparison?task_id={created['task_id']}")

    assert detail["invocations"][0]["started_at"] is not None
    assert detail["invocations"][0]["completed_at"] is not None
    assert page.status_code == 200
    assert "Comparaci" in page.text
    assert created["task_id"] in page.text
    assert "observado" in page.text
    assert "Propuesta" not in page.text


def test_single_rejects_slow_preset(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "test:single:slow",
                "content": {"prompt": "No válido"},
                "execution": {"strategy": "single", "preset": "slow"},
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "CONTRACT_VALIDATION_FAILED"


def test_execution_timeout_cancels_provider_and_persists_typed_error(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-timeout.db")),
        processing=ProcessingConfig(
            auto_dispatch=False,
            provider_mode="bootstrap",
            task_timeout_seconds=10,
        ),
    )
    probe = TimeoutProbeProvider()
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = probe
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "test:task:timeout",
                "content": {"prompt": "Tarea lenta"},
                "execution": {"strategy": "single", "timeout_seconds": 1},
            },
        ).json()

        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

    assert task["status"] == "failed"
    assert task["error"]["code"] == "TASK_TIMEOUT"
    assert task["progress"]["timeout_seconds"] == 1
    assert probe.cancelled


def test_context_limit_error_json_explains_prompt_exceeds_model_window(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-context-error.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        client.app.state.coordinator.provider = PromptTooLargeProvider()
        created = client.post(
            "/api/v1/tasks",
            json={
                "idempotency_key": "test:context:prompt-too-large",
                "content": {"prompt": "Prompt enorme"},
            },
        ).json()

        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

    assert task["status"] == "failed"
    assert task["error"]["code"] == "CONTEXT_LIMIT_EXCEEDED"
    assert task["error"]["details"]["reason"] == "prompt_context_exceeded"
    assert task["error"]["details"]["prompt_tokens_estimate"] == 1200
    assert task["error"]["details"]["context_window"] == 1000
    assert task["error"]["details"]["max_output_tokens_allowed"] == 0
    assert task["error"]["stage"] == "generating"
    assert task["error"]["model"] == "bootstrap-1"


def test_standard_consensus_fails_until_phase_c(tmp_path: Path) -> None:
    payload = {
        "idempotency_key": "test:standard",
        "content": {"prompt": "Analiza con rúbrica"},
        "execution": {
            "strategy": "mixture_of_agents",
            "preset": "standard",
            "selection": {"mode": "auto", "proposer_count": 3},
        },
    }
    with make_client(tmp_path) as client:
        created = client.post("/api/v1/tasks", json=payload).json()

        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

        assert task["status"] == "failed"
        assert task["error"]["code"] == "CONSENSUS_PRESET_NOT_IMPLEMENTED"


def test_create_is_idempotent_and_conflicting_payload_returns_409(tmp_path: Path) -> None:
    payload = {"idempotency_key": "idem:same", "request_id": "local-1", "content": {"prompt": "Hola"}}
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/tasks", json=payload)
        replay = client.post("/api/v1/tasks", json=payload)
        conflict = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "idem:same", "request_id": "local-1", "content": {"prompt": "Otro"}},
        )

        assert first.status_code == 202
        assert replay.status_code == 200
        assert replay.json()["task_id"] == first.json()["task_id"]
        assert conflict.status_code == 409
        assert len(client.get("/api/v1/queue").json()["pending"]) == 1


def test_background_dispatcher_consumes_queue_without_tick(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker-auto.db")),
        processing=ProcessingConfig(auto_dispatch=True, dispatcher_interval_seconds=0.01, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "auto:single", "content": {"prompt": "Procesa automáticamente"}},
        ).json()
        deadline = time.monotonic() + 2
        task = client.get(created["status_url"]).json()
        while task["status"] not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
            time.sleep(0.01)
            task = client.get(created["status_url"]).json()

        assert task["status"] == "completed"
        assert task["result"]["result_markdown"]


def test_idempotency_survives_broker_restart(tmp_path: Path) -> None:
    database = tmp_path / "broker-restart.db"
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(database)),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    payload = {"idempotency_key": "restart:same", "content": {"prompt": "No duplicar"}}
    with TestClient(create_app(config)) as first_client:
        first = first_client.post("/api/v1/tasks", json=payload)
        assert first.status_code == 202
        task_id = first.json()["task_id"]

    with TestClient(create_app(config)) as restarted_client:
        replay = restarted_client.post("/api/v1/tasks", json=payload)
        assert replay.status_code == 200
        assert replay.json()["task_id"] == task_id
        assert len(restarted_client.get("/api/v1/queue").json()["pending"]) == 1


def test_claim_is_atomic_and_never_activates_second_workflow(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for ordinal in (1, 2):
            response = client.post(
                "/api/v1/tasks",
                json={"idempotency_key": f"claim:{ordinal}", "content": {"prompt": f"Tarea {ordinal}"}},
            )
            assert response.status_code == 202
        repository = client.app.state.repository
        first = repository.claim_next_queued_task_id()
        second = repository.claim_next_queued_task_id()
        queue = client.get("/api/v1/queue").json()

        assert first is not None
        assert second is None
        assert len(queue["active"]) == 1
        assert len(queue["pending"]) == 1


def test_recovery_respects_max_task_attempts(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for ordinal in (1, 2):
            response = client.post(
                "/api/v1/tasks",
                json={"idempotency_key": f"recover:{ordinal}", "content": {"prompt": f"Tarea {ordinal}"}},
            )
            assert response.status_code == 202
        repository = client.app.state.repository
        db = client.app.state.db
        queue = client.get("/api/v1/queue").json()
        fresh_id = queue["pending"][0]["task_id"]
        exhausted_id = queue["pending"][1]["task_id"]
        db.execute("UPDATE tasks SET status = 'generating', attempt = 0 WHERE id = ?", (fresh_id,))
        db.execute("UPDATE tasks SET status = 'proposing', attempt = 2 WHERE id = ?", (exhausted_id,))

        recovered = repository.recover_interrupted_tasks(max_attempts=3)

        assert recovered == 1
        fresh = client.get(f"/api/v1/tasks/{fresh_id}").json()
        exhausted = client.get(f"/api/v1/tasks/{exhausted_id}").json()
        assert fresh["status"] == "queued"
        assert exhausted["status"] == "failed"
        assert exhausted["error"]["code"] == "TASK_RETRY_LIMIT_EXCEEDED"
        assert exhausted["error"]["retryable"] is False


def test_dashboard_actions_require_admin_token_when_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "secreto-admin")
    with make_client(tmp_path) as client:
        anonymous = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "admin:cancel", "content": {"prompt": "Tarea"}},
        )
        assert anonymous.status_code == 403
        assert anonymous.json()["detail"] == "ADMIN_AUTH_REQUIRED"

        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "admin:cancel", "content": {"prompt": "Tarea"}},
            headers={"X-Admin-Token": "secreto-admin"},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        token = dashboard_csrf(client)

        denied = client.post(
            f"/dashboard/actions/tasks/{task_id}/cancel",
            headers={"X-CSRF-Token": token},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "ADMIN_AUTH_REQUIRED"

        bad_login = client.post(
            "/dashboard/actions/login",
            data={"csrf_token": token, "admin_token": "incorrecto"},
        )
        assert bad_login.status_code == 403

        good_login = client.post(
            "/dashboard/actions/login",
            data={"csrf_token": token, "admin_token": "secreto-admin"},
            follow_redirects=False,
        )
        assert good_login.status_code == 303
        assert "ai_broker_dashboard_admin" in good_login.cookies

        allowed = client.post(
            f"/dashboard/actions/tasks/{task_id}/cancel",
            headers={"X-CSRF-Token": token},
        )
        assert allowed.status_code == 204
        # La lectura del estado también exige credencial: incluye result/progress.
        state = client.get(f"/api/v1/tasks/{task_id}", headers={"X-Admin-Token": "secreto-admin"})
        assert state.json()["status"] == "cancelled"


def test_dashboard_actions_accept_admin_token_header(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "secreto-admin")
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "admin:header", "content": {"prompt": "Tarea"}},
            headers={"X-Admin-Token": "secreto-admin"},
        )
        task_id = created.json()["task_id"]

        api_denied = client.patch("/api/v1/queue", json={"task_ids": [task_id]})
        assert api_denied.status_code == 403

        api_allowed = client.patch(
            "/api/v1/queue",
            json={"task_ids": [task_id]},
            headers={"X-Admin-Token": "secreto-admin"},
        )
        assert api_allowed.status_code == 200
        token = dashboard_csrf(client)
        allowed = client.post(
            f"/dashboard/actions/tasks/{task_id}/cancel",
            headers={"X-CSRF-Token": token, "X-Admin-Token": "secreto-admin"},
        )
        assert allowed.status_code == 204


def _fresh_keyring(monkeypatch, fake_get_password) -> None:
    """Limpia la caché del token y sustituye el backend de credenciales."""
    import keyring

    from app import admin_auth

    admin_auth._keyring_cache.clear()
    monkeypatch.setattr(keyring, "get_password", fake_get_password)


def test_sensitive_reads_require_admin_token_when_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "secreto-admin")
    with make_client(tmp_path) as client:
        admin = {"X-Admin-Token": "secreto-admin"}
        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "admin:reads", "content": {"prompt": "Contenido privado"}},
            headers=admin,
        )
        task_id = created.json()["task_id"]

        # Lecturas con prompts/resultados: cerradas sin credencial.
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 403
        assert client.get("/api/v1/dashboard/tasks").status_code == 403
        assert client.get(f"/api/v1/dashboard/tasks/{task_id}").status_code == 403
        # Con credencial siguen funcionando.
        assert client.get(f"/api/v1/tasks/{task_id}", headers=admin).status_code == 200
        assert client.get("/api/v1/dashboard/tasks", headers=admin).status_code == 200
        assert client.get(f"/api/v1/dashboard/tasks/{task_id}", headers=admin).status_code == 200
        # La cola solo expone ids/estados (sin contenido): sigue abierta.
        assert client.get("/api/v1/queue").status_code == 200

        # Las páginas del panel redirigen al login; los fragmentos responden 403.
        page = client.get("/dashboard", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/dashboard/login"
        assert client.get("/dashboard/login").status_code == 200
        assert client.get("/dashboard/fragments/active").status_code == 403
        assert client.get(f"/dashboard/tasks/{task_id}", follow_redirects=False).status_code == 303
        # Con la cabecera admin las vistas cargan.
        assert client.get("/dashboard", headers=admin).status_code == 200


def test_startup_refuses_lan_exposure_without_admin_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_BROKER_ADMIN_TOKEN", raising=False)
    config = BrokerConfig(
        server=ServerConfig(host="0.0.0.0"),
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )

    # Sin token (keyring responde pero no hay credencial guardada): no arranca.
    _fresh_keyring(monkeypatch, lambda service, username: None)
    with pytest.raises(RuntimeError, match="sin token admin"):
        create_app(config)

    # Backend de credenciales roto: tampoco arranca (no verificable != sin token).
    def broken_backend(service, username):
        raise RuntimeError("keyring roto")

    _fresh_keyring(monkeypatch, broken_backend)
    with pytest.raises(RuntimeError, match="backend de credenciales"):
        create_app(config)

    # Con token por variable de entorno arranca con normalidad.
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "secreto")
    with TestClient(create_app(config)) as client:
        assert client.get("/health/live").status_code == 200

    # Opt-out explícito: arranca sin token (queda solo el warning de exposición).
    monkeypatch.delenv("AI_BROKER_ADMIN_TOKEN", raising=False)
    _fresh_keyring(monkeypatch, lambda service, username: None)
    exposed = BrokerConfig(
        server=ServerConfig(host="0.0.0.0", allow_unauthenticated_lan=True),
        persistence=PersistenceConfig(database=str(tmp_path / "optout.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    with TestClient(create_app(exposed)) as client:
        assert client.get("/health/live").status_code == 200


def test_admin_verification_fails_closed_when_keyring_breaks(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.admin_auth import verify_admin_access

    monkeypatch.delenv("AI_BROKER_ADMIN_TOKEN", raising=False)

    def broken_backend(service, username):
        raise RuntimeError("keyring roto")

    _fresh_keyring(monkeypatch, broken_backend)

    # Fuera de loopback un keyring roto deniega el acceso (503), no lo abre.
    exposed = BrokerConfig(server=ServerConfig(host="0.0.0.0", allow_unauthenticated_lan=False))
    with pytest.raises(HTTPException) as raised:
        verify_admin_access(None, exposed)
    assert raised.value.status_code == 503
    assert raised.value.detail == "ADMIN_AUTH_BACKEND_UNAVAILABLE"

    # En loopback se degrada a "sin token": la API solo es alcanzable localmente.
    _fresh_keyring(monkeypatch, broken_backend)
    verify_admin_access(None, BrokerConfig())


def test_health_reports_dispatcher_state(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=True, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        healthy = client.get("/health").json()
        assert healthy["dependencies"]["dispatcher"]["status"] == "healthy"

        # Simula la muerte del bucle: el estado global debe pasar a degraded.
        task = client.app.state.dispatcher_task
        task.get_loop().call_soon_threadsafe(task.cancel)
        for _ in range(50):
            report = client.get("/health").json()
            if report["dependencies"]["dispatcher"]["status"] == "unavailable":
                break
        else:
            raise AssertionError("dispatcher dependency never became unavailable")
        assert report["status"] == "degraded"
        client.app.state.dispatcher_task = None


def test_health_omits_dispatcher_when_auto_dispatch_disabled(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        report = client.get("/health").json()
        assert "dispatcher" not in report["dependencies"]


def test_ready_returns_503_when_dispatcher_stops(tmp_path: Path) -> None:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=True, provider_mode="bootstrap"),
    )
    with TestClient(create_app(config)) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200

        # Con el bucle de despacho muerto el servicio acepta tareas que nadie
        # despachará: /health/ready debe dejar de responder 200.
        task = client.app.state.dispatcher_task
        task.get_loop().call_soon_threadsafe(task.cancel)
        for _ in range(50):
            not_ready = client.get("/health/ready")
            if not_ready.status_code == 503:
                break
        else:
            raise AssertionError("/health/ready never returned 503 after dispatcher death")
        assert not_ready.json()["dependencies"]["dispatcher"]["status"] == "unavailable"
        client.app.state.dispatcher_task = None


def test_health_reports_disk_dependency(tmp_path: Path) -> None:
    from app.config import HealthConfig

    with make_client(tmp_path) as client:
        report = client.get("/health").json()
        assert report["dependencies"]["disk"]["status"] == "healthy"

    # Con un umbral imposible (1 PB libre) el mismo volumen pasa a degraded.
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "alert.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        health=HealthConfig(disk_free_alert_gb=1_000_000),
    )
    with TestClient(create_app(config)) as client:
        report = client.get("/health").json()
        assert report["dependencies"]["disk"]["status"] == "degraded"
        assert report["status"] == "degraded"
        # El disco degradado no tumba la readiness: solo sqlite o dispatcher.
        assert client.get("/health/ready").status_code == 200


def test_zero_cost_cloud_providers_detection() -> None:
    from app.config import DeepSeekConfig, OpenAICompatibleProviderConfig, ProvidersConfig
    from app.main import _zero_cost_cloud_providers

    config = BrokerConfig(
        providers=ProvidersConfig(
            deepseek=DeepSeekConfig(enabled=True),
            custom=[
                OpenAICompatibleProviderConfig(
                    id="nvidia", enabled=True, base_url="https://example", deployment="cloud"
                ),
                OpenAICompatibleProviderConfig(
                    id="paid", enabled=True, base_url="https://example", deployment="cloud",
                    input_cost_per_million=0.5,
                ),
                OpenAICompatibleProviderConfig(
                    id="lmstudio", enabled=True, base_url="http://localhost:1234", deployment="local"
                ),
            ],
        )
    )
    assert _zero_cost_cloud_providers(config) == ["deepseek", "nvidia"]

    config.providers.deepseek.input_cost_per_million = 0.27
    assert _zero_cost_cloud_providers(config) == ["nvidia"]


def test_database_rejects_execute_inside_transaction(tmp_path: Path) -> None:
    from app.db import Database

    db = Database(tmp_path / "guard.db")
    db.init_schema()
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (NULL, 'x', '{}', 'now')")
    with pytest.raises(RuntimeError):
        with db.transaction():
            with db.transaction():
                pass
    # Tras el fallo, la conexión queda utilizable y sin transacción colgada.
    db.execute("INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (NULL, 'ok', '{}', 'now')")
    assert db.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 1
    db.close()


def test_prune_terminal_task_events(tmp_path: Path) -> None:
    from app.db import Database, dumps_json
    from app.maintenance import prune_terminal_task_events

    db = Database(tmp_path / "prune.db")
    db.init_schema()
    old, recent = "2024-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"
    for task_id, status, stamp in (
        ("t-old-done", "completed", old),
        ("t-old-live", "queued", old),
        ("t-new-done", "completed", recent),
    ):
        db.execute(
            "INSERT INTO tasks(id, request_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, dumps_json({}), status, stamp, stamp),
        )
        db.execute(
            "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, 'x', '{}', ?)",
            (task_id, stamp),
        )

    assert prune_terminal_task_events(db, older_than_days=30) == 1
    remaining = {row["task_id"] for row in db.query_all("SELECT task_id FROM events")}
    # Solo cae el evento de la tarea terminal antigua; la viva y la reciente se conservan.
    assert remaining == {"t-old-live", "t-new-done"}
    assert prune_terminal_task_events(db, older_than_days=0) == 0
    db.close()


def test_vram_budget_mismatch_detection() -> None:
    from app.main import _vram_budget_mismatch

    assert _vram_budget_mismatch(64.0, 8.0) is not None
    assert _vram_budget_mismatch(10.0, 8.0) is None
    assert _vram_budget_mismatch(64.0, None) is None
    assert _vram_budget_mismatch(64.0, 64.0) is None


def test_save_config_is_atomic_and_keeps_backup(tmp_path: Path) -> None:
    from app.config import save_config

    target = tmp_path / "broker_config.yaml"
    first = BrokerConfig()
    save_config(first, target)
    assert target.exists()
    assert not (tmp_path / "broker_config.yaml.tmp").exists()

    second = BrokerConfig(server=ServerConfig(port=9999))
    save_config(second, target)
    backup = tmp_path / "broker_config.yaml.bak"
    assert backup.exists()
    assert load_config(target).server.port == 9999
    # El .bak conserva la versión anterior íntegra y parseable.
    assert load_config(backup).server.port == 8080


def test_server_config_rejects_multiple_workers() -> None:
    with pytest.raises(ValueError):
        ServerConfig(workers=2)
    assert ServerConfig(workers=1).workers == 1


def test_admin_cookie_expires_server_side(tmp_path: Path, monkeypatch) -> None:
    from app.admin_auth import ADMIN_SESSION_SECONDS, admin_cookie_value

    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "secreto-admin")
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"idempotency_key": "admin:expiry", "content": {"prompt": "Tarea"}},
            headers={"X-Admin-Token": "secreto-admin"},
        )
        task_id = created.json()["task_id"]
        token = dashboard_csrf(client)

        stale = admin_cookie_value("secreto-admin", timestamp=time.time() - ADMIN_SESSION_SECONDS - 60)
        client.cookies.set("ai_broker_dashboard_admin", stale)
        expired = client.post(
            f"/dashboard/actions/tasks/{task_id}/cancel",
            headers={"X-CSRF-Token": token},
        )
        assert expired.status_code == 403

        fresh = admin_cookie_value("secreto-admin")
        client.cookies.set("ai_broker_dashboard_admin", fresh)
        allowed = client.post(
            f"/dashboard/actions/tasks/{task_id}/cancel",
            headers={"X-CSRF-Token": token},
        )
        assert allowed.status_code == 204


def test_admin_login_rate_limited_after_repeated_failures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "secreto-admin")
    with make_client(tmp_path) as client:
        token = dashboard_csrf(client)
        for _ in range(5):
            attempt = client.post(
                "/dashboard/actions/login",
                data={"csrf_token": token, "admin_token": "incorrecto"},
            )
            assert attempt.status_code == 403
        blocked = client.post(
            "/dashboard/actions/login",
            data={"csrf_token": token, "admin_token": "incorrecto"},
        )
        assert blocked.status_code == 429
        # Incluso con el token correcto sigue bloqueado hasta que pase el backoff.
        still_blocked = client.post(
            "/dashboard/actions/login",
            data={"csrf_token": token, "admin_token": "secreto-admin"},
        )
        assert still_blocked.status_code == 429


def test_single_strategy_retries_transient_provider_errors(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        provider = client.app.state.provider
        original_propose = provider.propose
        calls = {"count": 0}

        async def flaky_propose(request, model, ordinal):
            calls["count"] += 1
            if calls["count"] == 1:
                raise providers_module.ProviderError("PROVIDER_UNAVAILABLE", "blip transitorio", retryable=True)
            return await original_propose(request, model, ordinal)

        provider.propose = flaky_propose
        try:
            created = client.post(
                "/api/v1/tasks",
                json={"idempotency_key": "retry:single", "content": {"prompt": "Hola"}},
            )
            task_id = created.json()["task_id"]
            client.post("/api/v1/dispatcher/tick")
            final = client.get(f"/api/v1/tasks/{task_id}").json()
            assert final["status"] == "completed"
            assert calls["count"] == 2
        finally:
            provider.propose = original_propose


def test_single_strategy_does_not_retry_permanent_errors(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        provider = client.app.state.provider
        original_propose = provider.propose
        calls = {"count": 0}

        async def broken_propose(request, model, ordinal):
            calls["count"] += 1
            raise providers_module.ProviderError("MODEL_UNAVAILABLE", "no existe", retryable=False)

        provider.propose = broken_propose
        try:
            created = client.post(
                "/api/v1/tasks",
                json={"idempotency_key": "retry:permanent", "content": {"prompt": "Hola"}},
            )
            task_id = created.json()["task_id"]
            client.post("/api/v1/dispatcher/tick")
            final = client.get(f"/api/v1/tasks/{task_id}").json()
            assert final["status"] == "failed"
            assert calls["count"] == 1
        finally:
            provider.propose = original_propose


def test_consensus_completes_even_if_artifact_writes_fail(tmp_path: Path) -> None:
    payload = {
        "idempotency_key": "artifacts:fail",
        "content": {"prompt": "Compara dos enfoques"},
        "execution": {
            "strategy": "mixture_of_agents",
            "preset": "fast",
            "max_proposers": 3,
            "selection": {"mode": "auto", "proposer_count": 3},
        },
    }
    with make_client(tmp_path) as client:
        coordinator = client.app.state.coordinator

        def broken_write(*args, **kwargs):
            raise OSError("disco lleno")

        coordinator.artifacts.write_markdown = broken_write  # type: ignore[method-assign]
        coordinator.artifacts.write_text = broken_write  # type: ignore[method-assign]

        created = client.post("/api/v1/tasks", json=payload).json()
        client.post("/api/v1/dispatcher/tick")
        task = client.get(created["status_url"]).json()

        # La inferencia ya está pagada: el resultado se persiste en BD aunque el disco falle.
        assert task["status"] == "completed"
        assert task["result"]["result_markdown"]
        assert task["result"]["consensus"]["proposers_completed"] == 3


def test_prune_terminal_task_artifacts(tmp_path: Path) -> None:
    from app.db import Database, dumps_json
    from app.maintenance import prune_terminal_task_artifacts

    db = Database(tmp_path / "artifacts.db")
    db.init_schema()
    artifacts_root = tmp_path / "tasks"
    old_file = artifacts_root / "t-old" / "final.md"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("resultado", encoding="utf-8")

    old = "2024-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO tasks(id, request_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("t-old", dumps_json({}), "completed", old, old),
    )
    db.execute(
        "INSERT INTO artifacts(id, task_id, artifact_type, path, sha256, size_bytes, created_at) "
        "VALUES ('a1', 't-old', 'final_output', ?, 'x', 9, ?)",
        (str(old_file), old),
    )

    assert prune_terminal_task_artifacts(db, artifacts_root, older_than_days=30) == 1
    assert not old_file.exists()
    assert db.query_one("SELECT COUNT(*) AS n FROM artifacts")["n"] == 0
    assert prune_terminal_task_artifacts(db, artifacts_root, older_than_days=0) == 0
    db.close()
