"""Cobertura de la selección adaptativa: métricas por modelo y score multiobjetivo."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from app.config import TASK_AFFINITY_TYPES, BrokerConfig, RoutingConfig, TaskAffinityConfig
from app.db import Database
from app.maintenance import backfill_invocation_task_type
from app.model_stats import ModelStats, load_model_stats
from app.providers import RoutedModelProvider
from app.schemas import TaskCreateRequest
from app.task_classifier import TASK_TYPES


def _insert_invocation(db: Database, *, model: str, status: str, latency_ms: float | None,
                       cost_usd: float, created_at: str, provider: str = "ollama",
                       deployment: str = "local", task_type: str = "prose") -> None:
    db.execute(
        "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, task_type, "
        "tokens_input, tokens_output, cost_usd, latency_ms, status, created_at, updated_at) "
        "VALUES (?, 'task_stats', 'single', ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?)",
        (f"inv_{uuid4().hex}", provider, deployment, model, task_type,
         cost_usd, latency_ms, status, created_at, created_at),
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
        entry = stats[("ollama", "local", "a", "prose")]
        self.assertEqual(entry.attempts, 3)
        self.assertEqual(entry.successes, 2)
        self.assertEqual(entry.avg_latency_ms, 200)
        self.assertEqual(entry.avg_cost_usd, 0.02)
        # Suavizado de Laplace: (2+1)/(3+2).
        self.assertAlmostEqual(entry.success_rate, 0.6)

    def test_empty_history_returns_no_stats(self) -> None:
        self.assertEqual(load_model_stats(self.db, window_days=7), {})

    def test_backfill_recovers_history_written_before_the_task_type_column(self) -> None:
        # Historial anterior a la columna: invisible para el score aunque esté
        # en la base, porque load_model_stats descarta los NULL.
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE tasks SET request_json = ? WHERE id = 'task_stats'",
            (json.dumps({
                "idempotency_key": "legacy:1",
                "content": {"prompt": "Refactoriza esta función de Python y corrige el bug."},
            }),),
        )
        for _ in range(2):
            _insert_invocation(
                self.db, model="antiguo", status="completed", latency_ms=100,
                cost_usd=0.0, created_at=now, task_type=None,
            )
        self.assertEqual(load_model_stats(self.db, window_days=7), {})

        preview = backfill_invocation_task_type(self.db, dry_run=True)
        self.assertEqual((preview.pending, preview.classified, preview.skipped), (2, 2, 0))
        self.assertEqual(load_model_stats(self.db, window_days=7), {}, "dry-run no escribe")

        result = backfill_invocation_task_type(self.db)
        self.assertEqual(result.classified, 2)
        self.assertEqual(result.reconstructed, 0)
        stats = load_model_stats(self.db, window_days=7)
        self.assertEqual(stats[("ollama", "local", "antiguo", "code")].attempts, 2)
        # Idempotente: ya no queda nada pendiente.
        self.assertEqual(backfill_invocation_task_type(self.db).pending, 0)

    def test_backfill_reconstructs_requests_that_no_longer_validate(self) -> None:
        # El esquema de petición ha evolucionado: lo guardado hace meses puede
        # no validar hoy. Mientras quede el prompt, la fila es recuperable.
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE tasks SET request_json = ? WHERE id = 'task_stats'",
            (json.dumps({
                "content": {"prompt": "Redacta un resumen ejecutivo."},
                "execution": {"strategy": "estrategia_que_ya_no_existe"},
            }),),
        )
        _insert_invocation(
            self.db, model="antiguo", status="completed", latency_ms=100,
            cost_usd=0.0, created_at=now, task_type=None,
        )
        result = backfill_invocation_task_type(self.db)
        self.assertEqual((result.classified, result.reconstructed, result.skipped), (1, 1, 0))
        stats = load_model_stats(self.db, window_days=7)
        self.assertEqual(stats[("ollama", "local", "antiguo", "prose")].attempts, 1)

    def test_backfill_skips_rows_without_a_recoverable_prompt(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("UPDATE tasks SET request_json = '{}' WHERE id = 'task_stats'")
        _insert_invocation(
            self.db, model="antiguo", status="completed", latency_ms=100,
            cost_usd=0.0, created_at=now, task_type=None,
        )
        result = backfill_invocation_task_type(self.db)
        self.assertEqual((result.classified, result.skipped), (0, 1))
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


def _request(prompt: str = "hola", **model_requirements) -> TaskCreateRequest:
    return TaskCreateRequest(
        idempotency_key=f"adaptive:{uuid4().hex}",
        content={"prompt": prompt},
        model_requirements={"allowed_providers": ["ollama"], **model_requirements},
    )


class AdaptiveSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reliable_and_fast_model_is_preferred_over_catalog_order(self) -> None:
        catalog = [_local_entry("mediocre"), _local_entry("excelente")]
        stats = {
            ("ollama", "local", "mediocre", "prose"): ModelStats(10, 5, 5000.0, 0.0),
            ("ollama", "local", "excelente", "prose"): ModelStats(10, 10, 500.0, 0.0),
        }
        router = _router(catalog, stats)
        selected = await router.select(_request(), 2, ["proposer_1", "proposer_2"])
        self.assertEqual([item.model for item in selected], ["excelente", "mediocre"])
        await router.close()

    async def test_without_any_history_catalog_order_decides(self) -> None:
        # Empate total (nadie tiene historial): sin nada que distinga a los
        # candidatos, el orden del catálogo es el desempate final.
        catalog = [_local_entry("primero"), _local_entry("segundo")]
        router = _router(catalog, {})
        selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "primero")
        await router.close()

    async def test_disabled_adaptive_preserves_catalog_order(self) -> None:
        # Con adaptive_selection=false el score ni se calcula: reparto clásico.
        catalog = [_local_entry("primero"), _local_entry("segundo")]
        good_stats = {("ollama", "local", "segundo", "prose"): ModelStats(10, 10, 100.0, 0.0)}
        router = _router(catalog, good_stats, routing=RoutingConfig(adaptive_selection=False))
        selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "primero")
        await router.close()

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
            ("ollama", "local", "estrella", "prose"): ModelStats(10, 10, 100.0, 0.0),
            ("ollama", "local", "preferido", "prose"): ModelStats(10, 3, 8000.0, 0.0),
        }
        router = _router(catalog, stats)
        selected = await router.select(_request(preferred_model="preferido"), 1, ["single"])
        self.assertEqual(selected[0].model, "preferido")
        await router.close()

    async def test_cost_weight_prefers_cheaper_model_when_rest_is_equal(self) -> None:
        caro = {**_local_entry("caro"), "provider": "nvidia", "deployment": "api"}
        barato = {**_local_entry("barato"), "provider": "nvidia", "deployment": "api"}
        stats = {
            ("nvidia", "api", "caro", "prose"): ModelStats(10, 10, 1000.0, 0.05),
            ("nvidia", "api", "barato", "prose"): ModelStats(10, 10, 1000.0, 0.001),
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


class TaskTypeSegmentationTests(unittest.IsolatedAsyncioTestCase):
    """El score se calcula por (modelo, tipo de tarea): el historial de un
    modelo en código no debe decidir su ranking en prosa, ni viceversa."""

    _CODE_PROMPT = "Escribe una función en Python y depura el bug del traceback."

    async def test_code_specialist_wins_only_on_code_prompts(self) -> None:
        catalog = [_local_entry("generalista"), _local_entry("especialista_codigo")]
        stats = {
            # El generalista es mejor en prosa, pero mediocre en código.
            ("ollama", "local", "generalista", "prose"): ModelStats(10, 10, 200.0, 0.0),
            ("ollama", "local", "generalista", "code"): ModelStats(10, 4, 200.0, 0.0),
            # El especialista es al revés: fuerte en código, mediocre en prosa.
            ("ollama", "local", "especialista_codigo", "prose"): ModelStats(10, 4, 200.0, 0.0),
            ("ollama", "local", "especialista_codigo", "code"): ModelStats(10, 10, 200.0, 0.0),
        }
        router = _router(catalog, stats)

        prose = await router.select(_request(prompt="Hola, ¿cómo estás?"), 1, ["single"])
        self.assertEqual(prose[0].model, "generalista")

        code = await router.select(_request(prompt=self._CODE_PROMPT), 1, ["single"])
        self.assertEqual(code[0].model, "especialista_codigo")
        await router.close()

    async def test_no_history_for_this_task_type_is_cold_start(self) -> None:
        # "generalista" solo tiene historial de prosa: en código, sin
        # evidencia propia para ese tipo, puntúa neutro igual que "nuevo".
        catalog = [_local_entry("generalista"), _local_entry("nuevo")]
        stats = {
            ("ollama", "local", "generalista", "prose"): ModelStats(10, 10, 100.0, 0.0),
        }
        router = _router(catalog, stats)
        code = await router.select(_request(prompt=self._CODE_PROMPT), 1, ["single"])
        self.assertEqual(code[0].model, "generalista")
        await router.close()


class EvidenceTieBreakTests(unittest.IsolatedAsyncioTestCase):
    """Con todo el catálogo en score neutro (arranque en frío), el desempate
    no puede ser el orden del proveedor: eso ataba cada petición al último
    modelo descargado en Ollama. El desempate reparte la medición."""

    async def test_partially_measured_model_is_finished_first(self) -> None:
        # "a_medias" ya tiene 2 de las 3 invocaciones que exige
        # min_invocations: rematarlo da evidencia utilizable ya, mientras que
        # estrenar a otro no daría ninguna hasta dentro de tres peticiones.
        catalog = [_local_entry("sin_probar"), _local_entry("a_medias")]
        stats = {("ollama", "local", "a_medias", "prose"): ModelStats(2, 2, 100.0, 0.0)}
        router = _router(catalog, stats)
        selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "a_medias")
        await router.close()

    async def test_unmeasured_model_beats_already_measured_one(self) -> None:
        # "medido" tiene evidencia suficiente, pero su score sale neutro
        # (0.5 exactos con éxito/latencia/coste medios): a igualdad, la
        # invocación va a quien todavía no ha sido medido.
        catalog = [_local_entry("medido"), _local_entry("sin_probar")]
        stats = {("ollama", "local", "medido", "prose"): ModelStats(8, 3, None, None)}
        router = _router(catalog, stats)
        selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "sin_probar")
        await router.close()

    async def test_score_still_beats_the_tie_break(self) -> None:
        # El desempate solo actúa a igualdad de score: un modelo con buena
        # evidencia gana a uno sin probar.
        catalog = [_local_entry("sin_probar"), _local_entry("fiable")]
        stats = {("ollama", "local", "fiable", "prose"): ModelStats(10, 10, 100.0, 0.0)}
        router = _router(catalog, stats)
        selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "fiable")
        await router.close()

    async def test_rotation_ends_up_measuring_every_candidate(self) -> None:
        """El bucle real: cada petición elige, la invocación se registra y la
        siguiente petición ve ese historial.

        Es la prueba de que el bloqueo de arranque en frío queda roto. En
        cuanto un modelo completa min_invocations su score deja de ser neutro
        y ganaría todas las peticiones siguientes; lo que evita que ahí se
        acabe la historia es la exploración dirigida, que va rematando de uno
        en uno a los que aún no tienen evidencia."""
        catalog = [_local_entry("uno"), _local_entry("dos"), _local_entry("tres")]
        stats: dict = {}
        router = _router(catalog, stats, routing=RoutingConfig(exploration_rate=1.0))
        with patch("app.providers.routing.random.random", return_value=0.0), \
                patch("app.providers.routing.random.choice", side_effect=lambda pool: pool[0]):
            for _ in range(9):
                selected = await router.select(_request(), 1, ["single"])
                key = ("ollama", "local", selected[0].model, "prose")
                previous = stats.get(key)
                attempts = (previous.attempts if previous else 0) + 1
                # Todas completan con las mismas métricas: lo que decide es la
                # evidencia acumulada, no una diferencia de calidad.
                stats[key] = ModelStats(attempts, attempts, 100.0, 0.0)
        for name in ("uno", "dos", "tres"):
            self.assertGreaterEqual(stats[("ollama", "local", name, "prose")].attempts, 3)
        await router.close()

    async def test_exploration_finishes_the_started_model_before_starting_another(self) -> None:
        # Dispersar las exploraciones deja a todo el catálogo con una o dos
        # invocaciones sueltas: ninguna llega a min_invocations y ningún score
        # deja de ser neutro. Se remata al que ya está a medias.
        catalog = [_local_entry("campeon"), _local_entry("sin_probar"), _local_entry("a_medias")]
        stats = {
            ("ollama", "local", "campeon", "prose"): ModelStats(20, 20, 100.0, 0.0),
            ("ollama", "local", "a_medias", "prose"): ModelStats(1, 1, 5000.0, 0.0),
        }
        router = _router(catalog, stats, routing=RoutingConfig(exploration_rate=1.0))
        with patch("app.providers.routing.random.random", return_value=0.0):
            selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "a_medias")
        await router.close()


class TaskAffinityTests(unittest.IsolatedAsyncioTestCase):
    """Los filtros de elegibilidad responden a "¿puede atender la petición?".
    La idoneidad responde a "¿debería?": un modelo de código no redacta prosa
    solo porque encabece el catálogo o le toque en la rotación."""

    _CODE_PROMPT = "Implementa una función en Python y depura el traceback."

    def test_config_task_types_match_the_classifier(self) -> None:
        # config.py no puede importar task_classifier (ciclo de imports), así
        # que la lista está duplicada: aquí se verifica que no diverja.
        self.assertEqual(sorted(TASK_AFFINITY_TYPES), sorted(TASK_TYPES))

    async def test_code_specialist_is_skipped_for_prose(self) -> None:
        catalog = [_local_entry("qwen3-coder-next:latest"), _local_entry("gemma4:12b")]
        router = _router(catalog, {})
        prose = await router.select(_request(prompt="¿Cómo estás?"), 1, ["single"])
        self.assertEqual(prose[0].model, "gemma4:12b")
        await router.close()

    async def test_code_specialist_remains_eligible_for_code(self) -> None:
        catalog = [_local_entry("qwen3-coder-next:latest"), _local_entry("gemma4:12b")]
        router = _router(catalog, {})
        code = await router.select(_request(prompt=self._CODE_PROMPT), 1, ["single"])
        self.assertEqual(code[0].model, "qwen3-coder-next:latest")
        await router.close()

    async def test_matches_catalog_id_when_the_local_name_hides_it(self) -> None:
        # El mismo modelo se llama distinto según el proveedor: el nombre local
        # no delata la especialidad, el catalog_id de models.dev sí.
        disfrazado = {
            **_local_entry("mi-modelo:latest"),
            "catalog": {"catalog_id": "qwen/qwen3-coder-30b"},
        }
        router = _router([disfrazado, _local_entry("gemma4:12b")], {})
        prose = await router.select(_request(prompt="¿Cómo estás?"), 1, ["single"])
        self.assertEqual(prose[0].model, "gemma4:12b")
        await router.close()

    async def test_single_purpose_models_are_excluded_from_every_task_type(self) -> None:
        # Guardarraíles, OCR y traductores no son modelos de chat: la rotación
        # no debe estrenarlos nunca.
        catalog = [_local_entry("meta/llama-guard-4-12b"), _local_entry("gemma4:12b")]
        router = _router(catalog, {})
        for prompt in ("¿Cómo estás?", self._CODE_PROMPT):
            selected = await router.select(_request(prompt=prompt), 1, ["single"])
            self.assertEqual(selected[0].model, "gemma4:12b")
        await router.close()

    async def test_words_containing_a_pattern_are_not_excluded(self) -> None:
        # "mediocre" contiene "ocr" y "decoder" contiene "coder": los patrones
        # van anclados a separadores para no atrapar palabras completas.
        catalog = [_local_entry("mediocre:7b"), _local_entry("decoder-lm:8b")]
        router = _router(catalog, {})
        selected = await router.select(_request(prompt="¿Cómo estás?"), 2, ["a", "b"])
        self.assertEqual(
            sorted(item.model for item in selected), ["decoder-lm:8b", "mediocre:7b"],
        )
        await router.close()

    async def test_filter_is_ignored_rather_than_failing_the_task(self) -> None:
        # Si lo único disponible es un modelo de código, se usa: una
        # preferencia no puede convertir en irresoluble una tarea atendible.
        catalog = [_local_entry("qwen3-coder-next:latest")]
        router = _router(catalog, {})
        prose = await router.select(_request(prompt="¿Cómo estás?"), 1, ["single"])
        self.assertEqual(prose[0].model, "qwen3-coder-next:latest")
        await router.close()

    async def test_explicit_preference_overrides_the_filter(self) -> None:
        catalog = [_local_entry("gemma4:12b"), _local_entry("qwen3-coder-next:latest")]
        router = _router(catalog, {})
        prose = await router.select(
            _request(prompt="¿Cómo estás?", preferred_model="qwen3-coder-next:latest"),
            1,
            ["single"],
        )
        self.assertEqual(prose[0].model, "qwen3-coder-next:latest")
        await router.close()

    async def test_disabled_affinity_restores_the_previous_behaviour(self) -> None:
        catalog = [_local_entry("qwen3-coder-next:latest"), _local_entry("gemma4:12b")]
        config = BrokerConfig()
        config.task_affinity = TaskAffinityConfig(enabled=False)
        router = RoutedModelProvider(
            config, ollama=_CatalogStub(catalog), deepseek=_CatalogStub([]), stats_loader=lambda: {},
        )
        prose = await router.select(_request(prompt="¿Cómo estás?"), 1, ["single"])
        self.assertEqual(prose[0].model, "qwen3-coder-next:latest")
        await router.close()


class ExplorationTests(unittest.IsolatedAsyncioTestCase):
    """El score determinista deja fijado a un 'campeón' en cuanto gana: sin
    exploración, ningún otro candidato vuelve a recibir invocaciones para
    disputarle el puesto. exploration_rate rompe ese bloqueo."""

    def _dominant_stats(self) -> dict:
        return {
            ("ollama", "local", "campeon", "prose"): ModelStats(20, 20, 100.0, 0.0),
            ("ollama", "local", "candidato", "prose"): ModelStats(20, 10, 500.0, 0.0),
        }

    async def test_disabled_by_default_keeps_champion(self) -> None:
        catalog = [_local_entry("campeon"), _local_entry("candidato")]
        router = _router(catalog, self._dominant_stats())
        with patch("app.providers.routing.random.random", return_value=0.0):
            selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "campeon")
        await router.close()

    async def test_exploration_promotes_random_alternative(self) -> None:
        catalog = [_local_entry("campeon"), _local_entry("candidato")]
        routing = RoutingConfig(exploration_rate=1.0)
        router = _router(catalog, self._dominant_stats(), routing=routing)
        with patch("app.providers.routing.random.random", return_value=0.0), \
                patch("app.providers.routing.random.randrange", return_value=1):
            selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "candidato")
        await router.close()

    async def test_exploration_below_threshold_keeps_champion(self) -> None:
        catalog = [_local_entry("campeon"), _local_entry("candidato")]
        routing = RoutingConfig(exploration_rate=0.1)
        router = _router(catalog, self._dominant_stats(), routing=routing)
        with patch("app.providers.routing.random.random", return_value=0.5):
            selected = await router.select(_request(), 1, ["single"])
        self.assertEqual(selected[0].model, "campeon")
        await router.close()

    async def test_exploration_runs_with_no_history_at_all(self) -> None:
        # Antes, sin estadísticas el ranking devolvía el catálogo tal cual y ni
        # siquiera se llegaba a la exploración: el arranque en frío nunca
        # generaba el historial que necesitaba para dejar de serlo.
        catalog = [_local_entry("primero"), _local_entry("segundo")]
        routing = RoutingConfig(exploration_rate=1.0)
        for stats_loader in (lambda: {}, None):
            with self.subTest(stats_loader=stats_loader):
                router = _router(catalog, None, routing=routing, stats_loader=stats_loader)
                with patch("app.providers.routing.random.random", return_value=0.0), \
                        patch("app.providers.routing.random.choice", side_effect=lambda pool: pool[0]):
                    selected = await router.select(_request(), 1, ["single"])
                self.assertEqual(selected[0].model, "segundo")
                await router.close()

    async def test_preferred_model_overrides_exploration(self) -> None:
        catalog = [_local_entry("campeon"), _local_entry("candidato")]
        routing = RoutingConfig(exploration_rate=1.0)
        router = _router(catalog, self._dominant_stats(), routing=routing)
        with patch("app.providers.routing.random.random", return_value=0.0), \
                patch("app.providers.routing.random.randrange", return_value=1):
            selected = await router.select(_request(preferred_model="campeon"), 1, ["single"])
        self.assertEqual(selected[0].model, "campeon")
        await router.close()
