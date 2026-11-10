"""Cobertura en proceso de dos módulos que el resto de la suite solo toca de
lado: el worker de conversión (los tests lo lanzan como subproceso, invisible
para coverage) y el sandbox (los tests reales exigen Docker en marcha).

Aquí el worker se ejecuta llamando a main() con stdin/stdout sustituidos, y el
sandbox contra un `asyncio.create_subprocess_exec` falso que imita al CLI de
Docker: así se ejercen los caminos de error (Docker ausente, daemon parado,
plazos agotados, limpieza) que con un Docker real no se pueden provocar.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest

from app.config import BrokerConfig, IngestionConfig, SandboxConfig
from app.ingestion import engines, worker
from app.ingestion.conversion import ConversionResult, dump_settings
from app.sandbox import SandboxError, SandboxExecutor

# ------------------------------------------------------------------ worker


def _run_main(monkeypatch: pytest.MonkeyPatch, stdin_text: str) -> tuple[int, list[dict[str, Any]]]:
    stdout = io.StringIO()
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(worker.sys, "stdout", stdout)
    code = worker.main()
    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    return code, events


def _job(tmp_path: Path) -> str:
    source = {
        "file_id": "f1",
        "filename": "nota.txt",
        "extension": ".txt",
        "kind": "text",
        "original_path": str(tmp_path / "nota.txt"),
    }
    return json.dumps({"source": source, "ingestion": dump_settings(IngestionConfig())})


def test_worker_emits_progress_and_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_convert(source, settings, on_progress):
        on_progress({"step": "leyendo"})
        return ConversionResult(markdown="# hola ∫ —", meta={"pages": 1})

    monkeypatch.setattr(worker, "convert_file", fake_convert)
    code, events = _run_main(monkeypatch, _job(tmp_path))
    assert code == 0
    assert events[0] == {"event": "progress", "data": {"step": "leyendo"}}
    assert events[-1]["event"] == "result"
    assert events[-1]["data"]["markdown"] == "# hola ∫ —"


def test_worker_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    code, events = _run_main(monkeypatch, "{no es json")
    assert code == 2
    assert events == [events[0]] and events[0]["code"] == "WORKER_BAD_INPUT"


def test_worker_rejects_broken_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    code, events = _run_main(monkeypatch, json.dumps({"source": {"roto": True}, "ingestion": {}}))
    assert code == 2
    assert events[0]["code"] == "WORKER_BAD_INPUT"


def test_worker_empty_stdin_is_bad_input(monkeypatch: pytest.MonkeyPatch) -> None:
    code, events = _run_main(monkeypatch, "")
    assert code == 2
    assert events[0]["code"] == "WORKER_BAD_INPUT"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (engines.EngineMissing("docling no instalado"), "ENGINE_MISSING"),
        (ValueError("pdf corrupto"), "CONVERSION_FAILED"),
    ],
)
def test_worker_translates_conversion_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, expected_code: str
) -> None:
    def failing_convert(source, settings, on_progress):
        raise error

    monkeypatch.setattr(worker, "convert_file", failing_convert)
    code, events = _run_main(monkeypatch, _job(tmp_path))
    assert code == 1
    assert events[-1]["code"] == expected_code
    assert str(error) in events[-1]["message"]


def test_worker_reports_result_that_cannot_be_emitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Unserializable:
        pass

    def convert_with_bad_meta(source, settings, on_progress):
        return ConversionResult(markdown="ok", meta={"objeto": Unserializable()})

    monkeypatch.setattr(worker, "convert_file", convert_with_bad_meta)
    code, events = _run_main(monkeypatch, _job(tmp_path))
    assert code == 1
    assert events[-1]["code"] == "RESULT_EMIT_FAILED"


def test_use_utf8_tolerates_streams_without_reconfigure() -> None:
    worker._use_utf8(object())  # no debe lanzar


# ------------------------------------------------------------------ sandbox


class FakeProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", hang: bool = False) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self.stdin_data: bytes | None = None

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_data = data
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    async def wait(self) -> int:
        if self._hang:
            await asyncio.sleep(3600)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class FakeDocker:
    """Sustituto de create_subprocess_exec: responde según el subcomando."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *command: str, **_: Any) -> FakeProcess:
        self.calls.append(list(command))
        key = command[1] if command[1] != "volume" else f"volume {command[2]}"
        response = self.responses.get(key, FakeProcess())
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        self.processes.append(response)
        return response


@pytest.fixture
def executor() -> SandboxExecutor:
    return SandboxExecutor(BrokerConfig(sandbox=SandboxConfig(enabled=True, timeout_seconds=5)))


@pytest.fixture
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sandbox as sandbox

    monkeypatch.setattr(sandbox, "_STARTUP_MARGIN_SECONDS", -4.95)
    monkeypatch.setattr(sandbox, "_MANAGEMENT_TIMEOUT_SECONDS", 0.05)


