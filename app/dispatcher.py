"""Bucles de despacho, uno por carril de trabajo (app.schemas.TaskKind).

La inferencia se procesa de una en una: es el invariante del broker. La ingesta
tiene su propio bucle y su propio límite de concurrencia, porque convertir un
fichero no debe consumir el turno de las respuestas — si compartieran carril,
subir un PDF congelaría la cola de inferencia. Los embeddings (contrato 2.12)
tienen el suyo por la misma razón: un vector de décimas de segundo no puede
esperar minutos detrás de una generación.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.coordinator import ConsensusCoordinator
from app.repository import TaskRepository
from app.schemas import TaskStatus


async def dispatcher_loop(
    repository: TaskRepository,
    coordinator: ConsensusCoordinator,
    stop: asyncio.Event,
    interval_seconds: float,
) -> None:
    logger = logging.getLogger("ai_broker.dispatcher")
    while not stop.is_set():
        # Ningún fallo de una iteración puede matar el bucle: sin dispatcher
        # las tareas se acumularían en cola con el servidor aparentemente sano.
        try:
            task_id = await asyncio.to_thread(repository.claim_next_queued_task_id)
            if task_id is not None:
                try:
                    await coordinator.process_task(repository, task_id)
                except Exception as error:
                    current = repository.get_task(task_id)
                    if current.status in {TaskStatus.completed, TaskStatus.cancelled}:
                        continue
                    repository.update_task(
                        task_id,
                        TaskStatus.failed,
                        progress={"phase": TaskStatus.failed.value},
                        error={
                            "code": "INTERNAL_ERROR",
                            "message": f"Dispatcher failure: {type(error).__name__}",
                            "retryable": False,
                        },
                        clear_queue_position=True,
                    )
                continue
        except Exception:
            logger.exception("dispatcher.iteration_failed", extra={"event": "dispatcher.iteration_failed"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(interval_seconds, 1.0))
            except TimeoutError:
                pass
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def ingestion_dispatcher_loop(
    repository: TaskRepository,
    ingestion: Any,
    stop: asyncio.Event,
    interval_seconds: float,
    max_concurrent: int,
) -> None:
    """Carril de ingesta: hasta `max_concurrent` conversiones a la vez.

    Antes de los carriles cada subida arrancaba su propia tarea asyncio sin
    límite alguno: cinco PDFs eran cinco conversiones compitiendo con la
    inferencia por CPU y memoria, y ninguna aparecía en la cola."""
    logger = logging.getLogger("ai_broker.dispatcher.ingestion")
    # Indexadas por task_id para poder cancelar UNA conversión concreta cuando
    # el usuario la cancela desde el panel.
    running: dict[str, asyncio.Task] = {}
    while not stop.is_set():
        try:
            await _cancel_requested_conversions(repository, running, logger)
            while len(running) < max_concurrent:
                claimed = await asyncio.to_thread(
                    repository.claim_next_ingestion_task, max_concurrent,
                )
                if claimed is None:
                    break
                task_id, file_id = claimed
                job = asyncio.create_task(ingestion.run_task(repository, task_id, file_id))
                running[task_id] = job
                job.add_done_callback(_forget(running, task_id))
        except Exception:
            logger.exception(
                "dispatcher.ingestion_iteration_failed",
                extra={"event": "dispatcher.ingestion_iteration_failed"},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
    # Al parar, las conversiones en vuelo se cancelan: el subproceso muere con
    # ellas y la tarea queda en 'converting', que la recuperación del próximo
    # arranque devuelve a la cola.
    jobs = list(running.values())
    for job in jobs:
        job.cancel()
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)


async def embedding_dispatcher_loop(
    repository: TaskRepository,
    coordinator: ConsensusCoordinator,
    stop: asyncio.Event,
    interval_seconds: float,
    max_parallel: int,
) -> None:
    """Carril de embeddings: hasta `max_parallel` tareas a la vez, sin tocar
    el workflow único de inferencia.

    Antes un embedding de 200 ms esperaba en la cola a que terminase la
    generación de chat en curso, y la tarea tardaba 35–54 s. Cada tarea pasa
    por el mismo `process_task` que la inferencia —enrutado, memoria,
    cancelación, telemetría—; lo único que cambia es quién le da el turno.
    La cancelación la atiende el propio coordinador, como en inferencia."""
    logger = logging.getLogger("ai_broker.dispatcher.embedding")
    running: dict[str, asyncio.Task] = {}
    while not stop.is_set():
        try:
            while len(running) < max_parallel:
                task_id = await asyncio.to_thread(
                    repository.claim_next_embedding_task, max_parallel,
                )
                if task_id is None:
                    break
                job = asyncio.create_task(_process_claimed(repository, coordinator, task_id))
                running[task_id] = job
                job.add_done_callback(_forget(running, task_id))
        except Exception:
            logger.exception(
                "dispatcher.embedding_iteration_failed",
                extra={"event": "dispatcher.embedding_iteration_failed"},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
    # Igual que la ingesta: lo que estaba en vuelo queda en un estado activo y
    # la recuperación del próximo arranque lo devuelve a la cola.
    jobs = list(running.values())
    for job in jobs:
        job.cancel()
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)


async def _process_claimed(
    repository: TaskRepository, coordinator: ConsensusCoordinator, task_id: str,
) -> None:
    """Mismo trato de errores que el bucle de inferencia: un fallo inesperado
    deja la tarea en `failed` en vez de colgada en un estado activo."""
    try:
        await coordinator.process_task(repository, task_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        current = repository.get_task(task_id)
        if current.status in {TaskStatus.completed, TaskStatus.cancelled}:
            return
        repository.update_task(
            task_id,
            TaskStatus.failed,
            progress={"phase": TaskStatus.failed.value},
            error={
                "code": "INTERNAL_ERROR",
                "message": f"Dispatcher failure: {type(error).__name__}",
                "retryable": False,
            },
            clear_queue_position=True,
        )


def _forget(
    running: dict[str, asyncio.Task], task_id: str,
) -> Callable[[asyncio.Task], None]:
    """Callback que saca la conversión del registro al terminar."""
    def _done(_: asyncio.Task) -> None:
        running.pop(task_id, None)

    return _done


async def _cancel_requested_conversions(
    repository: TaskRepository, running: dict[str, asyncio.Task], logger: logging.Logger,
) -> None:
    """Aborta de verdad las conversiones que el usuario ha cancelado.

    Cancelar la tarea asyncio propaga la cancelación hasta el `communicate` del
    proceso hijo, que lo mata. Es lo que no se podía hacer cuando la conversión
    corría en un hilo: marcar la fila como cancelada no detenía el trabajo, y
    Docling seguía comiendo máquina hasta terminar."""
    for task_id, job in list(running.items()):
        if job.done():
            continue
        if await asyncio.to_thread(repository.is_cancel_requested, task_id):
            job.cancel()
            logger.info(
                "dispatcher.ingestion_cancelled",
                extra={"event": "dispatcher.ingestion_cancelled", "task_id": task_id},
            )
