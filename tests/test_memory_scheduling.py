"""Planificación por memoria: ceder el turno en vez de fallar.

Una tarea que no cabe AHORA pero sí cabría en la máquina vacía no es un fallo.
Cede el turno conservando su sitio, el despachador adelanta a quien sí quepa, y
tras unos cuantos turnos cedidos la aplazada reserva el suyo para que no se
quede esperando eternamente.

Las dos propiedades que se protegen aquí tiran en direcciones opuestas y por
eso conviven en el mismo fichero: **aprovechar la máquina** (que nadie esté
ocioso delante de una cola llena) y **no matar de hambre a nadie** (que una
petición grande acabe corriendo aunque lleguen pequeñas sin parar).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db import Database, dumps_json, loads_json
from app.repository import TaskRepository
from app.schemas import TaskStatus

_BLOCK = {
    "model": "lfm2:24b",
    "needed_bytes": 14 * 1024**3,
    "occupied_bytes": 52 * 1024**3,
    "budget_bytes": 62 * 1024**3,
    "holders": ["qwen3-coder-next:latest"],
}


class MemorySchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "broker.db")
        self.db.init_schema()
        self.repository = TaskRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _queue(self, task_id: str, position: int) -> None:
        # Petición mínima pero VÁLIDA: `request_cancel` reconstruye el contrato
        # al leer la fila, así que un '{}' no serviría para todos los casos.
        request = dumps_json({
            "idempotency_key": f"key-{task_id}", "content": {"prompt": "hola"},
        })
        self.db.execute(
            "INSERT INTO tasks (id, request_json, status, priority, queue_position, "
            "kind, created_at, updated_at) "
            "VALUES (?, ?, 'queued', 0, ?, 'inference', ?, ?)",
            (task_id, request, position, f"2026-01-01T00:00:0{position}", "2026-01-01"),
        )

    def _defer(self, task_id: str, **overrides) -> dict:
        options = {
            "wait_seconds": 20.0, "reserve_after": 5, "reserve_window_seconds": 300.0,
            **overrides,
        }
        return self.repository.defer_task_for_memory(task_id, dict(_BLOCK), **options)

    def _status(self, task_id: str) -> str:
        row = self.db.query_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
        return str(row["status"])

    # ------------------------------------------------------- ceder el turno

    def test_a_deferred_task_waits_instead_of_failing(self) -> None:
        self._queue("task_a", 1)

        state = self._defer("task_a")

        self.assertEqual(self._status("task_a"), TaskStatus.waiting_for_memory.value)
        self.assertEqual(state["deferrals"], 1)
        # Su sitio en la cola no se toca: esperar memoria no la degrada.
        row = self.db.query_one("SELECT queue_position FROM tasks WHERE id = ?", ("task_a",))
        self.assertEqual(row["queue_position"], 1)

    def test_the_next_task_that_fits_overtakes_the_one_waiting(self) -> None:
        """El aprovechamiento: la máquina no se queda ociosa delante de una
        cola llena solo porque la cabeza no quepa."""
        self._queue("task_a", 1)
        self._queue("task_b", 2)
        self._defer("task_a")

        self.assertEqual(self.repository.claim_next_queued_task_id(), "task_b")

    def test_the_waiting_task_is_claimed_again_once_its_turn_arrives(self) -> None:
        self._queue("task_a", 1)
        self._defer("task_a", wait_seconds=1.0)
        # Sin esperar de verdad: se adelanta el reloj de la fila.
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.db.execute("UPDATE tasks SET not_before = ? WHERE id = ?", (past, "task_a"))

        self.assertEqual(self.repository.claim_next_queued_task_id(), "task_a")

    def test_nothing_runs_while_the_only_task_is_still_waiting(self) -> None:
        self._queue("task_a", 1)
        self._defer("task_a")

        self.assertIsNone(self.repository.claim_next_queued_task_id())

    # ------------------------------------------------- no matar de hambre

    def test_after_enough_deferrals_the_task_reserves_its_turn(self) -> None:
        self._queue("task_a", 1)
        self._queue("task_b", 2)

        for _ in range(3):
            state = self._defer("task_a", reserve_after=3)

        self.assertIsNotNone(state["reserved_until"])
        # Con la reserva en pie, task_b ya no adelanta aunque quepa.
        self.assertIsNone(self.repository.claim_next_queued_task_id())

    def test_the_reserved_task_runs_as_soon_as_its_retry_arrives(self) -> None:
        self._queue("task_a", 1)
        self._queue("task_b", 2)
        for _ in range(3):
            self._defer("task_a", reserve_after=3)
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.db.execute("UPDATE tasks SET not_before = ? WHERE id = ?", (past, "task_a"))

        self.assertEqual(self.repository.claim_next_queued_task_id(), "task_a")

    def test_an_expired_reservation_lets_the_queue_flow_again(self) -> None:
        """La otra mitad del trato, y la razón de que la reserva sea una
        ventana: si la memoria no se libera JAMÁS, una reserva perpetua
        congelaría toda la cola. Al expirar, quien quepa vuelve a pasar."""
        self._queue("task_a", 1)
        self._queue("task_b", 2)
        for _ in range(3):
            self._defer("task_a", reserve_after=3)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.db.execute(
            "UPDATE tasks SET memory_reserved_until = ? WHERE id = ?", (expired, "task_a"),
        )

        self.assertEqual(self.repository.claim_next_queued_task_id(), "task_b")
        row = self.db.query_one(
            "SELECT memory_deferrals, memory_reserved_until FROM tasks WHERE id = ?", ("task_a",),
        )
        self.assertEqual(row["memory_deferrals"], 0)
        self.assertIsNone(row["memory_reserved_until"])

    # ------------------------------------------------------- sin romper nada

    def test_the_single_workflow_invariant_still_holds(self) -> None:
        """El invariante del broker: una inferencia a la vez. Que ahora haya
        tareas esperando memoria no puede abrir la puerta a dos a la vez."""
        self._queue("task_a", 1)
        self._queue("task_b", 2)
        self._defer("task_a")
        self.db.execute("UPDATE tasks SET status = 'generating' WHERE id = 'task_b'")

        self.assertIsNone(self.repository.claim_next_queued_task_id())

    def test_a_waiting_task_stays_visible_in_the_queue(self) -> None:
        self._queue("task_a", 1)
        self._defer("task_a")

        pending = [item.task_id for item in self.repository.list_queue().pending]

        self.assertEqual(pending, ["task_a"])

    def test_a_waiting_task_can_still_be_cancelled(self) -> None:
        self._queue("task_a", 1)
        self._defer("task_a")

        self.repository.request_cancel("task_a")

        self.assertEqual(self._status("task_a"), TaskStatus.cancelled.value)
        self.assertIsNone(self.repository.claim_next_queued_task_id())

    def test_a_waiting_task_can_still_be_reordered(self) -> None:
        """Mover una tarea en la cola es control del usuario: esperar memoria
        no puede quitárselo."""
        self._queue("task_a", 1)
        self._queue("task_b", 2)
        self._defer("task_a")

        self.repository.reorder_queue(["task_b", "task_a"])

        row = self.db.query_one("SELECT queue_position FROM tasks WHERE id = ?", ("task_a",))
        self.assertEqual(row["queue_position"], 2)

    def test_the_wait_records_who_is_holding_the_memory(self) -> None:
        """Sin esto el panel solo puede decir "espera", que no deja decidir si
        cancelar."""
        self._queue("task_a", 1)
        self._defer("task_a")

        row = self.db.query_one("SELECT memory_block_json FROM tasks WHERE id = ?", ("task_a",))
        block = loads_json(row["memory_block_json"], {})

        self.assertEqual(block["model"], "lfm2:24b")
        self.assertEqual(block["holders"], ["qwen3-coder-next:latest"])

    def test_starting_to_run_clears_the_waiting_history(self) -> None:
        self._queue("task_a", 1)
        self._defer("task_a")

        self.repository.clear_memory_wait("task_a")

        row = self.db.query_one(
            "SELECT not_before, memory_deferrals, memory_block_json FROM tasks WHERE id = ?",
            ("task_a",),
        )
        self.assertIsNone(row["not_before"])
        self.assertEqual(row["memory_deferrals"], 0)
        self.assertIsNone(row["memory_block_json"])


if __name__ == "__main__":
    unittest.main()
