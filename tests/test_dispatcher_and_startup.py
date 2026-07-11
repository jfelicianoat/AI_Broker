"""Cobertura del bucle de despacho y de los helpers de arranque."""
import asyncio
import logging
import unittest

from app.dispatcher import dispatcher_loop
from app.schemas import TaskStatus
from app.startup import auto_start_local_provider_servers, ensure_lmstudio_server, run_process


class _RecordingRepository:
    """Repositorio mínimo: entrega una tarea y registra los cambios de estado."""

    def __init__(self, task_ids):
        self._pending = list(task_ids)
        self.updates = []

    def claim_next_queued_task_id(self):
        return self._pending.pop(0) if self._pending else None

    def get_task(self, task_id):
        class _State:
            status = TaskStatus.generating

        return _State()

    def update_task(self, task_id, status, **kwargs):
        self.updates.append((task_id, status, kwargs.get("error")))


class DispatcherLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_crash_marks_task_failed_and_loop_survives(self) -> None:
        repository = _RecordingRepository(["task-1"])
        stop = asyncio.Event()

        class ExplodingCoordinator:
            async def process_task(self, repo, task_id):
                # El bucle debe capturar cualquier excepción: sin esto una tarea
                # envenenada mataría el despacho con el servidor "sano".
                stop.set()
                raise RuntimeError("boom")

        await asyncio.wait_for(
            dispatcher_loop(repository, ExplodingCoordinator(), stop, interval_seconds=0.01),
            timeout=5,
        )
        task_id, status, error = repository.updates[-1]
        self.assertEqual(task_id, "task-1")
        self.assertEqual(status, TaskStatus.failed)
        self.assertEqual(error["code"], "INTERNAL_ERROR")

    async def test_terminal_task_is_not_overwritten_after_crash(self) -> None:
        stop = asyncio.Event()

        class _Repo(_RecordingRepository):
            def get_task(self, task_id):
                class _State:
                    status = TaskStatus.completed

                return _State()

        repository = _Repo(["task-2"])

        class ExplodingCoordinator:
            async def process_task(self, repo, task_id):
                stop.set()
                raise RuntimeError("boom tras completar")

        await asyncio.wait_for(
            dispatcher_loop(repository, ExplodingCoordinator(), stop, interval_seconds=0.01),
            timeout=5,
        )
        # La tarea ya era terminal: el bucle no debe reescribirla como failed.
        self.assertEqual(repository.updates, [])

    async def test_claim_failure_logs_and_loop_continues(self) -> None:
        stop = asyncio.Event()
        attempts = {"count": 0}

        class _BrokenClaimRepo:
            def claim_next_queued_task_id(self):
                attempts["count"] += 1
                if attempts["count"] >= 2:
                    stop.set()
                raise RuntimeError("sqlite bloqueada")

        with self.assertLogs("ai_broker.dispatcher", level="ERROR"):
            await asyncio.wait_for(
                dispatcher_loop(_BrokenClaimRepo(), None, stop, interval_seconds=0.01),
                timeout=5,
            )
        self.assertGreaterEqual(attempts["count"], 2)


class RunProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_executable_returns_127_without_raising(self) -> None:
        result = await run_process(["ejecutable-que-no-existe-xyz"], timeout_seconds=5)
        self.assertEqual(result["returncode"], 127)
        self.assertIn("No se encontro el ejecutable", result["stderr"])


class AutoStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_start_skips_remote_and_unsupported_providers(self) -> None:
        from app.config import BrokerConfig, OpenAICompatibleProviderConfig, ProvidersConfig

        config = BrokerConfig(
            providers=ProvidersConfig(
                custom=[
                    OpenAICompatibleProviderConfig(
                        id="nvidia", enabled=True, auto_start=True,
                        base_url="https://integrate.api.nvidia.com/v1", deployment="api",
                    ),
                    OpenAICompatibleProviderConfig(
                        id="otro-local", enabled=True, auto_start=True,
                        base_url="http://127.0.0.1:9999/v1", deployment="local",
                    ),
                    OpenAICompatibleProviderConfig(
                        id="apagado", enabled=False, auto_start=True,
                        base_url="http://127.0.0.1:8888/v1", deployment="local",
                    ),
                ]
            )
        )
        logger = logging.getLogger("test.autostart")
        # Ninguno es LM Studio local habilitado: no debe intentarse ningún
        # arranque (si lo intentara, fallaría al no haber monkeypatch).
        with self.assertLogs("test.autostart", level="WARNING") as captured:
            await auto_start_local_provider_servers(config, logger)
        self.assertEqual(len(captured.records), 2)  # remoto + no soportado

    async def test_lmstudio_start_failure_only_warns(self) -> None:
        import app.startup as startup_module

        calls = []

        async def failing_run_process(args, *, timeout_seconds):
            calls.append(args)
            if args == ["lms", "server", "status"]:
                return {"returncode": 0, "stdout": "The server is not running.", "stderr": ""}
            return {"returncode": 1, "stdout": "", "stderr": "no se pudo arrancar"}

        original = startup_module.run_process
        startup_module.run_process = failing_run_process
        try:
            logger = logging.getLogger("test.lmstudio.fail")
            with self.assertLogs("test.lmstudio.fail", level="WARNING"):
                await ensure_lmstudio_server("http://127.0.0.1:1234/v1", logger)
        finally:
            startup_module.run_process = original
        self.assertEqual(len(calls), 2)

    async def test_lmstudio_already_running_skips_start(self) -> None:
        import app.startup as startup_module

        calls = []

        async def running_run_process(args, *, timeout_seconds):
            calls.append(args)
            return {"returncode": 0, "stdout": "Server is running on port 1234", "stderr": ""}

        original = startup_module.run_process
        startup_module.run_process = running_run_process
        try:
            logger = logging.getLogger("test.lmstudio.running")
            await ensure_lmstudio_server("http://127.0.0.1:1234/v1", logger)
        finally:
            startup_module.run_process = original
        self.assertEqual(calls, [["lms", "server", "status"]])
