"""Bucles de carril (embeddings, cloud) e ingesta con dobles en memoria.

Comprueban lo que el resto de la suite no alcanza sin un broker completo:
que un fallo inesperado del coordinador deja la tarea en `failed` (y no la
pisa si ya terminó), que una conversión cancelada desde el panel se aborta de
verdad y que al parar se cancela todo lo que seguía en vuelo.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from app import dispatcher
from app.schemas import TaskKind, TaskStatus


class FakeRepository:
    def __init__(self, lane_ids: list[str] | None = None, ingestion: list[tuple[str, str]] | None = None) -> None:
        self.lane_ids = list(lane_ids or [])
        self.ingestion = list(ingestion or [])
        self.statuses: dict[str, TaskStatus] = {}
        self.updates: list[tuple[str, TaskStatus, dict[str, Any]]] = []
        self.cancel_requested: set[str] = set()
        self.claim_errors = 0

    def claim_next_lane_task(self, kind: TaskKind, max_parallel: int) -> str | None:
        if self.claim_errors:
            self.claim_errors -= 1
            raise RuntimeError("sqlite ocupado")
        return self.lane_ids.pop(0) if self.lane_ids else None

    def claim_next_ingestion_task(self, max_concurrent: int) -> tuple[str, str] | None:
        if self.claim_errors:
            self.claim_errors -= 1
            raise RuntimeError("sqlite ocupado")
        return self.ingestion.pop(0) if self.ingestion else None

    def get_task(self, task_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=self.statuses.get(task_id, TaskStatus.generating))

    def update_task(self, task_id: str, status: TaskStatus, **kwargs: Any) -> None:
        self.updates.append((task_id, status, kwargs))

    def is_cancel_requested(self, task_id: str) -> bool:
        return task_id in self.cancel_requested


class FakeCoordinator:
    def __init__(self, behaviours: dict[str, str]) -> None:
        self.behaviours = behaviours
        self.started: list[str] = []
        self.cancelled: list[str] = []

    async def process_task(self, repository: FakeRepository, task_id: str) -> None:
        self.started.append(task_id)
        behaviour = self.behaviours.get(task_id, "ok")
        if behaviour == "boom":
            raise ValueError("fallo inesperado")
        if behaviour == "hang":
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.append(task_id)
                raise


async def _run_until(loop_coro, stop: asyncio.Event, condition, timeout: float = 5.0) -> None:
    runner = asyncio.create_task(loop_coro)
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "el bucle no llegó al estado esperado"
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, timeout)


def test_lane_loop_marks_unexpected_failures_and_cancels_on_stop() -> None:
    repository = FakeRepository(lane_ids=["ok", "boom", "done-already", "hang"])
    repository.statuses["done-already"] = TaskStatus.completed
    repository.claim_errors = 1  # la primera iteración falla y el bucle sobrevive
    coordinator = FakeCoordinator({"boom": "boom", "done-already": "boom", "hang": "hang"})

    async def scenario() -> None:
        stop = asyncio.Event()
        await _run_until(
            dispatcher.cloud_dispatcher_loop(repository, coordinator, stop, 0.01, 4),
            stop,
            lambda: len(coordinator.started) == 4 and len(repository.updates) == 1,
        )

    asyncio.run(scenario())
    # Solo la tarea que seguía activa pasa a failed; la ya completada no se pisa.
    assert [(task_id, status) for task_id, status, _ in repository.updates] == [("boom", TaskStatus.failed)]
    error = repository.updates[0][2]["error"]
    assert error["code"] == "INTERNAL_ERROR" and "ValueError" in error["message"]
    assert coordinator.cancelled == ["hang"]


def test_embedding_lane_uses_the_same_loop() -> None:
    repository = FakeRepository(lane_ids=["e1"])
    coordinator = FakeCoordinator({})

    async def scenario() -> None:
        stop = asyncio.Event()
        await _run_until(
            dispatcher.embedding_dispatcher_loop(repository, coordinator, stop, 0.01, 2),
            stop,
            lambda: coordinator.started == ["e1"],
        )

    asyncio.run(scenario())


def test_ingestion_loop_cancels_requested_and_inflight_conversions() -> None:
    repository = FakeRepository(ingestion=[("t-cancel", "f1"), ("t-stop", "f2")])
    repository.claim_errors = 1
    cancelled: list[str] = []
    started: list[str] = []

    class FakeIngestion:
        async def run_task(self, repo: FakeRepository, task_id: str, file_id: str) -> None:
            started.append(task_id)
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(task_id)
                raise

    async def scenario() -> None:
        stop = asyncio.Event()

        def ready() -> bool:
            if len(started) == 2:
                repository.cancel_requested.add("t-cancel")
            return "t-cancel" in cancelled

        await _run_until(
            dispatcher.ingestion_dispatcher_loop(repository, FakeIngestion(), stop, 0.01, 2),
            stop,
            ready,
        )

    asyncio.run(scenario())
    # La cancelada se aborta durante el bucle; la otra, al parar.
    assert cancelled == ["t-cancel", "t-stop"]
