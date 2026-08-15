from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.artifacts import ArtifactRecord
from app.db import Database, dumps_json, loads_json
from app.providers import ModelOutput, ProviderError
from app.schemas import (
    TERMINAL_STATUSES,
    ExecutionStrategy,
    ModelReference,
    QueueItem,
    QueueResponse,
    TaskCreateRequest,
    TaskKind,
    TaskStateResponse,
    TaskStatus,
    is_local_deployment,
)

# Estados en los que una tarea ocupa el workflow activo. Fuente única: la usan
# el reclamo del despachador (invariante de un solo workflow) y la
# comprobación de máquina ociosa del sondeo en sombra.
ACTIVE_TASK_STATUSES = (
    "routing", "planning", "resource_planning", "chunking", "generating",
    "proposing", "evaluating", "debating", "synthesizing", "verifying",
    # Carril de ingesta: ocupa la máquina, pero no el workflow de inferencia.
    "converting",
)

# Estados activos que consumen el workflow ÚNICO de inferencia. Deliberadamente
# sin "converting": si una conversión bloqueara este invariante, subir un PDF
# congelaría la cola de respuestas, que es justo lo que los carriles evitan.
ACTIVE_INFERENCE_STATUSES = tuple(
    status for status in ACTIVE_TASK_STATUSES if status != "converting"
)

# Estados de una tarea que sigue esperando turno. `waiting_for_memory` cuenta
# como pendiente en todo lo que mira la cola —listado, reordenación, mover
# arriba/abajo— porque para el usuario ES una tarea en cola: no ha corrido, no
# ha fallado, y quiere poder colocarla o cancelarla como cualquier otra.
PENDING_QUEUE_STATUSES = ("queued", "waiting_for_memory")


class IdempotencyConflict(ValueError):
    pass