def _install(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> FakeDocker:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", docker)
    return docker


def test_plain_run_returns_output_and_feeds_code(monkeypatch, executor) -> None:
    docker = _install(monkeypatch, FakeDocker({"run": FakeProcess(0, b"42\n")}))
    result = asyncio.run(executor.run_python("print(42)"))
    assert result == "42"
    assert docker.processes[0].stdin_data == b"print(42)"


def test_plain_run_reports_stopped_daemon(monkeypatch, executor) -> None:
    stderr = b"error during connect: open //./pipe/docker_engine"
    _install(monkeypatch, FakeDocker({"run": FakeProcess(1, b"", stderr)}))
    with pytest.raises(SandboxError, match="Docker no responde"):
        asyncio.run(executor.run_python("print(1)"))


def test_plain_run_code_error_is_a_result_not_an_exception(monkeypatch, executor) -> None:
    _install(monkeypatch, FakeDocker({"run": FakeProcess(1, b"", b"ZeroDivisionError")}))
    result = asyncio.run(executor.run_python("1/0"))
    assert "exit code 1" in result and "ZeroDivisionError" in result


def test_plain_run_missing_docker(monkeypatch, executor) -> None:
    _install(monkeypatch, FakeDocker({"run": FileNotFoundError()}))
    with pytest.raises(SandboxError, match="docker no encontrado"):
        asyncio.run(executor.run_python("print(1)"))


def test_plain_run_timeout_kills_and_removes(monkeypatch, executor, fast_timeouts) -> None:
    docker = _install(monkeypatch, FakeDocker({"run": FakeProcess(hang=True)}))
    with pytest.raises(SandboxError, match="superó el plazo"):
        asyncio.run(executor.run_python("while True: pass"))
    assert docker.processes[0].killed
    assert any(call[1] == "rm" for call in docker.calls)


def test_run_with_files_stages_and_cleans_up(monkeypatch, executor, tmp_path: Path) -> None:
    data = tmp_path / "datos.csv"
    data.write_text("a,b\n1,2\n", encoding="utf-8")
    docker = _install(monkeypatch, FakeDocker({"attach": FakeProcess(0, b"3\n")}))
    files = {"f1_datos.csv": data, "../../": data}
    result = asyncio.run(executor.run_python("print(3)", files=files))
    assert result == "3"
    subcommands = [" ".join(call[1:3]) for call in docker.calls]
    assert subcommands[0] == "volume create"
    cp_calls = [call for call in docker.calls if call[1] == "cp"]
    # El nombre vacío tras quitar la ruta se descarta; nada sale de /work/attachments.
    assert len(cp_calls) == 1
    assert cp_calls[0][-1].endswith(":/work/attachments/f1_datos.csv")
    assert any(call[1] == "rm" for call in docker.calls)
    assert any(call[1:3] == ["volume", "rm"] for call in docker.calls)


def test_run_with_files_preparation_failure(monkeypatch, executor, tmp_path: Path) -> None:
    docker = _install(monkeypatch, FakeDocker({"create": FakeProcess(1, b"", b"no such image")}))
    with pytest.raises(SandboxError, match="fallo preparando el sandbox: no such image"):
        asyncio.run(executor.run_python("print(1)", files={"a.csv": tmp_path / "a.csv"}))
    # La limpieza se hace aunque falle la preparación.
    assert any(call[1:3] == ["volume", "rm"] for call in docker.calls)


def test_management_command_daemon_down(monkeypatch, executor, tmp_path: Path) -> None:
    stderr = b"Cannot connect to the Docker daemon"
    _install(monkeypatch, FakeDocker({"volume create": FakeProcess(1, b"", stderr)}))
    with pytest.raises(SandboxError, match="Docker no responde"):
        asyncio.run(executor.run_python("print(1)", files={"a.csv": tmp_path / "a.csv"}))


def test_management_command_missing_docker(monkeypatch, executor, tmp_path: Path) -> None:
    _install(monkeypatch, FakeDocker({"volume create": FileNotFoundError()}))
    with pytest.raises(SandboxError, match="docker no encontrado"):
        asyncio.run(executor.run_python("print(1)", files={"a.csv": tmp_path / "a.csv"}))


def test_management_command_timeout(monkeypatch, executor, fast_timeouts, tmp_path: Path) -> None:
    docker = _install(monkeypatch, FakeDocker({"start": FakeProcess(hang=True)}))
    with pytest.raises(SandboxError, match="preparación del sandbox superó"):
        asyncio.run(executor.run_python("print(1)", files={"a.csv": tmp_path / "a.csv"}))
    assert any(process.killed for process in docker.processes)


def test_attach_errors(monkeypatch, executor, fast_timeouts, tmp_path: Path) -> None:
    files = {"a.csv": tmp_path / "a.csv"}
    _install(monkeypatch, FakeDocker({"attach": FileNotFoundError()}))
    with pytest.raises(SandboxError, match="docker no encontrado"):
        asyncio.run(executor.run_python("print(1)", files=files))

    _install(monkeypatch, FakeDocker({"attach": FakeProcess(1, b"", b"docker daemon is not running")}))
    with pytest.raises(SandboxError, match="Docker no responde"):
        asyncio.run(executor.run_python("print(1)", files=files))

    _install(monkeypatch, FakeDocker({"attach": FakeProcess(hang=True)}))
    with pytest.raises(SandboxError, match="superó el plazo"):
        asyncio.run(executor.run_python("print(1)", files=files))


def test_force_remove_logs_cleanup_failures(monkeypatch, executor, caplog) -> None:
    _install(monkeypatch, FakeDocker({"rm": OSError("sin docker"), "volume rm": OSError("sin docker")}))
    with caplog.at_level("WARNING", logger="ai_broker.sandbox"):
        asyncio.run(executor._force_remove("sbx", volume_name="vol"))
    assert [record.getMessage() for record in caplog.records].count("sandbox.cleanup_failed") == 2
