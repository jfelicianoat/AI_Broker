"""Bucle de despacho: reclama tareas encoladas y las procesa una a una."""
from __future__ import annotations

import asyncio
import logging

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
