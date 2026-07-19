import asyncio
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig, SandboxConfig
from app.main import create_app
from app.sandbox import SandboxError, SandboxExecutor
from app.skills import run_skill


def make_client(tmp_path: Path, sandbox_enabled: bool) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        sandbox=SandboxConfig(enabled=sandbox_enabled),
    )
    return TestClient(create_app(config))


def make_executor(**sandbox_kwargs) -> SandboxExecutor:
    sandbox_kwargs.setdefault("enabled", True)
    return SandboxExecutor(BrokerConfig(sandbox=SandboxConfig(**sandbox_kwargs)))


# ------------------------------------------------------------------- ejecutor

def test_docker_command_enforces_isolation_flags():
    executor = make_executor(memory_mb=512, cpus=1.5, timeout_seconds=30)
    command = executor._docker_command("sbx-test")
    joined = " ".join(command)
    # Fronteras no negociables del sandbox.
    assert "--network none" in joined
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--user 65534:65534" in joined
    assert "--memory 512m" in joined and "--memory-swap 512m" in joined
    assert "--pids-limit" in joined
    # Sin montajes del host: ningún -v ni --mount.
    assert "-v" not in command and "--mount" not in command
    # Timeout interno + código por stdin.
    assert command[-4:] == ["timeout", "30s", "python", "-I", "-B", "-"][-4:]


def test_format_result_reports_exit_and_truncates():
    executor = make_executor(max_output_chars=500)
    text = executor._format_result(1, "parcial", "Traceback: boom")
    assert "exit code 1" in text
    assert "[stderr]" in text and "boom" in text
    truncated = executor._format_result(0, "x" * 2000, "")
    assert len(truncated) <= 500 + len("\n[...salida truncada...]")
    assert truncated.endswith("[...salida truncada...]")


def test_format_result_timeout_and_empty():
    executor = make_executor(timeout_seconds=30)
    assert "límite de 30s" in executor._format_result(124, "", "")
    assert "print()" in executor._format_result(0, "", "")


def test_run_python_rejects_empty_code():
    executor = make_executor()
    with pytest.raises(SandboxError):
        asyncio.run(executor.run_python("   "))


def test_missing_docker_binary_raises_sandbox_error():
    executor = make_executor(docker_path="docker-inexistente-xyz")
    with pytest.raises(SandboxError, match="docker no encontrado"):
        asyncio.run(executor.run_python("print(1)"))


def test_disabled_sandbox_raises_and_live_toggle_applies():
    executor = make_executor(enabled=False)
    with pytest.raises(SandboxError, match="desactivado"):
        asyncio.run(executor.run_python("print(1)"))
    # El ejecutor lee la config en vivo: reemplazar la sección (lo que hace el
    # panel de Configuración) cambia su comportamiento sin reconstruirlo.
    executor.config.sandbox = SandboxConfig(enabled=True, docker_path="docker-inexistente-xyz")
    with pytest.raises(SandboxError, match="docker no encontrado"):
        asyncio.run(executor.run_python("print(1)"))


def test_config_form_updates_sandbox_and_ingestion():
    from app.dashboard_forms import _apply_config_update, _build_dashboard_config

    current = BrokerConfig()
    form = {
        # Campos base obligatorios del formulario de configuración.
        "task_timeout_seconds": "300", "queue_max_size": "1000",
        "max_parallel_invocations": "auto", "local_vram_budget_gb": "64",
        "vram_safety_margin_gb": "2", "max_loaded_local_models": "auto",
        "allow_execution_waves": "on", "prompt_compression_enabled": "on",
        "prompt_compression_level": "medium", "prompt_compression_min_chars": "40",
        # Sección sandbox.
        "sandbox_enabled": "on", "sandbox_image": "ai-broker-sandbox:latest",
        "sandbox_docker_path": "docker", "sandbox_timeout_seconds": "90",
        "sandbox_memory_mb": "2048", "sandbox_cpus": "4",
        # Sección ingesta.
        "ingestion_enabled": "on", "ingestion_max_file_mb": "200",
        "ingestion_ocr_enabled": "on", "ingestion_conversion_timeout_seconds": "1800",
        "ingestion_images_enabled": "on",
        "ingestion_images_base_url": "http://127.0.0.1:1234/v1/",
        "ingestion_images_model": "google/gemma-4-31b-qat",
        "ingestion_transcription_enabled": "on", "ingestion_whisper_model": "small",
        "ingestion_ffmpeg_path": "C:/ffmpeg/bin/ffmpeg.exe",
    }
    updated = _build_dashboard_config(current, form)
    assert updated.sandbox.enabled is True
    assert updated.sandbox.image == "ai-broker-sandbox:latest"
    assert updated.sandbox.timeout_seconds == 90
    assert updated.sandbox.memory_mb == 2048
    assert updated.ingestion.images.base_url == "http://127.0.0.1:1234/v1"
    assert updated.ingestion.transcription.model_size == "small"
    # Sin la sección en el formulario, la config previa no se toca.
    partial = {key: value for key, value in form.items() if not key.startswith(("sandbox_", "ingestion_"))}
    untouched = _build_dashboard_config(updated, partial)
    assert untouched.sandbox == updated.sandbox
    assert untouched.ingestion == updated.ingestion
    # La aplicación reemplaza las secciones en el BrokerConfig compartido.
    _apply_config_update(current, updated)
    assert current.sandbox.enabled is True
    assert current.ingestion.images.model == "google/gemma-4-31b-qat"


