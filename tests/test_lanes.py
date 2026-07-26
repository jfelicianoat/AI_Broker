"""Carriles de trabajo (app.schemas.TaskKind).

La propiedad que se protege es la que motivó el diseño: las conversiones se
VEN como tareas —cola, historial, carga de la máquina— pero no compiten por el
workflow único de la inferencia. Si compartieran capacidad, subir un PDF
congelaría la cola de respuestas.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.dashboard import DashboardQueryRepository
from app.db import Database
from app.repository import TaskRepository
from app.schemas import TaskCreateRequest, TaskKind, TaskStatus


class LaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "lanes.db")
        self.db.init_schema()
        self.repository = TaskRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _inference_task(self) -> str:
        request = TaskCreateRequest(
            idempotency_key=f"lane:{uuid4().hex}", content={"prompt": "hola"},
        )
        state, _ = self.repository.create_task(request, queue_max_size=100)
        return state.task_id

    def _ingestion_task(self, filename: str = "documento.pdf") -> str:
        return self.repository.create_ingestion_task(f"file_{uuid4().hex}", filename, 1024)

    def test_a_conversion_is_a_visible_queued_task(self) -> None:
        task_id = self._ingestion_task()

        queue = self.repository.list_queue()
        pending = [item for item in queue.pending if item.task_id == task_id]

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, TaskKind.ingestion)
        self.assertEqual(pending[0].status, TaskStatus.queued)

    def test_a_running_conversion_does_not_block_the_inference_lane(self) -> None:
        """El corazón del diseño: con una conversión en marcha, la inferencia
        sigue reclamando. Antes de los carriles esto no podía fallar porque las
        conversiones no estaban en `tasks`; ahora sí lo están, y hay que
        garantizar que no consumen el workflow único."""
        inference_id = self._inference_task()
        self._ingestion_task()
        claimed_ingestion = self.repository.claim_next_ingestion_task(2)
        self.assertIsNotNone(claimed_ingestion)

        claimed = self.repository.claim_next_queued_task_id()

        self.assertEqual(claimed, inference_id)

    def test_an_active_inference_still_blocks_another_inference(self) -> None:
        """El invariante de un solo workflow sigue en pie."""
        self._inference_task()
        self._inference_task()

        first = self.repository.claim_next_queued_task_id()
        second = self.repository.claim_next_queued_task_id()

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_the_ingestion_lane_respects_its_concurrency_limit(self) -> None:
        """Antes no había límite ninguno: cada subida arrancaba su hilo."""
        for _ in range(4):
            self._ingestion_task()

        claimed = [self.repository.claim_next_ingestion_task(2) for _ in range(4)]

        self.assertEqual(len([item for item in claimed if item is not None]), 2)

    def test_a_conversion_counts_as_a_busy_machine(self) -> None:
        """El sondeo en sombra pregunta por aquí antes de meter trabajo local.
        Con las conversiones invisibles, creía la máquina libre teniendo dos
        Doclings dentro."""
        self._ingestion_task()
        self.assertFalse(self.repository.has_active_task())

        self.repository.claim_next_ingestion_task(2)

        self.assertTrue(self.repository.has_active_task())

    def test_an_interrupted_conversion_returns_to_the_queue(self) -> None:
        task_id = self._ingestion_task()
        self.repository.claim_next_ingestion_task(2)

        requeued = self.repository.requeue_interrupted_ingestion_tasks()

        self.assertEqual(requeued, 1)
        self.assertEqual(self.repository.get_task(task_id).status, TaskStatus.queued)

    def test_inference_recovery_ignores_the_ingestion_lane(self) -> None:
        """La recuperación de inferencia razona sobre invocaciones ambiguas y
        coste ya facturado; una conversión no tiene ni lo uno ni lo otro."""
        self._ingestion_task()
        self.repository.claim_next_ingestion_task(2)

        self.assertEqual(self.repository.recover_interrupted_tasks(), 0)

    def test_an_ingestion_task_carries_a_descriptor_not_an_inference_request(self) -> None:
        task_id = self._ingestion_task("informe.pdf")

        self.assertEqual(self.repository.get_task_kind(task_id), TaskKind.ingestion)
        state = self.repository.get_task(task_id)
        self.assertIsNone(state.execution_strategy)
        with self.assertRaises(ValueError):
            self.repository.get_task_request(task_id)

    def test_the_panel_reports_the_load_of_both_lanes(self) -> None:
        """La respuesta a «cuál es la carga real de la máquina»: hasta que las
        conversiones fueron tareas, la mitad de esa carga no se podía contar."""
        queries = DashboardQueryRepository(self.db)
        self._inference_task()
        self._ingestion_task()
        self._ingestion_task()
        self.repository.claim_next_ingestion_task(2)

        lanes = {lane.kind: lane for lane in queries.lane_occupancy({"inference": 1, "ingestion": 2})}

        self.assertEqual((lanes[TaskKind.inference].active, lanes[TaskKind.inference].queued), (0, 1))
        self.assertEqual((lanes[TaskKind.ingestion].active, lanes[TaskKind.ingestion].queued), (1, 1))
        self.assertEqual(lanes[TaskKind.ingestion].capacity, 2)

    def test_the_task_list_can_be_filtered_by_lane(self) -> None:
        queries = DashboardQueryRepository(self.db)
        self._inference_task()
        self._ingestion_task("informe.pdf")

        only_ingestion = queries.list_tasks(
            page=1, page_size=10, status=None, origin=None, kind=TaskKind.ingestion,
        )
        both = queries.list_tasks(page=1, page_size=10, status=None, origin=None)

        self.assertEqual(both.total, 2)
        self.assertEqual(only_ingestion.total, 1)
        item = only_ingestion.items[0]
        self.assertEqual(item.kind, TaskKind.ingestion)
        # El nombre del fichero es lo que identifica el trabajo: una conversión
        # no tiene estrategia que enseñar, y fabricarle una sería mentir.
        self.assertEqual(item.label, "informe.pdf")
        self.assertIsNone(item.execution_strategy)

    def test_the_active_task_panel_only_shows_inference(self) -> None:
        """El panel "Tarea activa" habla del workflow único de inferencia; una
        conversión no tiene ni prompt ni estrategia que enseñar ahí."""
        queries = DashboardQueryRepository(self.db)
        self._ingestion_task()
        self.repository.claim_next_ingestion_task(2)

        self.assertIsNone(queries.active_task_detail())

    def test_claiming_a_conversion_reports_which_file_to_convert(self) -> None:
        file_id = f"file_{uuid4().hex}"
        task_id = self.repository.create_ingestion_task(file_id, "informe.pdf", 10)

        claimed = self.repository.claim_next_ingestion_task(1)

        self.assertEqual(claimed, (task_id, file_id))
