"""Sondeo en sombra (app.shadow_probe).

Lo crítico que se protege aquí: que el sondeo mida sin hacer esperar a nadie,
que no se salte la frontera de datos de la tarea que lo origina, y que no meta
trabajo local en una máquina ocupada.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.config import BrokerConfig, RoutingConfig, ShadowProbeConfig
from app.db import Database
from app.model_stats import ModelStats
from app.providers.base import ModelOutput
from app.repository import TaskRepository
from app.schemas import TaskCreateRequest
from app.shadow_probe import SHADOW_ROLE, ShadowProbe, choose_aspirant


def _entry(name: str, deployment: str = "local", provider: str = "ollama") -> dict:
    return {"name": name, "provider": provider, "deployment": deployment, "context_window": 100000}


def _stat(attempts: int) -> ModelStats:
    return ModelStats(attempts=attempts, successes=attempts, avg_latency_ms=1000.0, avg_cost_usd=0.0)


class ChooseAspirantTests(unittest.TestCase):
    def test_finishes_the_half_measured_model_before_starting_another(self) -> None:
        candidates = [_entry("virgen"), _entry("a-medias")]
        stats = {("ollama", "local", "a-medias", "prose"): _stat(2)}

        chosen = choose_aspirant(candidates, stats, "prose", min_invocations=3, allow_local=True)

        assert chosen is not None
        self.assertEqual(chosen["name"], "a-medias")

    def test_starts_the_first_untested_model_when_none_is_in_progress(self) -> None:
        candidates = [_entry("primero"), _entry("segundo")]

        chosen = choose_aspirant(candidates, {}, "prose", min_invocations=3, allow_local=True)

        assert chosen is not None
        self.assertEqual(chosen["name"], "primero")

    def test_returns_none_when_everyone_has_evidence(self) -> None:
        candidates = [_entry("medido")]
        stats = {("ollama", "local", "medido", "prose"): _stat(5)}

        self.assertIsNone(
            choose_aspirant(candidates, stats, "prose", min_invocations=3, allow_local=True)
        )

    def test_evidence_of_another_task_type_does_not_count(self) -> None:
        """Cada (modelo, tipo de tarea) acumula evidencia por separado: haber
        medido un modelo en prosa no dice nada de cómo rinde en código."""
        candidates = [_entry("solo-en-prosa")]
        stats = {("ollama", "local", "solo-en-prosa", "prose"): _stat(9)}

        chosen = choose_aspirant(candidates, stats, "code", min_invocations=3, allow_local=True)

        assert chosen is not None
        self.assertEqual(chosen["name"], "solo-en-prosa")

    def test_local_models_are_skipped_when_the_machine_is_busy(self) -> None:
        candidates = [_entry("local-1"), _entry("remoto", deployment="cloud")]

        chosen = choose_aspirant(candidates, {}, "prose", min_invocations=3, allow_local=False)

        assert chosen is not None
        self.assertEqual(chosen["name"], "remoto")

    def test_no_candidate_when_only_local_models_and_the_machine_is_busy(self) -> None:
        self.assertIsNone(
            choose_aspirant([_entry("local-1")], {}, "prose", min_invocations=3, allow_local=False)
        )


class _ProviderStub:
    """Proveedor mínimo con lo que el sondeo necesita."""

    def __init__(self, catalog: list[dict], stats: dict | None = None) -> None:
        self.catalog = catalog
        self.stats = stats or {}
        self.measured: list[str] = []
        self.fail = False

    async def eligible_catalog(self, request):
        return self.catalog, self.catalog, self.catalog

    def model_stats_snapshot(self):
        return self.stats

    async def measure(self, request, model) -> ModelOutput:
        self.measured.append(model.model)
        if self.fail:
            raise RuntimeError("el aspirante no responde")
        return ModelOutput("respuesta descartada", 10, 5, 0.0, 1234.0)


class ShadowProbeRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "shadow.db")
        self.db.init_schema()
        self.repository = TaskRepository(self.db)
        self.task_id = f"task_{uuid4().hex}"
        self.db.execute(
            "INSERT INTO tasks (id, request_json, status, created_at, updated_at) "
            "VALUES (?, '{}', 'completed', '2026-01-01', '2026-01-01')",
            (self.task_id,),
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _config(self, **kwargs) -> BrokerConfig:
        return BrokerConfig(
            routing=RoutingConfig(min_invocations=3),
            shadow_probe=ShadowProbeConfig(**{"enabled": True, **kwargs}),
        )

    def _request(self, prompt: str = "hola") -> TaskCreateRequest:
        return TaskCreateRequest(idempotency_key=f"shadow:{uuid4().hex}", content={"prompt": prompt})

    def _invocations(self) -> list:
        return self.db.query_all(
            "SELECT role, model, status, latency_ms FROM model_invocations WHERE task_id = ?",
            (self.task_id,),
        )

    async def test_a_probe_records_the_measurement_as_evidence(self) -> None:
        provider = _ProviderStub([_entry("aspirante", deployment="cloud")])
        probe = ShadowProbe(provider, self._config())

        probe.schedule(self.repository, self.task_id, self._request())
        await probe.drain()

        self.assertEqual(provider.measured, ["aspirante"])
        rows = self._invocations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], SHADOW_ROLE)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["latency_ms"], 1234.0)

    async def test_a_failed_probe_is_also_evidence(self) -> None:
        """Un modelo que no responde no es candidato, y eso debe verse en su
        tasa de éxito en vez de perderse en silencio."""
        provider = _ProviderStub([_entry("roto", deployment="cloud")])
        provider.fail = True
        probe = ShadowProbe(provider, self._config())

        probe.schedule(self.repository, self.task_id, self._request())
        await probe.drain()

        rows = self._invocations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")

    async def test_disabled_by_configuration_does_nothing(self) -> None:
        provider = _ProviderStub([_entry("aspirante", deployment="cloud")])
        probe = ShadowProbe(provider, self._config(enabled=False))

        probe.schedule(self.repository, self.task_id, self._request())
        await probe.drain()

        self.assertEqual(provider.measured, [])
        self.assertEqual(self._invocations(), [])

    async def test_a_huge_prompt_is_not_probed(self) -> None:
        provider = _ProviderStub([_entry("aspirante", deployment="cloud")])
        probe = ShadowProbe(provider, self._config(max_prompt_chars=10))

        probe.schedule(self.repository, self.task_id, self._request(prompt="x" * 200))
        await probe.drain()

        self.assertEqual(provider.measured, [])

    async def test_a_confidential_task_never_probes_a_cloud_model(self) -> None:
        """La frontera de datos la aplica la misma cadena de elegibilidad que
        usa la selección real: si la tarea es confidencial, el catálogo que
        llega al sondeo ya viene sin modelos cloud."""
        provider = _ProviderStub([_entry("solo-local")])
        probe = ShadowProbe(provider, self._config())
        request = TaskCreateRequest(
            idempotency_key=f"shadow:{uuid4().hex}",
            content={"prompt": "secreto"},
            risk={"data_classification": "confidential"},
        )

        self.assertFalse(request.model_requirements.cloud_allowed)

        probe.schedule(self.repository, self.task_id, request)
        await probe.drain()

        self.assertEqual(provider.measured, ["solo-local"])

    async def test_an_active_task_blocks_probing_local_models(self) -> None:
        self.db.execute(
            "INSERT INTO tasks (id, request_json, status, created_at, updated_at) "
            "VALUES ('task_ocupada', '{}', 'generating', '2026-01-01', '2026-01-01')"
        )
        provider = _ProviderStub([_entry("local-pesado")])
        probe = ShadowProbe(provider, self._config())

        probe.schedule(self.repository, self.task_id, self._request())
        await probe.drain()

        self.assertEqual(provider.measured, [])

    async def test_cloud_models_are_probed_even_with_the_machine_busy(self) -> None:
        """El cloud no compite por la VRAM local, así que la ocupación de la
        máquina no es motivo para no medirlo."""
        self.db.execute(
            "INSERT INTO tasks (id, request_json, status, created_at, updated_at) "
            "VALUES ('task_ocupada', '{}', 'generating', '2026-01-01', '2026-01-01')"
        )
        provider = _ProviderStub([_entry("local-pesado"), _entry("remoto", deployment="cloud")])
        probe = ShadowProbe(provider, self._config())

        probe.schedule(self.repository, self.task_id, self._request())
        await probe.drain()

        self.assertEqual(provider.measured, ["remoto"])
