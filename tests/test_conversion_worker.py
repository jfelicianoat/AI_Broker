"""Conversión aislada en proceso hijo (app.ingestion.worker).

El resto de la batería de ingesta corre con `isolate_conversions=False`, porque
sustituye los motores por dobles dentro del propio proceso y un hijo real no
vería esos monkeypatch. Aquí se cubre el camino que de verdad se despliega:
proceso hijo, protocolo de líneas JSON, y la propiedad que motivó todo el
trabajo — que agotar el plazo MATA la conversión en vez de dejarla corriendo
por detrás.

Se usan ficheros de texto a propósito: el motor passthrough no arrastra Docling
ni torch, así que estos casos son rápidos y no dependen de extras opcionales.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import BrokerConfig, IngestionConfig, PersistenceConfig
from app.db import Database
from app.ingestion.conversion import ConversionInput, dump_settings
from app.ingestion.service import IngestionService

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_worker(job: dict) -> tuple[int, list[dict]]:
    """Ejecuta el hijo tal cual lo lanza el servicio y devuelve sus eventos."""
    completed = subprocess.run(
        [sys.executable, "-m", "app.ingestion.worker"],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        timeout=120,
    )
    events = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    return completed.returncode, events


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _job(self, source: ConversionInput) -> dict:
        return {
            "source": source.to_json(),
            "ingestion": dump_settings(IngestionConfig(storage_dir=str(self.tmp / "files"))),
        }

    def test_the_child_returns_the_markdown_on_its_result_line(self) -> None:
        path = self.tmp / "nota.md"
        path.write_text("# Titulo\n\ncuerpo", encoding="utf-8")
        source = ConversionInput("file_1", "nota.md", ".md", "text", str(path))

        code, events = _run_worker(self._job(source))

        self.assertEqual(code, 0)
        results = [event for event in events if event["event"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertIn("# Titulo", results[0]["data"]["markdown"])

    def test_the_child_reports_a_failure_with_its_code(self) -> None:
        source = ConversionInput("file_2", "fantasma.md", ".md", "text", str(self.tmp / "no-existe.md"))

        code, events = _run_worker(self._job(source))

        self.assertEqual(code, 1)
        errors = [event for event in events if event["event"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], "CONVERSION_FAILED")

    def test_bad_input_is_rejected_without_pretending_to_convert(self) -> None:
        code, events = _run_worker({"source": {"roto": True}, "ingestion": {}})

        self.assertEqual(code, 2)
        self.assertEqual([event["event"] for event in events], ["error"])
        self.assertEqual(events[0]["code"], "WORKER_BAD_INPUT")


class IsolatedConversionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = Database(self.tmp / "broker.db")
        self.db.init_schema()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _service(self, **overrides) -> IngestionService:
        config = BrokerConfig(
            persistence=PersistenceConfig(database=str(self.tmp / "broker.db")),
            ingestion=IngestionConfig(
                storage_dir=str(self.tmp / "files"), isolate_conversions=True, **overrides,
            ),
        )
        return IngestionService(self.db, config)

    async def test_a_file_converts_end_to_end_in_a_child_process(self) -> None:
        service = self._service()
        record, _ = service.store_upload("nota.md", b"# Titulo\n\ncuerpo")

        await service.process(record.id)

        updated = service.get(record.id)
        assert updated is not None
        self.assertEqual(updated.status, "ready")
        self.assertIn("# Titulo", Path(updated.markdown_path).read_text(encoding="utf-8"))

    async def test_the_deadline_kills_the_conversion_instead_of_letting_it_run(self) -> None:
        """La propiedad que `asyncio.to_thread` no podía dar: al agotarse el
        plazo el trabajo se PARA. Antes, cancelar la espera dejaba a Docling
        dentro del broker consumiendo CPU hasta terminar.

        Se fuerza con un plazo imposible: ni siquiera da tiempo a que arranque
        un intérprete nuevo. Que el test termine en vez de colgarse demuestra
        además que al hijo se le espera tras matarlo, sin dejar zombis."""
        service = self._service()
        record, _ = service.store_upload("otra.md", b"# Titulo")

        source = ConversionInput.from_record(record)
        with self.assertRaises(TimeoutError):
            await service._run_conversion_subprocess(record, source, timeout=0.01)

    async def test_a_timeout_marks_the_file_failed_with_its_code(self) -> None:
        service = self._service()
        record, _ = service.store_upload("lenta.md", b"# Titulo")

        async def _too_slow(*args, **kwargs):
            raise TimeoutError

        service._run_conversion = _too_slow  # type: ignore[method-assign]

        await service.process(record.id)

        updated = service.get(record.id)
        assert updated is not None
        self.assertEqual(updated.status, "failed")
        assert updated.error is not None
        self.assertEqual(updated.error["code"], "CONVERSION_TIMEOUT")
