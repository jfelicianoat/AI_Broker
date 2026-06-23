from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.artifacts import ArtifactStore
from app.db import Database, dumps_json
from app.providers import BootstrapModelProvider, ModelOutput, ProviderError
from app.repository import _utc_now_iso
from app.resource_scheduler import ResourceScheduler
from app.schemas import ExecutionStrategy, ModelReference, TaskCreateRequest, TaskStatus


class ConsensusCoordinator:
    algorithm_version = "fase-b-fast-consensus-v1"

    def __init__(
        self,
        db: Database,
        scheduler: ResourceScheduler,
        artifacts: ArtifactStore | None = None,
        provider: Any | None = None,
    ) -> None:
        self.db = db
        self.scheduler = scheduler
        self.artifacts = artifacts or ArtifactStore()
        self.provider = provider or BootstrapModelProvider()

    def initialize_run(self, task_id: str, request: TaskCreateRequest) -> str | None:
        if request.execution.strategy == ExecutionStrategy.single:
            return None

        run_id = f"run_{uuid4().hex}"
        now = _utc_now_iso()
        limits = {
            "max_proposers": request.execution.max_proposers,
            "max_judges": request.execution.max_judges,
            "max_rounds": request.execution.max_rounds,
            "timeout_seconds": request.execution.timeout_seconds,
        }
        self.db.execute(
            """
            INSERT INTO consensus_runs (
                id, task_id, strategy, preset, selection_mode, algorithm_version,
                limits_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task_id,
                request.execution.strategy.value,
                request.execution.preset.value,
                request.execution.selection.mode.value,
                self.algorithm_version,
                dumps_json(limits),
                now,
                now,
            ),
        )
        self._create_stage(task_id, run_id, 1, "resource_planning")
        self._create_stage(task_id, run_id, 2, "proposing")
        if request.execution.preset.value in {"standard", "verified", "high_stakes"}:
            self._create_stage(task_id, run_id, 3, "evaluating")
            self._create_stage(task_id, run_id, 4, "synthesizing")
        else:
            self._create_stage(task_id, run_id, 3, "synthesizing")
        return run_id

    async def process_next(self, repository) -> str | None:
        task_id = repository.claim_next_queued_task_id()
        if task_id is None:
            return None
        await self.process_task(repository, task_id)
        return task_id

    async def process_task(self, repository, task_id: str) -> None:
        try:
            request = repository.get_task_request(task_id)
            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            if request.execution.strategy == ExecutionStrategy.single:
                await self._process_single(repository, task_id, request)
                return
            if request.execution.preset.value != "fast":
                repository.update_task(
                    task_id,
                    TaskStatus.failed,
                    progress={"phase": TaskStatus.failed.value},
                    error={
                        "code": "CONSENSUS_PRESET_NOT_IMPLEMENTED",
                        "message": "Fase B only implements mixture_of_agents/fast",
                    },
                    clear_queue_position=True,
                )
                return
            await self._process_fast_consensus(repository, task_id, request)
        except ProviderError as error:
            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={"phase": TaskStatus.failed.value},
                error={
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                },
                clear_queue_position=True,
            )

    async def _process_single(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        model = (await self.provider.select(request, 1, ["single"]))[0]
        repository.update_task(
            task_id,
            TaskStatus.generating,
            progress={"phase": TaskStatus.generating.value, "invocations_completed": 0, "invocations_total": 1},
            clear_queue_position=True,
        )
        output = await self._run_cancellable(
            repository,
            task_id,
            self.provider.propose(request, model, 1),
        )
        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return
        invocation_id = repository.record_invocation(task_id, None, "single", model, output)
        artifact = self.artifacts.write_markdown(task_id, "single/final.md", output.content)
        repository.record_artifact(task_id, None, invocation_id, "single_output", artifact)
        repository.update_task(
            task_id,
            TaskStatus.completed,
            progress={"phase": TaskStatus.completed.value, "invocations_completed": 1, "invocations_total": 1},
            result={
                "result_markdown": output.content,
                "usage": self._usage([output]),
                "models_used": [model.model_dump(mode="json")],
            },
        )

    async def _process_fast_consensus(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        run_id = repository.get_consensus_run_id(task_id) or self.initialize_run(task_id, request)
        assert run_id is not None
        proposers = await self._select_proposers(request)
        arbiter = await self._select_arbiter(request)
        resource_plan = self.scheduler.plan(request)
        progress = {
            "phase": TaskStatus.resource_planning.value,
            "invocations_completed": 0,
            "invocations_total": len(proposers) + 1,
            "scheduling_mode": resource_plan.mode.value,
            "wave_current": 0,
            "wave_total": len(resource_plan.waves),
            "active_invocations": [],
            "cost_reserved_usd": request.model_requirements.max_cost_usd or 0.0,
            "cost_actual_usd": 0.0,
        }
        repository.update_task(task_id, TaskStatus.resource_planning, progress=progress, clear_queue_position=True)
        self._write_request_artifacts(repository, task_id, run_id, request, resource_plan.model_dump(mode="json"))

        proposals: list[tuple[ModelReference, ModelOutput]] = []
        completed = 0
        repository.update_task(task_id, TaskStatus.proposing, progress={**progress, "phase": TaskStatus.proposing.value})
        for wave_index, labels in enumerate(resource_plan.waves, start=1):
            active_models = proposers[completed : completed + len(labels)]
            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            wave_outputs: list[tuple[ModelReference, ModelOutput]] = []
            for ordinal, model in enumerate(active_models, start=1):
                invocation_request = self._with_remaining_budget(
                    request,
                    [output for _, output in proposals] + [output for _, output in wave_outputs],
                )
                try:
                    output = await self._run_cancellable(
                        repository,
                        task_id,
                        self.provider.propose(invocation_request, model, len(proposals) + ordinal),
                    )
                    wave_outputs.append((model, output))
                except ProviderError as error:
                    if not error.retryable:
                        raise

            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return

            for model, output in wave_outputs:
                ordinal = len(proposals) + 1
                role = model.role or "proposer"
                invocation_id = repository.record_invocation(task_id, run_id, role, model, output)
                artifact = self.artifacts.write_markdown(
                    task_id,
                    f"proposers/{ordinal:02d}_{self._safe_name(role)}_{self._safe_name(model.model)}.md",
                    self._model_markdown(task_id, role, model, output),
                )
                repository.record_artifact(task_id, run_id, invocation_id, "proposer_output", artifact)
                proposals.append((model, output))
                completed += 1
                self._enforce_budget(request, [item for _, item in proposals])
                repository.update_task(
                    task_id,
                    TaskStatus.proposing,
                    progress={
                        **progress,
                        "phase": TaskStatus.proposing.value,
                        "invocations_completed": completed,
                        "wave_current": wave_index,
                        "active_invocations": [item.model_dump(mode="json") for item in active_models],
                        "cost_actual_usd": sum(item.cost_usd for _, item in proposals),
                    },
                )

        quorum = min(2, len(proposers))
        if len(proposals) < quorum:
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={**progress, "phase": TaskStatus.failed.value},
                error={"code": "CONSENSUS_QUORUM_NOT_REACHED", "completed": len(proposals), "required": quorum},
            )
            return

        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return

        repository.update_task(
            task_id,
            TaskStatus.synthesizing,
            progress={
                **progress,
                "phase": TaskStatus.synthesizing.value,
                "invocations_completed": len(proposals),
                "wave_current": len(resource_plan.waves),
                "active_invocations": [arbiter.model_dump(mode="json")],
                "cost_actual_usd": sum(output.cost_usd for _, output in proposals),
            },
        )
        synthesis_request = self._with_remaining_budget(request, [output for _, output in proposals])
        synthesis = await self._run_cancellable(
            repository,
            task_id,
            self.provider.synthesize(synthesis_request, arbiter, [output for _, output in proposals]),
        )
        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return
        self._enforce_budget(request, [output for _, output in proposals] + [synthesis])
        synthesis_invocation_id = repository.record_invocation(task_id, run_id, "arbiter", arbiter, synthesis)
        synthesis_artifact = self.artifacts.write_markdown(
            task_id,
            f"synthesis/arbiter_{self._safe_name(arbiter.model)}.md",
            self._model_markdown(task_id, "arbiter", arbiter, synthesis),
        )
        repository.record_artifact(task_id, run_id, synthesis_invocation_id, "synthesis_output", synthesis_artifact)
        final_artifact = self.artifacts.write_markdown(task_id, "synthesis/final.md", synthesis.content)
        repository.record_artifact(task_id, run_id, None, "final_output", final_artifact)

        all_outputs = [output for _, output in proposals] + [synthesis]
        result = {
            "result_markdown": synthesis.content,
            "consensus": {
                "level": "fast",
                "confidence": None,
                "proposers_completed": len(proposals),
                "rounds": 1,
                "remaining_disagreements": [],
                "warnings": [],
            },
            "scheduling": {
                "mode_used": resource_plan.mode.value,
                "waves": len(resource_plan.waves),
                "peak_vram_reserved_gb": resource_plan.peak_vram_reserved_gb,
                "peak_vram_observed_gb": None,
            },
            "usage": self._usage(all_outputs),
            "models_used": [model.model_dump(mode="json") for model, _ in proposals]
            + [arbiter.model_dump(mode="json")],
            "artifacts_root": str(self.artifacts.root / task_id),
        }
        repository.update_task(
            task_id,
            TaskStatus.completed,
            progress={
                **progress,
                "phase": TaskStatus.completed.value,
                "invocations_completed": len(all_outputs),
                "active_invocations": [],
                "cost_actual_usd": result["usage"]["cost_usd"],
            },
            result=result,
        )

    def _write_request_artifacts(
        self,
        repository,
        task_id: str,
        run_id: str,
        request: TaskCreateRequest,
        resource_plan: dict,
    ) -> None:
        request_artifact = self.artifacts.write_markdown(
            task_id,
            "request.md",
            f"# Request\n\n{request.content.prompt}\n",
        )
        repository.record_artifact(task_id, run_id, None, "request", request_artifact)
        manifest = {
            "task_id": task_id,
            "run_id": run_id,
            "algorithm_version": self.algorithm_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resource_plan": resource_plan,
        }
        manifest_artifact = self.artifacts.write_text(task_id, "manifest.json", dumps_json(manifest))
        repository.record_artifact(task_id, run_id, None, "manifest", manifest_artifact)

    async def _select_proposers(self, request: TaskCreateRequest) -> list[ModelReference]:
        selection = request.execution.selection
        if selection.mode.value == "manual":
            return selection.proposers[: request.execution.max_proposers]

        selected = list(selection.required_proposers) if selection.mode.value == "hybrid" else []
        target_count = request.execution.max_proposers
        roles = ["generalist", "specialist", "skeptic", "analyst", "reviewer"]
        missing = target_count - len(selected)
        if missing > 0:
            selected.extend(await self.provider.select(request, missing, roles[len(selected):target_count]))
        return selected[:target_count]

    async def _select_arbiter(self, request: TaskCreateRequest) -> ModelReference:
        selection = request.execution.selection
        if selection.arbiter is not None:
            return selection.arbiter
        if selection.preferred_arbiter is not None:
            return selection.preferred_arbiter
        return (await self.provider.select(request, 1, ["arbiter"]))[0]

    def _enforce_budget(self, request: TaskCreateRequest, outputs: list[ModelOutput]) -> None:
        maximum = request.model_requirements.max_cost_usd
        actual = sum(output.cost_usd for output in outputs)
        if maximum is not None and actual > maximum:
            raise ProviderError(
                "BUDGET_EXCEEDED",
                f"El coste acumulado ({actual:.6f} USD) supera el presupuesto ({maximum:.6f} USD)",
            )

    def _with_remaining_budget(
        self,
        request: TaskCreateRequest,
        outputs: list[ModelOutput],
    ) -> TaskCreateRequest:
        maximum = request.model_requirements.max_cost_usd
        if maximum is None:
            return request
        remaining = maximum - sum(output.cost_usd for output in outputs)
        if remaining <= 0:
            raise ProviderError("BUDGET_EXCEEDED", "No queda presupuesto para otra invocación")
        requirements = request.model_requirements.model_copy(update={"max_cost_usd": remaining})
        return request.model_copy(update={"model_requirements": requirements})

    async def _run_cancellable(self, repository, task_id: str, operation) -> ModelOutput:
        task = asyncio.create_task(operation)
        while not task.done():
            await asyncio.wait({task}, timeout=0.1)
            if repository.is_cancel_requested(task_id):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise ProviderError("TASK_CANCELLED", "La inferencia fue cancelada")
        return await task

    def _usage(self, outputs: list[ModelOutput]) -> dict:
        return {
            "invocations": len(outputs),
            "tokens_input": sum(output.tokens_input for output in outputs),
            "tokens_output": sum(output.tokens_output for output in outputs),
            "cost_usd": round(sum(output.cost_usd for output in outputs), 8),
        }

    def _model_markdown(
        self,
        task_id: str,
        role: str,
        model: ModelReference,
        output: ModelOutput,
    ) -> str:
        return (
            f"# {role.title()}\n\n"
            f"- Task ID: {task_id}\n"
            f"- Role: {role}\n"
            f"- Provider: {model.provider}\n"
            f"- Deployment: {model.deployment}\n"
            f"- Model: {model.model}\n"
            f"- Input tokens: {output.tokens_input}\n"
            f"- Output tokens: {output.tokens_output}\n"
            f"- Cost USD: {output.cost_usd:.8f}\n\n"
            "## Respuesta\n\n"
            f"{output.content}\n"
        )

    def _safe_name(self, value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value).strip("-") or "item"

    def _create_stage(self, task_id: str, run_id: str, ordinal: int, stage_type: str) -> None:
        now = _utc_now_iso()
        stage_id = f"stage_{uuid4().hex}"
        self.db.execute(
            """
            INSERT INTO stages (
                id, task_id, run_id, ordinal, stage_type, status,
                idempotency_key, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage_id,
                task_id,
                run_id,
                ordinal,
                stage_type,
                "queued",
                f"{task_id}:{run_id}:{stage_type}:{ordinal}",
                now,
                now,
            ),
        )
