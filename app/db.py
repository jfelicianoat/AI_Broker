from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str | Path, journal_mode: str = "WAL") -> None:
        self.path = Path(path)
        self.journal_mode = journal_mode
        self._lock = threading.RLock()
        self._in_transaction = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA journal_mode = {journal_mode}")
        # Procesos externos (backups, herramientas) pueden mantener locks breves:
        # mejor esperar unos segundos que fallar con "database is locked".
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                request_id TEXT,
                idempotency_key TEXT,
                request_hash TEXT,
                request_json TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'inference',
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                queue_position INTEGER,
                progress_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error_json TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS consensus_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                strategy TEXT NOT NULL,
                preset TEXT NOT NULL,
                selection_mode TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                rubric_json TEXT,
                limits_json TEXT NOT NULL,
                consensus_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS stages (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES consensus_runs(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                stage_type TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                dependencies_json TEXT NOT NULL DEFAULT '[]',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS model_invocations (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES consensus_runs(id) ON DELETE CASCADE,
                stage_id TEXT REFERENCES stages(id) ON DELETE SET NULL,
                role TEXT NOT NULL,
                provider TEXT NOT NULL,
                deployment TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_hash TEXT,
                output_json TEXT,
                tokens_input INTEGER DEFAULT 0,
                tokens_output INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                latency_ms REAL,
                started_at TEXT,
                completed_at TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES consensus_runs(id) ON DELETE CASCADE,
                invocation_id TEXT REFERENCES model_invocations(id) ON DELETE SET NULL,
                artifact_type TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS routing_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                signal_bucket TEXT NOT NULL,
                chosen_strategy TEXT NOT NULL,
                final_strategy TEXT NOT NULL,
                escalated INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                cost_usd REAL NOT NULL DEFAULT 0,
                latency_ms REAL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ingested_files (
                id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                kind TEXT NOT NULL,
                engine TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_json TEXT,
                original_path TEXT NOT NULL,
                markdown_path TEXT,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_ingested_files_sha
            ON ingested_files(sha256, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_queue
            ON tasks(status, queue_position, priority, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_events_task
            ON events(task_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_updated
            ON tasks(status, updated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_invocations_task
            ON model_invocations(task_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_invocations_usage
            ON model_invocations(status, updated_at, provider)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_routing_cases_bucket
            ON routing_cases(signal_bucket, created_at)
            """,
        ]
        with self._lock:
            for statement in statements:
                self._conn.execute(statement)
            task_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "idempotency_key" not in task_columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT")
            if "request_hash" not in task_columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN request_hash TEXT")
            if "agent_state_json" not in task_columns:
                # Conversación del agente persistida al pausar por tools del cliente.
                self._conn.execute("ALTER TABLE tasks ADD COLUMN agent_state_json TEXT")
            if "kind" not in task_columns:
                # Carril de trabajo (app.schemas.TaskKind). Todo lo anterior a
                # los carriles es inferencia, de ahí el default: la migración
                # no necesita reescribir ninguna fila.
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'inference'"
                )
            if "not_before" not in task_columns:
                # Momento a partir del cual la tarea vuelve a ser reclamable.
                # Lo pone el aplazamiento por memoria: reintentar en bucle
                # cerrado contra una máquina llena solo quema CPU. NULL = ya.
                self._conn.execute("ALTER TABLE tasks ADD COLUMN not_before TEXT")
            if "memory_deferrals" not in task_columns:
                # Veces que esta tarea ha cedido el turno por falta de memoria.
                # Es el contador que dispara la reserva anti-inanición.
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN memory_deferrals INTEGER NOT NULL DEFAULT 0"
                )
            if "memory_reserved_until" not in task_columns:
                # Ventana durante la cual NADIE adelanta a esta tarea. Es una
                # ventana y no una reserva perpetua a propósito: si la memoria
                # no se libera nunca, una reserva sin caducidad congelaría la
                # cola entera en vez de dejar pasar a quien sí cabe.
                self._conn.execute("ALTER TABLE tasks ADD COLUMN memory_reserved_until TEXT")
            if "task_group" not in task_columns:
                # Etiqueta de agrupación declarada por el cliente
                # (TaskCreateRequest.group). Va en columna, no en el JSON de la
                # petición, porque se consulta en cada reclamo para saber si un
                # grupo está drenado: mirarlo dentro del JSON obligaría a leer
                # y parsear la cola entera en cada vuelta del despachador.
                # "group" es palabra reservada de SQL, de ahí el nombre.
                self._conn.execute("ALTER TABLE tasks ADD COLUMN task_group TEXT")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_group ON tasks(task_group, status)"
                )
            if "dependency_wait_json" not in task_columns:
                # Qué está esperando la tarea y desde cuándo, cuando el estado
                # es waiting_for_dependencies. Mismo papel que
                # memory_block_json: sin esto el panel solo puede decir
                # "espera", que no ayuda a decidir si cancelar.
                self._conn.execute("ALTER TABLE tasks ADD COLUMN dependency_wait_json TEXT")
            if "memory_block_json" not in task_columns:
                # Qué ocupaba la memoria la última vez que se intentó: modelo
                # pedido, bytes necesarios y quién los tenía. Sin esto el panel
                # solo puede decir "espera", que no ayuda a decidir si cancelar.
                self._conn.execute("ALTER TABLE tasks ADD COLUMN memory_block_json TEXT")
            invocation_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(model_invocations)").fetchall()
            }
            if "started_at" not in invocation_columns:
                self._conn.execute("ALTER TABLE model_invocations ADD COLUMN started_at TEXT")
            if "completed_at" not in invocation_columns:
                self._conn.execute("ALTER TABLE model_invocations ADD COLUMN completed_at TEXT")
            if "task_type" not in invocation_columns:
                # Segmenta las métricas de enrutamiento por naturaleza de la
                # tarea (app.task_classifier): NULL en filas anteriores a esta
                # columna, se tratan como sin clasificar y no participan en el
                # score adaptativo por tipo.
                self._conn.execute("ALTER TABLE model_invocations ADD COLUMN task_type TEXT")
            if "was_loaded" not in invocation_columns:
                # ¿Estaba el modelo ya cargado en VRAM cuando empezó esta
                # invocación? Un modelo local en frío paga la carga desde disco
                # dentro de su propia latencia, así que sin este dato la media
                # mezcla dos poblaciones muy distintas y el tiempo estimado no
                # sirve para decidir AHORA. NULL = desconocido (filas previas y
                # proveedores cloud, que no cargan nada).
                self._conn.execute("ALTER TABLE model_invocations ADD COLUMN was_loaded INTEGER")
            if "error_code" not in invocation_columns:
                # Código del fallo (app.providers.base.ProviderError). Hasta
                # ahora solo vivía en el log de eventos, así que las métricas
                # veían "failed" sin poder distinguir un modelo que devuelve
                # basura de uno que se quedó sin memoria: el primero hay que
                # apartarlo, el segundo solo esperar. NULL en filas previas y
                # en las que terminaron bien.
                self._conn.execute("ALTER TABLE model_invocations ADD COLUMN error_code TEXT")
            file_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(ingested_files)").fetchall()
            }
            if "describe_images" not in file_columns:
                # Política de imágenes CON LA QUE SE CONVIRTIÓ este fichero, ya
                # resuelta (0/1, nunca "heredar"). Es lo que decide si el
                # Markdown guardado lleva descripciones de las figuras, así que
                # también participa en la deduplicación por sha256: reutilizar
                # una conversión sin descripciones cuando se piden con ellas
                # devolvería un documento distinto del pedido. NULL en filas
                # anteriores a la columna; se asumen convertidas con la
                # política global vigente (ver IngestionService._reusable_row).
                self._conn.execute("ALTER TABLE ingested_files ADD COLUMN describe_images INTEGER")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency "
                "ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_invocations_task_type "
                "ON model_invocations(task_type, provider, model, status)"
            )
            # Después de los ALTER: en una base anterior a los carriles la
            # columna `kind` no existe hasta la migración de arriba, y el
            # índice fallaría si se creara con el resto del esquema.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_lane ON tasks(kind, status, created_at)"
            )
            self._conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            if self._in_transaction:
                # El RLock es reentrante: sin este guard, un execute() dentro de
                # db.transaction() commitearía la transacción exterior a mitad.
                raise RuntimeError(
                    "Database.execute() dentro de una transacción abierta: "
                    "usa la conexión que expone db.transaction()"
                )
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    @contextmanager
    def transaction(self):
        with self._lock:
            if self._in_transaction:
                raise RuntimeError("Transacción anidada no soportada en Database.transaction()")
            self._in_transaction = True
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                self._in_transaction = False


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)