# ---------------------------------------------------------------------- skill

def test_run_skill_without_sandbox_returns_clear_error():
    result = asyncio.run(run_skill("run_code", {"code": "print(1)"}, sandbox=None))
    assert result.startswith("ERROR de run_code")
    assert "sandbox" in result


class FakeSandbox:
    async def run_python(self, code: str) -> str:
        return f"ejecutado:{len(code)}"


def test_run_skill_delegates_to_sandbox():
    result = asyncio.run(run_skill("run_code", {"code": "print('hola')"}, sandbox=FakeSandbox()))
    assert result == "ejecutado:13"


def test_run_skill_requires_code_argument():
    result = asyncio.run(run_skill("run_code", {}, sandbox=FakeSandbox()))
    assert result.startswith("ERROR de run_code")


# ------------------------------------------------------------------- contrato

def test_default_agent_skills_exclude_run_code():
    from app.schemas import AgentExecutionConfig

    assert "run_code" not in AgentExecutionConfig().skills
    explicit = AgentExecutionConfig(skills=["run_code"])
    assert explicit.skills == ["run_code"]


def test_capabilities_reflect_sandbox_state(tmp_path):
    with make_client(tmp_path, sandbox_enabled=False) as client:
        caps = client.get("/api/v1/capabilities").json()
        assert caps["sandbox_run_code"] is False
        assert "run_code" not in caps["agent_skills"]
    with make_client(tmp_path / "on", sandbox_enabled=True) as client:
        caps = client.get("/api/v1/capabilities").json()
        assert caps["sandbox_run_code"] is True
        assert "run_code" in caps["agent_skills"]


def test_task_with_run_code_rejected_when_sandbox_disabled(tmp_path):
    payload = {
        "idempotency_key": "sandbox:rejected",
        "content": {"prompt": "usa run_code"},
        "execution": {"strategy": "agent", "agent": {"skills": ["run_code"]}},
    }
    with make_client(tmp_path, sandbox_enabled=False) as client:
        response = client.post("/api/v1/tasks", json=payload)
        assert response.status_code == 409
        assert response.json()["detail"] == "SANDBOX_DISABLED"
    with make_client(tmp_path / "on", sandbox_enabled=True) as client:
        response = client.post("/api/v1/tasks", json=payload)
        assert response.status_code == 202


def test_tester_hides_and_rejects_run_code_without_sandbox(tmp_path):
    import json as jsonlib
    bootstrap_model = jsonlib.dumps(
        {"provider": "ollama", "deployment": "bootstrap", "model": "bootstrap-agent"},
    )
    with make_client(tmp_path, sandbox_enabled=False) as client:
        page = client.get("/dashboard/prompt-tester")
        assert "agent_skill_run_code" not in page.text
        token = client.cookies.get("ai_broker_dashboard_csrf")
        response = client.post(
            "/dashboard/actions/prompt-tester",
            data={
                "csrf_token": token,
                "action": "enqueue",
                "prompt": "prueba",
                "strategy": "agent",
                "agent_model": bootstrap_model,
                "agent_skill_run_code": "on",
            },
        )
        assert response.status_code == 200
        assert "sandbox" in response.text
        assert client.get("/api/v1/queue").json()["pending"] == []
    with make_client(tmp_path / "on", sandbox_enabled=True) as client:
        page = client.get("/dashboard/prompt-tester")
        assert "agent_skill_run_code" in page.text


# ------------------------------------------------- integración real (opt-in)

docker_ready = shutil.which("docker") is not None and os.environ.get("AI_BROKER_SANDBOX_DOCKER") == "1"


@pytest.mark.skipif(not docker_ready, reason="requiere Docker en marcha y AI_BROKER_SANDBOX_DOCKER=1")
def test_real_docker_execution_and_isolation():
    executor = make_executor(timeout_seconds=30)
    result = asyncio.run(executor.run_python("print(6 * 7)"))
    assert "42" in result
    # Sin red: cualquier intento de conexión debe fallar dentro del contenedor.
    network_probe = asyncio.run(executor.run_python(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('RED-ABIERTA')\n"
        "except OSError:\n"
        "    print('RED-BLOQUEADA')\n"
    ))
    assert "RED-BLOQUEADA" in network_probe
    # Rootfs de solo lectura.
    fs_probe = asyncio.run(executor.run_python(
        "try:\n"
        "    open('/etc/prueba', 'w')\n"
        "    print('ESCRITURA-PERMITIDA')\n"
        "except OSError:\n"
        "    print('ESCRITURA-BLOQUEADA')\n"
    ))
    assert "ESCRITURA-BLOQUEADA" in fs_probe
