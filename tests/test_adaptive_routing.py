"""Cobertura de la selección adaptativa: métricas por modelo y score multiobjetivo."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.config import BrokerConfig, RoutingConfig
from app.db import Database
from app.model_stats import ModelStats, load_model_stats
from app.providers import RoutedModelProvider
from app.schemas import TaskCreateRequest


def _insert_invocation(db: Database, *, model: str, status: str, latency_ms: float | None,
                       cost_usd: float, created_at: str, provider: str = "ollama",
                       deployment: str = "local") -> None:
    db.execute(
        "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, "
        "tokens_input, tokens_output, cost_usd, latency_ms, status, created_at, updated_at) "
        "VALUES (?, 'task_stats', 'single', ?, ?, ?, 1, 1, ?, ?, ?, ?, ?)",
        (f"inv_{uuid4().hex}", provider, deployment, model, cost_usd, latency_ms, status, created_at, created_at),
    )


class ModelStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "stats.db")
        self.db.init_schema()
        # La FK de task_id exige una tarea; una fila mínima basta.
        self.db.execute(
            "INSERT INTO tasks (id, request_json, status, created_at, updated_at) "
            "VALUES ('task_stats', '{}', 'completed', '2026-01-01', '2026-01-01')"
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_aggregates_success_latency_and_cost_within_window(self) -> None:
        now = datetime.now(timezone.utc)
        recent = now.isoformat()
        stale = (now - timedelta(days=30)).isoformat()
        _insert_invocation(self.db, model="a", status="completed", latency_ms=100, cost_usd=0.01, created_at=recent)
        _insert_invocation(self.db, model="a", status="completed", latency_ms=300, cost_usd=0.03, created_at=recent)
        _insert_invocation(self.db, model="a", status="failed", latency_ms=None, cost_usd=0.0, created_at=recent)
        # Fuera de ventana e in-flight: no cuentan.
        _insert_invocation(self.db, model="a", status="completed", latency_ms=9999, cost_usd=9.9, created_at=stale)
        _insert_invocation(self.db, model="a", status="started", latency_ms=None, cost_usd=0.0, created_at=recent)
        _insert_invocation(self.db, model="a", status="ambiguous", latency_ms=None, cost_usd=0.0, created_at=recent)

        stats = load_model_stats(self.db, window_days=7)
        entry = stats[("ollama", "local", "a")]
        self.assertEqual(entry.attempts, 3)
        self.assertEqual(entry.successes, 2)
        self.assertEqual(entry.avg_latency_ms, 200)
        self.assertEqual(entry.avg_cost_usd, 0.02)
        # Suavizado de Laplace: (2+1)/(3+2).
        self.assertAlmostEqual(entry.success_rate, 0.6)

    def test_empty_history_returns_no_stats(self) -> None:
        self.assertEqual(load_model_stats(self.db, window_days=7), {})


class _CatalogStub:
    def __init__(self, models):
        self._models = models

    async def models(self):
        return self._models

    async def close(self):
        return None


def _local_entry(name: str) -> dict:
    return {
        "name": name,
        "provider": "ollama",
        "deployment": "local",
        "context_window": 100000,
        "capabilities": ["completion"],
        "compatibility": "compatible",
    }


def _router(catalog: list[dict], stats: dict | None, routing: RoutingConfig | None = None,
            stats_loader=None) -> RoutedModelProvider:
    config = BrokerConfig()
    if routing is not None:
        config.routing = routing
    loader = stats_loader if stats_loader is not None else (lambda: stats) if stats is not None else None
    return RoutedModelProvider(
        config,
        ollama=_CatalogStub(catalog),
        deepseek=_CatalogStub([]),
        stats_loader=loader,
    )


def _request(**model_requirements) -> TaskCreateRequest:
    return TaskCreateRequest(
        idempotency_key=f"adaptive:{uuid4().hex}",
        content={"prompt": "hola"},
        model_requirements={"allowed_providers": ["ollama"], **model_requirements},
    )


class AdaptiveSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reliable_and_fast_model_is_preferred_over_catalog_order(self) -> None:
        catalog = [_local_entry("mediocre"), _local_entry("excelente")]
        stats = {
            ("ollama", "local", "mediocre"): ModelStats(10, 5, 5000.0, 0.0),
            ("ollama", "local", "excelente"): ModelStats(10, 10, 500.0, 0.0),
        }
        router = _router(catalog, stats)
        selected = await router.select(_request(), 2, ["proposer_1", "proposer_2"])
        self.assertEqual([item.model for item in selected], ["excelente", "mediocre"])
        await router.close()

    async def test_cold_start_and_disabled_adaptive_preserve_catalog_order(self) -> None:
        catalog = [_local_entry("primero"), _local_entry("segundo")]

        # Sin historial suficiente (min_invocations) todo puntúa neutro:
        # el orden estable conserva el catálogo.
        cold_stats = {("ollama", "local", "segundo"): ModelStats(1, 1, 100.0, 0.0)}
        cold_router = _router(catalog, cold_stats)
        cold = await cold_router.select(_request(), 1, ["single"])
        self.assertEqual(cold[0].model, "primero")
        await cold_router.close()

        # Con adaptive_selection=false el score ni se calcula.
        good_stats = {("ollama", "local", "segundo"): ModelStats(10, 10, 100.0, 0.0)}
        disabled_router = _router(catalog, good_stats, routing=RoutingConfig(adaptive_selection=False))
        disabled = await disabled_router.select(_request(), 1, ["single"])
        self.assertEqual(disabled[0].model, "primero")
        await disabled_router.close()

    async def test_stats_loader_failure_degrades_to_catalog_order(self) -> None:
        catalog = [_local_entry("primero"), _local_entry("segundo")]

        def broken_loader():
            raise RuntimeError("sqlite bloqueada")

        router = _router(catalog, None, stats_loader=broken_loader)
        selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "primero")
        await router.close()

    async def test_preferred_model_still_overrides_score(self) -> None:
        catalog = [_local_entry("estrella"), _local_entry("preferido")]
        stats = {
            ("ollama", "local", "estrella"): ModelStats(10, 10, 100.0, 0.0),
            ("ollama", "local", "preferido"): ModelStats(10, 3, 8000.0, 0.0),
        }
        router = _router(catalog, stats)
        selected = await router.select(_request(preferred_model="preferido"), 1, ["single"])
        self.assertEqual(selected[0].model, "preferido")
        await router.close()

    async def test_cost_weight_prefers_cheaper_model_when_rest_is_equal(self) -> None:
        caro = {**_local_entry("caro"), "provider": "nvidia", "deployment": "api"}
        barato = {**_local_entry("barato"), "provider": "nvidia", "deployment": "api"}
        stats = {
            ("nvidia", "api", "caro"): ModelStats(10, 10, 1000.0, 0.05),
            ("nvidia", "api", "barato"): ModelStats(10, 10, 1000.0, 0.001),
        }
        config = BrokerConfig()
        router = RoutedModelProvider(
            config,
            ollama=_CatalogStub([]),
            deepseek=_CatalogStub([]),
            custom={"nvidia": _CatalogStub([caro, barato])},
            stats_loader=lambda: stats,
        )
        request = TaskCreateRequest(
            idempotency_key=f"adaptive:{uuid4().hex}",
            content={"prompt": "hola"},
            model_requirements={"allowed_providers": ["nvidia"], "cloud_allowed": True},
        )
        selected = await router.select(request, 1, ["single"])
        self.assertEqual(selected[0].model, "barato")
        await router.close()
