import asyncio
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient

import app.dashboard_web as dashboard_web
from app.config import BrokerConfig, LoggingConfig, PersistenceConfig, ProcessingConfig, load_config
from app.dashboard_web import load_dashboard_resources
from app.maintenance import create_state_backup, restore_state_backup, verify_state_backup
from app.main import create_app
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


class FailingSynthesisProvider(TimelineProbeProvider):
    async def synthesize(self, request, model, proposals):
        raise ProviderError("MODEL_ERROR", "fallo controlado", retryable=True)


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
    assert fragment.status_code == 200
    assert first["task_id"] in fragment.text
    assert moved.status_code == 204
    assert moved.headers["hx-trigger"] == "dashboard-refresh"
    assert reordered[0]["task_id"] == second["task_id"]
    assert cancelled.status_code == 204
    assert css.status_code == 200
    assert "--teal" in css.text
    assert script.status_code == 200
    assert "refreshDashboard" in script.text


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
    assert saved.providers.custom[0].models[0].name == "meta/llama-3.1-70b-instruct"


def test_dashboard_provider_probe_persists_model_compatibility(tmp_path: Path, monkeypatch) -> None:
    class FakeProbeProvider:
        def __init__(self, config):
            self.config = config

        async def probe_all_models(self):
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
                "custom_provider_1_input_cost_per_million": "0",
                "custom_provider_1_output_cost_per_million": "0",
                "custom_provider_1_sync_models": "on",
            },
        )

    saved = load_config(config_path)
    by_name = {item.name: item for item in saved.providers.custom[0].models}
    assert response.status_code == 200
    assert by_name["chat-ok"].compatibility == "compatible"
    assert by_name["vision-only"].compatibility == "incompatible"
    assert saved.providers.custom[0].probe_skip_compatible is True
    assert "No compatible mixture" in response.text


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
        for index in range(40):
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
    assert "workers=1" in runner.read_text(encoding="utf-8")


def test_dashboard_resources_degrade_without_breaking_the_panel() -> None:
    config = BrokerConfig(processing=ProcessingConfig(provider_mode="bootstrap", auto_dispatch=False))
    resources = asyncio.run(
        load_dashboard_resources(UnavailableResourceProvider(), ResourceScheduler(config), config)
    )

    assert resources.status == "unavailable"
    assert resources.used_vram_bytes == 0
    assert resources.detail == "PROVIDER_UNAVAILABLE: snapshot de recursos no disponible"


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