class QueueFull(ValueError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_task(self, request: TaskCreateRequest, *, queue_max_size: int) -> tuple[TaskStateResponse, bool]:
        task_id = f"task_{uuid4().hex}"
        now = _utc_now_iso()
        request_json = request.model_dump(mode="json")
        canonical = json.dumps(request_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        progress = {
            "phase": TaskStatus.queued.value,
            "invocations_completed": 0,
            "invocations_total": 1
            if request.execution.strategy == ExecutionStrategy.single
            else request.execution.max_proposers,
        }

        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id, request_hash FROM tasks WHERE idempotency_key = ?", (request.idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict(request.idempotency_key)
                return self.get_task(existing["id"]), False
            queued = connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued'").fetchone()[0]
            if int(queued) >= queue_max_size:
                raise QueueFull("QUEUE_FULL")
            row = connection.execute("SELECT COALESCE(MAX(queue_position), 0) AS pos FROM tasks").fetchone()
            queue_position = int(row["pos"]) + 1 if row else 1
            connection.execute(
                """
                INSERT INTO tasks (
                    id, request_id, idempotency_key, request_hash, request_json, status, priority, queue_position,
                    progress_json, task_group, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, request.request_id, request.idempotency_key, request_hash, dumps_json(request_json),
                 TaskStatus.queued.value, request.priority, queue_position, dumps_json(progress),
                 request.group, now, now),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.created", dumps_json({"status": TaskStatus.queued.value}), now),
            )
        return self.get_task(task_id), True

    def get_task(self, task_id: str) -> TaskStateResponse:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return self._row_to_task_state(row)

    def get_task_request(self, task_id: str) -> TaskCreateRequest:
        row = self.db.query_one("SELECT request_json, kind FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        kind = TaskKind(row["kind"])
        if kind != TaskKind.inference:
            raise ValueError(
                f"la tarea {task_id} es del carril {kind.value} y no lleva una petición de inferencia"
            )
        return TaskCreateRequest.model_validate(loads_json(row["request_json"]))

    def has_unfinished_task(self) -> bool:
        """¿Queda trabajo por hacer: ejecutándose, esperando turno o en cola?

        Más amplia que `has_active_task` a propósito. La usa el borrado de
        modelos del panel: una tarea que aún no ha corrido puede tener por
        delante justo el modelo que se va a borrar, y se encontraría con un
        MODEL_UNAVAILABLE que nadie sabría explicar. Con un solo workflow
        activo, esperar a que la cola se vacíe es cuestión de poco.
        """
        statuses = (*ACTIVE_TASK_STATUSES, *PENDING_QUEUE_STATUSES,
                    TaskStatus.waiting_for_dependencies.value, TaskStatus.waiting_for_tools.value)
        marks = ",".join("?" for _ in statuses)
        row = self.db.query_one(
            f"SELECT 1 FROM tasks WHERE status IN ({marks}) LIMIT 1", statuses,
        )
        return row is not None

    def has_active_task(self) -> bool:
        """¿Está la máquina ocupada ahora mismo, en cualquiera de los carriles?

        La consulta el sondeo en sombra para no meter trabajo local en una
        máquina que ya trabaja. Cuenta las conversiones a propósito: son las
        que más recursos consumen y, antes de los carriles, eran invisibles —
        el broker creía la máquina libre con dos Doclings dentro. Es una foto
        del instante: el despachador puede reclamar la siguiente justo después.
        """
        marks = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
        row = self.db.query_one(
            f"SELECT 1 FROM tasks WHERE status IN ({marks}) LIMIT 1", ACTIVE_TASK_STATUSES,
        )
        return row is not None

    # Estados desde los que una tarea de inferencia puede entrar a ejecutarse:
    # la que nunca ha corrido y la que cedió el turno esperando memoria. Son un
    # único conjunto a propósito — para el orden de la cola, esperar memoria no
    # degrada a la tarea ni la manda al final.
    CLAIMABLE_INFERENCE_STATUSES = (
        TaskStatus.queued.value, TaskStatus.waiting_for_memory.value,
        TaskStatus.waiting_for_dependencies.value,
    )

    def claim_next_queued_task_id(self) -> str | None:
        """Reclama como máximo un workflow de INFERENCIA en una transacción
        inmediata. El carril de ingesta va por `claim_next_ingestion_task`.

        Dos reglas por encima del orden FIFO, ambas por la memoria:

        1. **Se salta a quien está esperando su turno.** Una tarea aplazada
           lleva `not_before` en el futuro; mientras tanto pasa la siguiente
           que sí quepa. Es lo que evita que la máquina se quede ociosa
           delante de una cola llena solo porque la cabeza no cabe.
        2. **Salvo que alguien tenga reserva.** Tras `memory_reserve_after`
           turnos cedidos, la tarea aplazada reserva el suyo y nadie la
           adelanta hasta que la ventana expire. Sin esto, una petición grande
           no correría jamás mientras siguieran llegando pequeñas.
        """
        active = ACTIVE_INFERENCE_STATUSES
        marks = ",".join("?" for _ in active)
        claimable = self.CLAIMABLE_INFERENCE_STATUSES
        claim_marks = ",".join("?" for _ in claimable)
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            if connection.execute(
                f"SELECT 1 FROM tasks WHERE kind = 'inference' AND status IN ({marks}) LIMIT 1",
                active,
            ).fetchone():
                return None
            # La reserva vencida se limpia antes de mirar nada: si siguiera en
            # pie tras expirar, la cola quedaría congelada para siempre cuando
            # la memoria no llega a liberarse.
            connection.execute(
                "UPDATE tasks SET memory_reserved_until = NULL, memory_deferrals = 0 "
                "WHERE memory_reserved_until IS NOT NULL AND memory_reserved_until <= ?",
                (now,),
            )
            order = "ORDER BY queue_position ASC, priority ASC, created_at ASC LIMIT 1"
            reserved = connection.execute(
                f"SELECT id, progress_json FROM tasks WHERE status IN ({claim_marks}) "
                f"AND kind = 'inference' AND memory_reserved_until > ? {order}",
                (*claimable, now),
            ).fetchone()
            if reserved is not None:
                # Con reserva en pie solo corre la reservada, y solo cuando le
                # toca reintentar. Si aún no le toca, nadie corre: eso es
                # exactamente lo que la reserva significa.
                row = connection.execute(
                    "SELECT id, progress_json FROM tasks WHERE id = ? "
                    "AND (not_before IS NULL OR not_before <= ?)",
                    (reserved["id"], now),
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT id, progress_json FROM tasks WHERE status IN ({claim_marks}) "
                    f"AND kind = 'inference' AND (not_before IS NULL OR not_before <= ?) {order}",
                    (*claimable, now),
                ).fetchone()
            if row is None:
                return None
            progress = loads_json(row["progress_json"], {})
            progress["phase"] = TaskStatus.routing.value
            cursor = connection.execute(
                f"UPDATE tasks SET status = ?, queue_position = NULL, progress_json = ?, "
                f"not_before = NULL, updated_at = ? "
                f"WHERE id = ? AND status IN ({claim_marks})",
                (TaskStatus.routing.value, dumps_json(progress), now, row["id"], *claimable),
            )
            if cursor.rowcount != 1:
                return None
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "task.claimed", dumps_json({"status": "routing"}), now),
            )
            return str(row["id"])

    def pending_dependencies(
        self, task_id: str, depends_on: list[str], depends_on_group: str | None,
    ) -> dict[str, Any]:
        """Qué le falta a una tarea para poder correr. Diccionario vacío = nada.

        «Terminada» es cualquier estado terminal, incluidos `failed` y
        `cancelled`: la dependencia se considera resuelta y quien esperaba sigue
        adelante con un aviso (ver ConsensusCoordinator._await_dependencies).
        Esperar eternamente a algo que ya no va a terminar sería peor.

        Un id desconocido NO bloquea: no se puede esperar a una tarea que no
        existe, y un cliente que manda un id caducado dejaría la suya colgada
        para siempre. Se informa aparte para que el aviso lo diga.
        """
        terminal = tuple(status.value for status in TERMINAL_STATUSES)
        pending: dict[str, Any] = {}
        if depends_on:
            id_marks = ",".join("?" for _ in depends_on)
            terminal_marks = ",".join("?" for _ in terminal)
            rows = self.db.query_all(
                f"SELECT id, status FROM tasks WHERE id IN ({id_marks})", tuple(depends_on),
            )
            known = {str(row["id"]): str(row["status"]) for row in rows}
            unfinished = [
                task for task, status in known.items() if status not in terminal
            ]
            missing = [task for task in depends_on if task not in known]
            if unfinished:
                pending["tasks"] = sorted(unfinished)
            if missing:
                pending["unknown_tasks"] = sorted(missing)
        if depends_on_group:
            terminal_marks = ",".join("?" for _ in terminal)
            row = self.db.query_one(
                f"SELECT COUNT(*) AS pending FROM tasks WHERE task_group = ? AND id <> ? "
                f"AND status NOT IN ({terminal_marks})",
                (depends_on_group, task_id, *terminal),
            )
            remaining = int(row["pending"]) if row else 0
            if remaining:
                pending["group"] = depends_on_group
                pending["group_pending"] = remaining
        return pending

    def defer_task_for_dependencies(
        self, task_id: str, pending: dict[str, Any], *, wait_seconds: float,
    ) -> dict[str, Any]:
        """Devuelve la tarea a la cola porque aún le falta trabajo ajeno.

        Misma mecánica que `defer_task_for_memory` —conserva `queue_position` y
        vuelve a ser reclamable dentro de `wait_seconds`— pero sin reserva
        anti-inanición: aquí no compite por un recurso que otros le quiten, solo
        espera a que algo termine, y reservar turno no lo aceleraría.
        """
        now = datetime.now(timezone.utc)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT dependency_wait_json FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            previous = loads_json(row["dependency_wait_json"], {}) or {}
            not_before = (now + timedelta(seconds=wait_seconds)).isoformat()
            state = {
                **pending,
                "waits": int(previous.get("waits") or 0) + 1,
                "waiting_since": previous.get("waiting_since") or now.isoformat(),
                "retry_at": not_before,
            }
            connection.execute(
                "UPDATE tasks SET status = ?, not_before = ?, dependency_wait_json = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    TaskStatus.waiting_for_dependencies.value, not_before,
                    dumps_json(state), now.isoformat(), task_id,
                ),
            )
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.waiting_for_dependencies", dumps_json(state), now.isoformat()),
            )
            return state

    def dependency_wait_state(self, task_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT dependency_wait_json FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return loads_json(row["dependency_wait_json"], {}) or {}

    def clear_dependency_wait(self, task_id: str) -> None:
        self.db.execute(
            "UPDATE tasks SET not_before = NULL, dependency_wait_json = NULL WHERE id = ?",
            (task_id,),
        )

    def defer_task_for_memory(
        self,
        task_id: str,
        block: dict[str, Any],
        *,
        wait_seconds: float,
        reserve_after: int,
        reserve_window_seconds: float,
    ) -> dict[str, Any]:
        """Devuelve la tarea a la cola porque ahora mismo no hay memoria.

        No es un fallo y no se pierde trabajo: la tarea conserva su sitio en la
        cola (`queue_position` intacto) y vuelve a ser reclamable dentro de
        `wait_seconds`. Mientras tanto el despachador puede adelantar a quien
        sí quepa, que es de lo que se trata.

        Al alcanzar `reserve_after` turnos cedidos abre una ventana de reserva:
        durante ella nadie la adelanta. La ventana caduca sola para que una
        máquina que nunca libera memoria no acabe congelando toda la cola.

        Devuelve el estado de la espera, para el evento y para el panel.
        """
        now = datetime.now(timezone.utc)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT status, memory_deferrals, memory_reserved_until FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            deferrals = int(row["memory_deferrals"] or 0) + 1
            reserved_until = row["memory_reserved_until"]
            # La reserva se abre una vez al cruzar el umbral y no se renueva en
            # cada aplazamiento: renovarla la haría perpetua, que es justo lo
            # que la ventana existe para impedir.
            if deferrals >= reserve_after and not reserved_until:
                reserved_until = (
                    now + timedelta(seconds=reserve_window_seconds)
                ).isoformat()
            not_before = (now + timedelta(seconds=wait_seconds)).isoformat()
            state = {
                **block,
                "deferrals": deferrals,
                "waiting_since": block.get("waiting_since") or now.isoformat(),
                "retry_at": not_before,
                "reserved_until": reserved_until,
            }
            connection.execute(
                "UPDATE tasks SET status = ?, not_before = ?, memory_deferrals = ?, "
                "memory_reserved_until = ?, memory_block_json = ?, updated_at = ? WHERE id = ?",
                (
                    TaskStatus.waiting_for_memory.value, not_before, deferrals,
                    reserved_until, dumps_json(state), now.isoformat(), task_id,
                ),
            )
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.waiting_for_memory", dumps_json(state), now.isoformat()),
            )
            return state

    def clear_memory_wait(self, task_id: str) -> None:
        """Borra el rastro de la espera cuando la tarea por fin arranca.

        Se llama al empezar a ejecutar de verdad, no al reclamar: entre reclamo
        y ejecución todavía puede volver a no haber memoria, y en ese caso el
        contador de aplazamientos debe seguir contando."""
        self.db.execute(
            "UPDATE tasks SET not_before = NULL, memory_deferrals = 0, "
            "memory_reserved_until = NULL, memory_block_json = NULL WHERE id = ?",
            (task_id,),
        )

    def create_ingestion_task(self, file_id: str, filename: str, size_bytes: int) -> str:
        """Fila de tarea para una conversión de fichero.

        `request_json` no es un TaskCreateRequest: es un descriptor del trabajo.
        Una conversión no tiene prompt, ni modelo, ni estrategia, y fabricarle
        un contrato de inferencia sintético solo para que valide contaminaría
        el contrato con un caso que no es una inferencia. Quien lea la petición
        debe mirar antes el `kind` (ver `get_task_kind`).
        """
        task_id = f"task_{uuid4().hex}"
        now = _utc_now_iso()
        descriptor = {"file_id": file_id, "filename": filename, "size_bytes": size_bytes}
        progress = {"phase": TaskStatus.queued.value, "filename": filename}
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(queue_position), 0) AS pos FROM tasks"
            ).fetchone()
            queue_position = int(row["pos"]) + 1 if row else 1
            connection.execute(
                """
                INSERT INTO tasks (
                    id, request_id, request_json, kind, status, priority, queue_position,
                    progress_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id, f"ingestion-{file_id}", dumps_json(descriptor),
                    TaskKind.ingestion.value, TaskStatus.queued.value, 100, queue_position,
                    dumps_json(progress), now, now,
                ),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.created", dumps_json({"kind": TaskKind.ingestion.value}), now),
            )
        return task_id

    def get_task_kind(self, task_id: str) -> TaskKind:
        row = self.db.query_one("SELECT kind FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return TaskKind(row["kind"])

    def claim_next_ingestion_task(self, max_concurrent: int) -> tuple[str, str] | None:
        """Reclama una conversión si el carril tiene sitio: (task_id, file_id).

        A diferencia de la inferencia, aquí no hay invariante de uno: el límite
        es configurable porque convertir dos ficheros a la vez es razonable y
        convertir diez es asfixiar la máquina. Antes de los carriles no había
        límite ninguno: cada subida arrancaba su propio hilo."""
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            active = connection.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE kind = 'ingestion' AND status = ?",
                (TaskStatus.converting.value,),
            ).fetchone()
            if int(active["total"]) >= max_concurrent:
                return None
            row = connection.execute(
                "SELECT id, request_json, progress_json FROM tasks "
                "WHERE kind = 'ingestion' AND status = 'queued' "
                "ORDER BY queue_position ASC, created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            progress = loads_json(row["progress_json"], {})
            progress["phase"] = TaskStatus.converting.value
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, queue_position = NULL, progress_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (TaskStatus.converting.value, dumps_json(progress), now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "task.claimed", dumps_json({"status": TaskStatus.converting.value}), now),
            )
            descriptor = loads_json(row["request_json"], {}) or {}
            return str(row["id"]), str(descriptor.get("file_id") or "")

    def requeue_interrupted_ingestion_tasks(self) -> int:
        """Devuelve a la cola las conversiones que quedaron a medias.

        Su recuperación es más simple que la de inferencia: no hay invocaciones
        ambiguas que resolver ni coste que se haya podido facturar. Convertir
        otra vez es idempotente."""
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE kind = 'ingestion' AND status = ?",
                (TaskStatus.converting.value,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE tasks SET status = 'queued', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (row["id"], "task.requeued", dumps_json({"reason": "interrupted"}), now),
                )
            return len(rows)

    def ingestion_task_for_file(self, file_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT id FROM tasks WHERE kind = 'ingestion' AND request_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f"ingestion-{file_id}",),
        )
        return str(row["id"]) if row else None

    def update_task(
        self,
        task_id: str,
        status: TaskStatus,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        clear_queue_position: bool = False,
    ) -> TaskStateResponse:
        now = _utc_now_iso()
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, now]
        if progress is not None:
            assignments.append("progress_json = ?")
            params.append(dumps_json(progress))
        if result is not None:
            assignments.append("result_json = ?")
            params.append(dumps_json(result))
        if error is not None:
            assignments.append("error_json = ?")
            params.append(dumps_json(error))
        if clear_queue_position:
            assignments.append("queue_position = NULL")
        params.append(task_id)
        with self.db.transaction() as connection:
            connection.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", params)
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.status_changed", dumps_json({"status": status.value}), now),
            )
        return self.get_task(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        row = self.db.query_one("SELECT cancel_requested, status FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return bool(row["cancel_requested"]) or row["status"] == TaskStatus.cancelled.value

    def get_consensus_run_id(self, task_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT id FROM consensus_runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        return str(row["id"]) if row else None

    def start_invocation(
        self,
        task_id: str,
        run_id: str | None,
        role: str,
        model: ModelReference,
        task_type: str,
        was_loaded: bool | None = None,
    ) -> str:
        """Checkpoint pre-vuelo: la fila existe ANTES de llamar al proveedor.

        Si el proceso muere con la llamada en el aire, la recuperación
        encontrará esta fila en 'started' y sabrá que la inferencia pudo
        llegar a ejecutarse (y facturarse) aunque no haya respuesta persistida.
        task_type (app.task_classifier) permite luego segmentar las métricas
        de enrutamiento por naturaleza de la tarea (código/prosa/contexto
        largo) en vez de agregarlas todas en un único score.

        was_loaded registra si el modelo ya estaba en VRAM al arrancar: la
        latencia de un modelo local en frío incluye la carga desde disco, y
        mezclar ambas poblaciones en una media hace inservible la estimación
        de tiempo. None = no se sabe (o no aplica, como en cloud).
        """
        invocation_id = f"inv_{uuid4().hex}"
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO model_invocations (id, task_id, run_id, role, provider, deployment, model, "
                "task_type, was_loaded, tokens_input, tokens_output, cost_usd, started_at, status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, 'started', ?, ?)",
                (
                    invocation_id, task_id, run_id, role,
                    model.provider, model.deployment, model.model, task_type,
                    None if was_loaded is None else int(was_loaded),
                    now, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "model_invocation.started",
                    dumps_json({
                        "invocation_id": invocation_id,
                        "role": role,
                        "provider": model.provider,
                        "deployment": model.deployment,
                        "model": model.model,
                    }),
                    now,
                ),
            )
        return invocation_id

    def complete_invocation(self, invocation_id: str, task_id: str, output: ModelOutput) -> None:
        """Cierra el checkpoint con la respuesta real (tokens, coste, latencia)."""
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET output_json = ?, tokens_input = ?, tokens_output = ?, "
                "cost_usd = ?, latency_ms = ?, completed_at = ?, status = 'completed', updated_at = ? "
                "WHERE id = ?",
                (
                    dumps_json(output.technical_output()),
                    output.tokens_input,
                    output.tokens_output,
                    output.cost_usd,
                    output.latency_ms,
                    now,
                    now,
                    invocation_id,
                ),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "model_invocation.completed", dumps_json({"invocation_id": invocation_id}), now),
            )

    def fail_invocation(
        self,
        invocation_id: str,
        task_id: str,
        code: str,
        message: str,
        output: ModelOutput | None = None,
    ) -> None:
        """Cierra el checkpoint como fallido.

        output es lo que el modelo llegó a devolver cuando quien declara el
        fallo es el broker DESPUÉS de recibir la respuesta (PROMPT_ECHOED es el
        caso vivo). Sin él la fila quedaba a cero tokens, sin coste y sin
        texto: el gasto real desaparecía de las métricas y de la respuesta que
        rompió la tarea no quedaba nada que auditar, justo la que más falta
        hace ver. Un fallo de transporte no trae output y la fila se queda como
        siempre.
        """
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            if output is None:
                connection.execute(
                    "UPDATE model_invocations SET completed_at = ?, status = 'failed', "
                    "error_code = ?, updated_at = ? WHERE id = ?",
                    (now, code, now, invocation_id),
                )
            else:
                connection.execute(
                    "UPDATE model_invocations SET output_json = ?, tokens_input = ?, "
                    "tokens_output = ?, cost_usd = ?, latency_ms = ?, completed_at = ?, "
                    "status = 'failed', error_code = ?, updated_at = ? WHERE id = ?",
                    (
                        dumps_json(output.technical_output()),
                        output.tokens_input,
                        output.tokens_output,
                        output.cost_usd,
                        output.latency_ms,
                        now,
                        code,
                        now,
                        invocation_id,
                    ),
                )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "model_invocation.failed",
                    dumps_json({"invocation_id": invocation_id, "code": code, "message": message}),
                    now,
                ),
            )

    def set_stage_status(self, task_id: str, run_id: str | None, stage_type: str, status: str) -> None:
        """Checkpoint de etapa del consenso (queued → running → completed/failed).

        Da observabilidad del punto exacto de una interrupción; las tareas
        single no tienen run ni etapas y la llamada es un no-op.
        """
        if run_id is None:
            return
        now = _utc_now_iso()
        self.db.execute(
            "UPDATE stages SET status = ?, "
            "attempts = attempts + CASE WHEN ? = 'running' THEN 1 ELSE 0 END, updated_at = ? "
            "WHERE task_id = ? AND run_id = ? AND stage_type = ?",
            (status, status, now, task_id, run_id, stage_type),
        )

    def complete_single_task(
        self,
        task_id: str,
        invocation_id: str,
        model: ModelReference,
        output: ModelOutput,
        *,
        progress: dict[str, Any],
        result: dict[str, Any],
    ) -> str:
        """Cierra el checkpoint de invocación y el resultado terminal en la
        misma transacción: o queda todo persistido o nada (la recuperación
        tratará la fila 'started' como ambigua)."""
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or row["status"] == TaskStatus.cancelled.value:
                raise ProviderError("TASK_CANCELLED", "La tarea fue cancelada antes de persistir el resultado")
            connection.execute(
                "UPDATE model_invocations SET output_json = ?, tokens_input = ?, tokens_output = ?, "
                "cost_usd = ?, latency_ms = ?, completed_at = ?, status = 'completed', updated_at = ? "
                "WHERE id = ?",
                (
                    dumps_json(output.technical_output()), output.tokens_input, output.tokens_output,
                    output.cost_usd, output.latency_ms, now, now, invocation_id,
                ),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, progress_json = ?, result_json = ?, queue_position = NULL, updated_at = ? "
                "WHERE id = ?",
                (TaskStatus.completed.value, dumps_json(progress), dumps_json(result), now, task_id),
            )
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "model_invocation.completed", dumps_json({
                    "invocation_id": invocation_id, "role": "single", "model": model.model,
                }), now),
            )
            connection.execute(
                "INSERT INTO events(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "task.status_changed", dumps_json({"status": TaskStatus.completed.value}), now),
            )
        return invocation_id

    def record_artifact(
        self,
        task_id: str,
        run_id: str | None,
        invocation_id: str | None,
        artifact_type: str,
        artifact: ArtifactRecord,
    ) -> str:
        artifact_id = f"art_{uuid4().hex}"
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, task_id, run_id, invocation_id, artifact_type,
                    path, sha256, size_bytes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    task_id,
                    run_id,
                    invocation_id,
                    artifact_type,
                    artifact.path,
                    artifact.sha256,
                    artifact.size_bytes,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "artifact.created",
                    dumps_json({"artifact_id": artifact_id, "type": artifact_type, "path": artifact.path}),
                    now,
                ),
            )
        return artifact_id

    def list_queue(self) -> QueueResponse:
        rows = self.db.query_all(
            """
            SELECT * FROM tasks
            ORDER BY
              CASE
                WHEN status IN ('queued','waiting_for_memory') THEN 0
                WHEN status IN ('routing','planning','resource_planning','chunking','generating','proposing','evaluating','debating','synthesizing','verifying') THEN 1
                ELSE 2
              END,
              queue_position ASC,
              updated_at DESC
            """
        )
        pending: list[QueueItem] = []
        active: list[QueueItem] = []
        terminal: list[QueueItem] = []
        active_statuses = {
            TaskStatus.routing,
            TaskStatus.planning,
            TaskStatus.resource_planning,
            TaskStatus.chunking,
            TaskStatus.generating,
            TaskStatus.proposing,
            TaskStatus.evaluating,
            TaskStatus.debating,
            TaskStatus.synthesizing,
            TaskStatus.verifying,
            TaskStatus.converting,
        }
        terminal_statuses = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
        for row in rows:
            item = self._row_to_queue_item(row)
            if item.status.value in PENDING_QUEUE_STATUSES:
                pending.append(item)
            elif item.status in active_statuses:
                active.append(item)
            elif item.status in terminal_statuses:
                terminal.append(item)
        return QueueResponse(pending=pending, active=active, terminal=terminal)

    def request_cancel(self, task_id: str) -> TaskStateResponse:
        task = self.get_task(task_id)
        now = _utc_now_iso()
        if task.status in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}:
            return task
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET cancel_requested = 1, status = ?, queue_position = NULL, updated_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (TaskStatus.cancelled.value, now, task_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, "task.cancelled", dumps_json({"requested": True}), now),
                )
        return self.get_task(task_id)

    def reorder_queue(self, task_ids: list[str]) -> QueueResponse:
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            marks = ",".join("?" for _ in PENDING_QUEUE_STATUSES)
            rows = connection.execute(
                f"SELECT id FROM tasks WHERE status IN ({marks})", PENDING_QUEUE_STATUSES,
            ).fetchall()
            current_ids = {str(row["id"]) for row in rows}
            if set(task_ids) != current_ids:
                raise ValueError("task_ids must contain exactly all queued task ids")
            for position, task_id in enumerate(task_ids, start=1):
                connection.execute(
                    f"UPDATE tasks SET queue_position = ?, updated_at = ? "
                    f"WHERE id = ? AND status IN ({marks})",
                    (position, now, task_id, *PENDING_QUEUE_STATUSES),
                )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (None, "queue.reordered", dumps_json({"task_ids": task_ids}), now),
            )
        return self.list_queue()

    def event_payloads(self, task_id: str, event_type: str) -> list[dict[str, Any]]:
        """Payloads de un tipo de evento de una tarea, en orden cronológico."""
        rows = self.db.query_all(
            "SELECT payload_json FROM events WHERE task_id = ? AND event_type = ? ORDER BY id ASC",
            (task_id, event_type),
        )
        return [loads_json(row["payload_json"], {}) or {} for row in rows]

    def append_result_warnings(self, task_id: str, warnings: list[str]) -> None:
        """Añade avisos al resultado ya escrito, sin tocar nada más.

        Los escribe quien sabe del aviso pero no del resultado: la espera por
        dependencias ocurre antes de elegir estrategia y el resultado lo redacta
        cada estrategia por su cuenta. Sin este canal el aviso se quedaría en el
        log de eventos, que es justo donde nadie lo va a ver.
        """
        if not warnings:
            return
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if row is None or not row["result_json"]:
                return
            result = loads_json(row["result_json"], None)
            if not isinstance(result, dict):
                return
            existing = result.get("warnings")
            result["warnings"] = [*existing, *warnings] if isinstance(existing, list) else list(warnings)
            connection.execute(
                "UPDATE tasks SET result_json = ?, updated_at = ? WHERE id = ?",
                (dumps_json(result), _utc_now_iso(), task_id),
            )

    def add_event(self, task_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event_type, dumps_json(payload), _utc_now_iso()),
        )

    def record_routing_case(
        self,
        task_id: str,
        signal_bucket: str,
        chosen_strategy: str,
        final_strategy: str,
        escalated: bool,
        status: str,
        cost_usd: float,
        latency_ms: float | None,
    ) -> None:
        """Persiste un caso de enrutamiento (señales → decisión → resultado) para
        que el aprendizaje adaptativo (pieza 3) aprenda de la evidencia."""
        self.db.execute(
            "INSERT INTO routing_cases (task_id, signal_bucket, chosen_strategy, final_strategy, "
            "escalated, status, cost_usd, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, signal_bucket, chosen_strategy, final_strategy,
                1 if escalated else 0, status, float(cost_usd), latency_ms, _utc_now_iso(),
            ),
        )

    def routing_cases_for_bucket(self, signal_bucket: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT chosen_strategy, final_strategy, escalated, status, cost_usd, latency_ms "
            "FROM routing_cases WHERE signal_bucket = ? ORDER BY created_at DESC LIMIT ?",
            (signal_bucket, limit),
        )
        return [
            {
                "chosen_strategy": row["chosen_strategy"],
                "final_strategy": row["final_strategy"],
                "escalated": bool(row["escalated"]),
                "status": row["status"],
                "cost_usd": float(row["cost_usd"] or 0),
                "latency_ms": row["latency_ms"],
            }
            for row in rows
        ]

    def latest_event_payload(self, task_id: str, event_type: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT payload_json FROM events WHERE task_id = ? AND event_type = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id, event_type),
        )
        if row is None:
            return None
        payload = loads_json(row["payload_json"], None)
        return payload if isinstance(payload, dict) else None

    def has_event(self, task_id: str, event_type: str) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM events WHERE task_id = ? AND event_type = ? LIMIT 1",
            (task_id, event_type),
        ) is not None

    def routing_case_buckets(self, limit: int = 50) -> list[dict[str, Any]]:
        """Resumen agregado de casos por tipo de petición (bucket de señales):
        total, escalados, y desglose por estrategia final con tasa de éxito."""
        rows = self.db.query_all(
            "SELECT signal_bucket, final_strategy, chosen_strategy, escalated, status, "
            "COUNT(*) AS n FROM routing_cases "
            "GROUP BY signal_bucket, final_strategy, chosen_strategy, escalated, status",
        )
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = buckets.setdefault(row["signal_bucket"], {
                "signal_bucket": row["signal_bucket"], "total": 0, "escalated": 0,
                "single_chosen": 0, "strategies": {},
            })
            n = int(row["n"])
            bucket["total"] += n
            if row["escalated"]:
                bucket["escalated"] += n
            if row["chosen_strategy"] == "single":
                bucket["single_chosen"] += n
            strat = bucket["strategies"].setdefault(
                row["final_strategy"], {"total": 0, "completed": 0},
            )
            strat["total"] += n
            if row["status"] == "completed":
                strat["completed"] += n
        ordered = sorted(buckets.values(), key=lambda b: b["total"], reverse=True)
        return ordered[:limit]

    def task_cost_and_latency(self, task_id: str) -> tuple[float, float | None]:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost, SUM(latency_ms) AS latency "
            "FROM model_invocations WHERE task_id = ?",
            (task_id,),
        )
        if row is None:
            return 0.0, None
        return float(row["cost"] or 0), (float(row["latency"]) if row["latency"] is not None else None)

    def pause_for_client_tools(
        self,
        task_id: str,
        agent_state: dict[str, Any],
        pending_tool_calls: list[dict[str, Any]],
        progress: dict[str, Any],
    ) -> None:
        """Congela la conversación del agente y deja la tarea a la espera de que
        el cliente resuelva las tool_calls pendientes (passthrough)."""
        now = _utc_now_iso()
        result = {"pending_tool_calls": pending_tool_calls, "status": "waiting_for_tools"}
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, agent_state_json = ?, progress_json = ?, "
                "result_json = ?, queue_position = NULL, updated_at = ? WHERE id = ?",
                (
                    TaskStatus.waiting_for_tools.value, dumps_json(agent_state),
                    dumps_json(progress), dumps_json(result), now, task_id,
                ),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "agent.paused", dumps_json({"pending_tool_calls": pending_tool_calls}), now),
            )

    def load_agent_state(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT agent_state_json FROM tasks WHERE id = ?", (task_id,))
        if row is None or row["agent_state_json"] is None:
            return None
        state = loads_json(row["agent_state_json"], None)
        return state if isinstance(state, dict) else None

    def resume_with_tool_results(self, task_id: str, tool_results: list[dict[str, str]]) -> None:
        """Añade los resultados de las tools del cliente a la conversación y
        re-encola la tarea para que el dispatcher reanude el bucle del agente."""
        now = _utc_now_iso()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT status, agent_state_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] != TaskStatus.waiting_for_tools.value:
                raise ValueError(f"La tarea no está esperando tools (estado {row['status']})")
            state = loads_json(row["agent_state_json"], None)
            if not isinstance(state, dict):
                raise ValueError("No hay estado de agente que reanudar")
            pending_ids = {str(item.get("id")) for item in state.get("pending_tool_calls") or []}
            provided_ids = {str(item.get("tool_call_id")) for item in tool_results}
            if pending_ids != provided_ids:
                raise ValueError(
                    f"Los tool_call_id no coinciden con los pendientes: esperados {sorted(pending_ids)}"
                )
            messages = list(state.get("messages") or [])
            for item in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("tool_call_id")),
                    "content": str(item.get("content") or ""),
                })
            state["messages"] = messages
            state["pending_tool_calls"] = []
            state["resumed"] = True
            # Reentra por ORDEN DE LLEGADA, no por el final de la cola. Es la
            # misma regla que ya rige para quien cede el turno esperando memoria
            # (ver CLAIMABLE_INFERENCE_STATUSES): ceder el turno no degrada a la
            # tarea. Mandarla al final penalizaba justo al trabajo que ya está
            # en vuelo —una investigación de diez pasos pagaba diez veces la
            # espera de la cola entera— y ninguna prioridad podía rescatarla,
            # porque `queue_position` manda sobre `priority` en el reclamo.
            # Se cuela delante de lo que llegó después de ella; los empates los
            # rompe `created_at`, que es el desempate final del ORDER BY.
            newer = connection.execute(
                "SELECT MIN(queue_position) AS pos FROM tasks "
                "WHERE status = 'queued' AND queue_position IS NOT NULL "
                "AND created_at > (SELECT created_at FROM tasks WHERE id = ?)",
                (task_id,),
            ).fetchone()
            if newer is not None and newer["pos"] is not None:
                position = int(newer["pos"]) - 1
            else:
                queue_row = connection.execute(
                    "SELECT COALESCE(MAX(queue_position), -1) + 1 AS pos FROM tasks WHERE status = 'queued'"
                ).fetchone()
                position = int(queue_row["pos"])
            connection.execute(
                "UPDATE tasks SET status = 'queued', agent_state_json = ?, result_json = NULL, "
                "queue_position = ?, updated_at = ? WHERE id = ?",
                (dumps_json(state), position, now, task_id),
            )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "agent.resumed", dumps_json({"tool_results": len(tool_results)}), now),
            )

    def recover_interrupted_tasks(self, max_attempts: int | None = None) -> int:
        """Re-encola tareas interrumpidas según lo que había en vuelo al morir.

        Las invocaciones que quedaron en 'started' pasan a 'ambiguous': no se
        sabe si el proveedor llegó a ejecutar (y facturar) la llamada. Si
        alguna era remota, la tarea NO se reintenta automáticamente — repetir
        el workflow podría pagar dos veces la misma inferencia — y se marca
        failed con código explícito para que el operador decida. Con solo
        llamadas locales el reintento automático es seguro: cuesta cómputo,
        no dinero.
        """
        # Solo inferencia: las conversiones interrumpidas las recupera
        # `requeue_interrupted_ingestion_tasks`, que no tiene que razonar sobre
        # invocaciones ambiguas ni sobre coste ya facturado.
        active_statuses = ACTIVE_INFERENCE_STATUSES
        placeholders = ",".join("?" for _ in active_statuses)
        now = _utc_now_iso()
        recovered = 0
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT id, attempt, progress_json FROM tasks "
                f"WHERE kind = 'inference' AND status IN ({placeholders})",
                active_statuses,
            ).fetchall()
            position = 0
            for row in rows:
                started = connection.execute(
                    "SELECT id, provider, deployment, model FROM model_invocations "
                    "WHERE task_id = ? AND status = 'started'",
                    (row["id"],),
                ).fetchall()
                for item in started:
                    connection.execute(
                        "UPDATE model_invocations SET status = 'ambiguous', updated_at = ? WHERE id = ?",
                        (now, item["id"]),
                    )
                    connection.execute(
                        "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                        (
                            row["id"],
                            "model_invocation.ambiguous",
                            dumps_json({
                                "invocation_id": item["id"],
                                "provider": item["provider"],
                                "deployment": item["deployment"],
                                "model": item["model"],
                            }),
                            now,
                        ),
                    )
                ambiguous_remote = [
                    f"{item['provider']}/{item['deployment']}/{item['model']}"
                    for item in started
                    if not is_local_deployment(item["deployment"])
                ]
                if ambiguous_remote:
                    progress = loads_json(row["progress_json"], {})
                    progress["phase"] = TaskStatus.failed.value
                    error = {
                        "code": "RECOVERY_AMBIGUOUS_REMOTE_CALL",
                        "message": (
                            "La tarea se interrumpió con llamadas remotas en vuelo que pudieron "
                            "facturarse: " + ", ".join(ambiguous_remote) + ". No se reintenta "
                            "automáticamente; verifica el consumo en el proveedor y reenvía la "
                            "tarea si procede."
                        ),
                        "retryable": False,
                        "ambiguous_invocations": ambiguous_remote,
                    }
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = 'failed',
                            queue_position = NULL,
                            progress_json = ?,
                            error_json = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (dumps_json(progress), dumps_json(error), now, row["id"]),
                    )
                    connection.execute(
                        "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                        (
                            row["id"],
                            "task.status_changed",
                            dumps_json({"status": "failed", "code": "RECOVERY_AMBIGUOUS_REMOTE_CALL"}),
                            now,
                        ),
                    )
                    continue
                next_attempt = int(row["attempt"]) + 1
                if max_attempts is not None and next_attempt >= max_attempts:
                    progress = loads_json(row["progress_json"], {})
                    progress["phase"] = TaskStatus.failed.value
                    error = {
                        "code": "TASK_RETRY_LIMIT_EXCEEDED",
                        "message": (
                            f"La tarea fue interrumpida {next_attempt} veces y alcanzó el límite "
                            f"de {max_attempts} intentos"
                        ),
                        "retryable": False,
                    }
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = 'failed',
                            queue_position = NULL,
                            attempt = ?,
                            progress_json = ?,
                            error_json = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (next_attempt, dumps_json(progress), dumps_json(error), now, row["id"]),
                    )
                    connection.execute(
                        "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                        (
                            row["id"],
                            "task.status_changed",
                            dumps_json({"status": "failed", "code": "TASK_RETRY_LIMIT_EXCEEDED"}),
                            now,
                        ),
                    )
                    continue
                position += 1
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'queued',
                        queue_position = COALESCE(queue_position, ?),
                        attempt = attempt + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (position, now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (row["id"], "task.recovered", dumps_json({"status": "queued"}), now),
                )
                recovered += 1
        return recovered

    def _row_to_task_state(self, row: Any) -> TaskStateResponse:
        kind = TaskKind(row["kind"] if "kind" in row.keys() else TaskKind.inference.value)
        # El request_json del carril de ingesta es un descriptor de conversión,
        # no un TaskCreateRequest: validarlo como tal reventaría. Es la razón
        # de que haya que mirar el kind ANTES de interpretar la petición.
        request = (
            TaskCreateRequest.model_validate(loads_json(row["request_json"]))
            if kind == TaskKind.inference
            else None
        )
        return TaskStateResponse(
            task_id=row["id"],
            kind=kind,
            status=TaskStatus(row["status"]),
            request_id=row["request_id"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            execution_strategy=request.execution.strategy if request else None,
            execution_preset=request.execution.preset if request else None,
            selection_mode=request.execution.selection.mode if request else None,
            progress=loads_json(row["progress_json"], {}),
            result=loads_json(row["result_json"], None),
            error=loads_json(row["error_json"], None),
        )

    def _row_to_queue_item(self, row: Any) -> QueueItem:
        return QueueItem(
            task_id=row["id"],
            kind=TaskKind(row["kind"] if "kind" in row.keys() else TaskKind.inference.value),
            status=TaskStatus(row["status"]),
            request_id=row["request_id"],
            priority=row["priority"],
            queue_position=row["queue_position"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )


