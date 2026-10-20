"""Prioridad de la cola: `priority` se traduce en posición al encolar.

El reclamo ordena antes por `queue_position` que por `priority`. Antes de esto
cada tarea nueva iba al final con un número único, y la prioridad solo
desempataba un empate que no ocurría nunca: una app que necesitaba respuesta
rápida esperaba detrás de toda la cola. Ahora una tarea entra delante de la
primera pendiente menos urgente que ella, y entre iguales sigue el FIFO.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import Database
from app.repository import TaskRepository
from app.schemas import TaskCreateRequest


class QueuePriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "broker.db")
        self.db.init_schema()
        self.repository = TaskRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _create(self, name: str, priority: int = 100) -> str:
        request = TaskCreateRequest.model_validate({
            "idempotency_key": f"key-{name}", "content": {"prompt": name}, "priority": priority,
        })
        task, _ = self.repository.create_task(request, queue_max_size=100)
        return task.task_id

    def _pending(self) -> list[str]:
        return [item.task_id for item in self.repository.list_queue().pending]

    def _claim_order(self) -> list[str]:
        order = []
        while (task_id := self.repository.claim_next_queued_task_id()) is not None:
            order.append(task_id)
            # Una inferencia a la vez: se da por terminada para reclamar la siguiente.
            self.db.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))
        return order

    def test_equal_priorities_keep_arrival_order(self) -> None:
        first, second, third = self._create("a"), self._create("b"), self._create("c")

        self.assertEqual(self._claim_order(), [first, second, third])

    def test_an_urgent_task_jumps_ahead_of_the_waiting_queue(self) -> None:
        normal_a, normal_b = self._create("a"), self._create("b")
        urgent = self._create("urgent", priority=10)

        self.assertEqual(self._pending(), [urgent, normal_a, normal_b])
        self.assertEqual(self._claim_order(), [urgent, normal_a, normal_b])

    def test_urgent_tasks_keep_arrival_order_among_themselves(self) -> None:
        normal = self._create("normal")
        urgent_1 = self._create("u1", priority=10)
        urgent_2 = self._create("u2", priority=10)
        most_urgent = self._create("u0", priority=0)

        self.assertEqual(self._claim_order(), [most_urgent, urgent_1, urgent_2, normal])

    def test_a_less_urgent_task_goes_behind_the_normal_ones(self) -> None:
        background = self._create("bg", priority=500)
        normal = self._create("normal")

        self.assertEqual(self._claim_order(), [normal, background])

    def test_positions_never_repeat(self) -> None:
        for index, priority in enumerate((100, 10, 500, 10, 0, 100)):
            self._create(f"t{index}", priority=priority)

        rows = self.db.query_all(
            "SELECT queue_position FROM tasks WHERE queue_position IS NOT NULL"
        )
        positions = [int(row["queue_position"]) for row in rows]
        self.assertEqual(len(positions), len(set(positions)))

    def test_manual_reorder_still_has_the_last_word(self) -> None:
        """La prioridad se aplica al encolar; mover una tarea a mano en el
        panel la deja donde el usuario dijo, aunque sea menos urgente."""
        normal = self._create("normal")
        urgent = self._create("urgent", priority=10)

        self.repository.reorder_queue([normal, urgent])

        self.assertEqual(self._claim_order(), [normal, urgent])


if __name__ == "__main__":
    unittest.main()
