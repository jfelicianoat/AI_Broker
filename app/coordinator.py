from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.artifacts import ArtifactStore
from app.db import Database, dumps_json
from app.providers import BootstrapModelProvider, ModelOutput, ProviderError
from app.repository import _utc_now_iso
from app.resource_scheduler import ResourcePlanningError, ResourceScheduler
from app.schemas import ExecutionPreset, ExecutionStrategy, InferenceKind, ModelReference, TaskCreateRequest, TaskStatus

logger = logging.getLogger("ai_broker.coordinator")


class ConsensusCoordinator:
    algorithm_version = "fase-5-fast-slow-consensus-v2"

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
            "max_cost_usd": request.model_requirements.max_cost_usd,
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
        request = repository.get_task_request(task_id)
        effective_timeout = min(
            request.execution.timeout_seconds,
            self.scheduler.config.processing.task_timeout_seconds,
        )
        try:
            await asyncio.wait_for(
                self._process_task_request(repository, task_id, request),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={"phase": TaskStatus.failed.value, "timeout_seconds": effective_timeout},
                error={
                    "code": "TASK_TIMEOUT",
                    "message": f"La tarea superó el timeout efectivo de {effective_timeout} segundos",
                    "retryable": True,
                },
                clear_queue_position=True,
            )
            logger.warning(
                "task.failed",
                extra={
                    "event": "task.failed",
                    "task_id": task_id,
                    "code": "TASK_TIMEOUT",
                    "retryable": True,
                },
            )
        except ProviderError as error:
            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            current = repository.get_task(task_id)
            context = self._error_context(error, current.progress)
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={"phase": TaskStatus.failed.value},
                error={
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    **({"details": error.details} if getattr(error, "details", None) else {}),
                    **context,
                },
                clear_queue_position=True,
            )
            logger.warning(
                "task.failed",
                extra={
                    "event": "task.failed",
                    "task_id": task_id,
                    "code": error.code,
                    "retryable": error.retryable,
                    **context,
                },
            )

    async def _process_task_request(
        self,
        repository,
        task_id: str,
        request: TaskCreateRequest,
    ) -> None:
        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return
        if request.execution.strategy == ExecutionStrategy.single:
            await self._process_single(repository, task_id, request)
            return
        if request.execution.preset not in {ExecutionPreset.fast, ExecutionPreset.slow}:
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={"phase": TaskStatus.failed.value},
                error={
                    "code": "CONSENSUS_PRESET_NOT_IMPLEMENTED",
                    "message": "Only mixture_of_agents/fast and mixture_of_agents/slow are implemented",
                },
                clear_queue_position=True,
            )
            return
        await self._process_consensus(repository, task_id, request)

    async def _process_single(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        model = (await self.provider.select(request, 1, ["single"]))[0]
        repository.update_task(
            task_id,
            TaskStatus.generating,
            progress={
                "phase": TaskStatus.generating.value,
                "invocations_completed": 0,
                "invocations_total": 1,
                "budget_limit_usd": request.model_requirements.max_cost_usd,
                "cost_estimated_usd": None,
                "cost_actual_usd": 0.0,
            },
            clear_queue_position=True,
        )
        # Un blip transitorio del provider (429/5xx) no debe tirar la tarea:
        # el campo retryable del error deja de ser una promesa vacía al cliente.
        retry_delays = (0.5, 1.0)
        output: ModelOutput | None = None
        for attempt_index in range(len(retry_delays) + 1):
            try:
                output = await self._run_cancellable(
                    repository,
                    task_id,
                    self.provider.propose(request, model, 1),
                )
                break
            except ProviderError as error:
                self._attach_error_context(error, "generating", model)
                if (
                    not error.retryable
                    or error.code == "TASK_CANCELLED"
                    or attempt_index >= len(retry_delays)
                    or repository.is_cancel_requested(task_id)
                ):
                    raise
                logger.warning(
                    "task.single_retry",
                    extra={
                        "event": "task.single_retry",
                        "task_id": task_id,
                        "code": error.code,
                        "attempt": attempt_index + 1,
                    },
                )
                await asyncio.sleep(retry_delays[attempt_index])
        assert output is not None
        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return
        progress = {
            "phase": TaskStatus.completed.value,
            "invocations_completed": 1,
            "invocations_total": 1,
            "budget_limit_usd": request.model_requirements.max_cost_usd,
            "cost_estimated_usd": None,
            "cost_actual_usd": output.cost_usd,
        }
        result = self._technical_result(request, model, output)
        invocation_id = repository.complete_single_task(
            task_id, model, output, progress=progress, result=result,
        )
        try:
            if output.embedding is not None:
                artifact = self.artifacts.write_text(task_id, "single/embedding.json", dumps_json({
                    "embedding": list(output.embedding),
                }))
                artifact_type = "embedding_output"
            else:
                suffix = "json" if request.output.format.value == "json" else "md"
                artifact = self.artifacts.write_text(task_id, f"single/final.{suffix}", output.content or "")
                artifact_type = "single_output"
            repository.record_artifact(task_id, None, invocation_id, artifact_type, artifact)
        except Exception as error:
            try:
                repository.add_event(task_id, "artifact.failed", {"message": str(error)})
            except Exception:
                pass

    async def _process_consensus(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        run_id = repository.get_consensus_run_id(task_id) or self.initialize_run(task_id, request)
        assert run_id is not None
        proposers = await self._select_proposers(request)
        arbiter = await self._select_arbiter(request)
        plan_execution = request.execution.model_copy(update={"max_proposers": len(proposers)})
        plan_request = request.model_copy(update={"execution": plan_execution})
        try:
            resource_plan = self.scheduler.plan(plan_request)
        except ResourcePlanningError as error:
            raise ProviderError("PARALLEL_CAPACITY_INSUFFICIENT", str(error)) from error
        progress = {
            "phase": TaskStatus.resource_planning.value,
            "invocations_completed": 0,
            "invocations_total": len(proposers) + 1,
            "scheduling_requested": request.execution.scheduling.value,
            "scheduling_mode": resource_plan.mode.value,
            "wave_current": 0,
            "wave_total": len(resource_plan.waves),
            "active_invocations": [],
            "max_parallel_invocations_launched": 0,
            "budget_limit_usd": request.model_requirements.max_cost_usd,
            "cost_estimated_usd": None,
            "cost_actual_usd": 0.0,
        }
        repository.update_task(task_id, TaskStatus.resource_planning, progress=progress, clear_queue_position=True)
        self._write_request_artifacts(repository, task_id, run_id, request, resource_plan.model_dump(mode="json"))

        proposals: list[tuple[ModelReference, ModelOutput]] = []
        skipped_proposers: list[dict[str, Any]] = []
        completed = 0
        attempted = 0
        max_parallel_launched = 0
        repository.update_task(task_id, TaskStatus.proposing, progress={**progress, "phase": TaskStatus.proposing.value})
        for wave_index, labels in enumerate(resource_plan.waves, start=1):
            active_models = proposers[attempted : attempted + len(labels)]
            attempted += len(active_models)
            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            max_parallel_launched = max(
                max_parallel_launched,
                len(active_models) if request.execution.preset == ExecutionPreset.slow else min(1, len(active_models)),
            )
            launch_models = active_models if request.execution.preset == ExecutionPreset.slow else active_models[:1]
            repository.update_task(
                task_id,
                TaskStatus.proposing,
                progress={
                    **progress,
                    "phase": TaskStatus.proposing.value,
                    "invocations_completed": completed,
                    "wave_current": wave_index,
                    "active_invocations": [item.model_dump(mode="json") for item in launch_models],
                    "skipped_proposers": skipped_proposers,
                    "max_parallel_invocations_launched": max_parallel_launched,
                    "cost_actual_usd": sum(item.cost_usd for _, item in proposals),
                },
            )
            wave_outputs, wave_skipped = await self._run_proposer_wave(
                repository,
                task_id,
                request,
                active_models,
                proposals,
            )
            skipped_proposers.extend(wave_skipped)
            if wave_skipped and not wave_outputs:
                repository.update_task(
                    task_id,
                    TaskStatus.proposing,
                    progress={
                        **progress,
                        "phase": TaskStatus.proposing.value,
                        "invocations_completed": completed,
                        "wave_current": wave_index,
                        "active_invocations": [],
                        "skipped_proposers": skipped_proposers,
                        "max_parallel_invocations_launched": max_parallel_launched,
                        "cost_actual_usd": sum(item.cost_usd for _, item in proposals),
                    },
                )

            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return

            for model, output in wave_outputs:
                ordinal = len(proposals) + 1
                role = model.role or "proposer"
                invocation_id = repository.record_invocation(task_id, run_id, role, model, output)
                self._record_artifact_safely(
                    repository, task_id, run_id, invocation_id, "proposer_output",
                    lambda o=ordinal, r=role, m=model, out=output: self.artifacts.write_markdown(
                        task_id,
                        f"proposers/{o:02d}_{self._safe_name(r)}_{self._safe_name(m.model)}.md",
                        self._model_markdown(task_id, r, m, out),
                    ),
                )
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
                        "active_invocations": [],
                        "skipped_proposers": skipped_proposers,
                        "max_parallel_invocations_launched": max_parallel_launched,
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
                "skipped_proposers": skipped_proposers,
                "max_parallel_invocations_launched": max_parallel_launched,
                "cost_actual_usd": sum(output.cost_usd for _, output in proposals),
            },
        )
        synthesis_request = self._with_remaining_budget(request, [output for _, output in proposals])
        try:
            synthesis = await self._run_cancellable(
                repository,
                task_id,
                self.provider.synthesize(synthesis_request, arbiter, [output for _, output in proposals]),
            )
        except ProviderError as error:
            self._attach_error_context(error, "synthesizing", arbiter, role="arbiter")
            raise
        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return
        self._enforce_budget(request, [output for _, output in proposals] + [synthesis])
        synthesis_invocation_id = repository.record_invocation(task_id, run_id, "arbiter", arbiter, synthesis)
        self._record_artifact_safely(
            repository, task_id, run_id, synthesis_invocation_id, "synthesis_output",
            lambda: self.artifacts.write_markdown(
                task_id,
                f"synthesis/arbiter_{self._safe_name(arbiter.model)}.md",
                self._model_markdown(task_id, "arbiter", arbiter, synthesis),
            ),
        )
        self._record_artifact_safely(
            repository, task_id, run_id, None, "final_output",
            lambda: self.artifacts.write_markdown(task_id, "synthesis/final.md", synthesis.content or ""),
        )

        all_outputs = [output for _, output in proposals] + [synthesis]
        result: dict[str, Any] = {
            "result_markdown": synthesis.content,
            "assistant_content": synthesis.content,
            "inference_kind": "chat",
            "output_format": request.output.format.value,
            "consensus": {
                "level": request.execution.preset.value,
                "confidence": None,
                "proposers_completed": len(proposals),
                "proposers_failed": len(skipped_proposers),
                "rounds": 1,
                "remaining_disagreements": [],
                "warnings": [self._skipped_proposer_warning(item) for item in skipped_proposers],
            },
            "skipped_proposers": skipped_proposers,
            "scheduling": {
                "requested": request.execution.scheduling.value,
                "mode_used": resource_plan.mode.value,
                "waves": len(resource_plan.waves),
                "max_parallel_invocations_launched": max_parallel_launched,
                "peak_vram_reserved_gb": resource_plan.peak_vram_reserved_gb,
                "peak_vram_observed_gb": None,
            },
            "usage": self._usage(all_outputs),
            "models_used": [model.model_dump(mode="json") for model, _ in proposals]
            + [arbiter.model_dump(mode="json")],
            "model_used": arbiter.model_dump(mode="json"),
            "fallback_used": self._fallback_used(request, arbiter),
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
                "skipped_proposers": skipped_proposers,
                "max_parallel_invocations_launched": max_parallel_launched,
                "cost_actual_usd": result["usage"]["cost_usd"],
            },
            result=result,
        )

    async def _run_proposer_wave(
        self,
        repository,
        task_id: str,
        request: TaskCreateRequest,
        active_models: list[ModelReference],
        completed_proposals: list[tuple[ModelReference, ModelOutput]],
    ) -> tuple[list[tuple[ModelReference, ModelOutput]], list[dict[str, Any]]]:
        completed_outputs = [output for _, output in completed_proposals]
        invocation_request = self._with_wave_budget(request, completed_outputs, len(active_models))

        async def invoke(ordinal: int, model: ModelReference):
            try:
                output = await self._run_cancellable(
                    repository,
                    task_id,
                    self.provider.propose(invocation_request, model, len(completed_proposals) + ordinal),
                )
                return model, output, None
            except ProviderError as error:
                self._attach_error_context(error, "proposing", model)
                return model, None, error

        if request.execution.preset == ExecutionPreset.slow and len(active_models) > 1:
            results = await asyncio.gather(
                *(invoke(ordinal, model) for ordinal, model in enumerate(active_models, start=1))
            )
        else:
            results = []
            for ordinal, model in enumerate(active_models, start=1):
                results.append(await invoke(ordinal, model))

        outputs: list[tuple[ModelReference, ModelOutput]] = []
        skipped: list[dict[str, Any]] = []
        for model, output, error in results:
            if error is not None:
                if not error.retryable:
                    raise error
                skipped_item = self._skipped_proposer_detail(model, error)
                repository.add_event(task_id, "proposer.skipped", skipped_item)
                skipped.append(skipped_item)
                continue
            assert output is not None
            outputs.append((model, output))
        return outputs, skipped

    def _skipped_proposer_detail(self, model: ModelReference, error: ProviderError) -> dict[str, Any]:
        return {
            "stage": error.stage or "proposing",
            "role": error.role or model.role or "proposer",
            "provider": error.provider or model.provider,
            "deployment": error.deployment or model.deployment,
            "model": error.model or model.model,
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        }

    def _skipped_proposer_warning(self, item: dict[str, Any]) -> str:
        model_name = f"{item.get('provider')}/{item.get('deployment')}/{item.get('model')}"
        return f"{item.get('role', 'proposer')} omitido ({model_name}): {item.get('code')}"

    def _with_wave_budget(
        self,
        request: TaskCreateRequest,
        completed_outputs: list[ModelOutput],
        wave_size: int,
    ) -> TaskCreateRequest:
        invocation_request = self._with_remaining_budget(request, completed_outputs)
        maximum = invocation_request.model_requirements.max_cost_usd
        if maximum is None or wave_size <= 1:
            return invocation_request
        requirements = invocation_request.model_requirements.model_copy(
            update={"max_cost_usd": maximum / wave_size}
        )
        return invocation_request.model_copy(update={"model_requirements": requirements})

    def _record_artifact_safely(
        self,
        repository,
        task_id: str,
        run_id: str | None,
        invocation_id: str | None,
        artifact_type: str,
        write,
    ) -> None:
        """Un fallo de disco al persistir un artefacto no debe tirar una tarea ya pagada."""
        try:
            artifact = write()
            repository.record_artifact(task_id, run_id, invocation_id, artifact_type, artifact)
        except Exception as error:
            logger.warning(
                "artifact.failed",
                extra={
                    "event": "artifact.failed",
                    "task_id": task_id,
                    "artifact_type": artifact_type,
                    "detail": str(error),
                },
            )
            try:
                repository.add_event(
                    task_id, "artifact.failed", {"artifact_type": artifact_type, "message": str(error)}
                )
            except Exception:
                pass

    def _write_request_artifacts(
        self,
        repository,
        task_id: str,
        run_id: str,
        request: TaskCreateRequest,
        resource_plan: dict,
    ) -> None:
        self._record_artifact_safely(
            repository, task_id, run_id, None, "request",
            lambda: self.artifacts.write_markdown(
                task_id,
                "request.md",
                f"# Request\n\n{request.content.prompt}\n",
            ),
        )
        manifest = {
            "task_id": task_id,
            "run_id": run_id,
            "algorithm_version": self.algorithm_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resource_plan": resource_plan,
        }
        self._record_artifact_safely(
            repository, task_id, run_id, None, "manifest",
            lambda: self.artifacts.write_text(task_id, "manifest.json", dumps_json(manifest)),
        )

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
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.1)
                if task.done():
                    break
                if await asyncio.to_thread(repository.is_cancel_requested, task_id):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise ProviderError("TASK_CANCELLED", "La inferencia fue cancelada")
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _usage(self, outputs: list[ModelOutput]) -> dict:
        return {
            "invocations": len(outputs),
            "tokens_input": sum(output.tokens_input for output in outputs),
            "tokens_output": sum(output.tokens_output for output in outputs),
            "cost_usd": round(sum(output.cost_usd for output in outputs), 8),
        }

    def _technical_result(
        self,
        request: TaskCreateRequest,
        model: ModelReference,
        output: ModelOutput,
    ) -> dict:
        result: dict[str, Any] = {
            "inference_kind": request.inference_kind.value,
            "output_format": request.output.format.value,
            "usage": self._usage([output]),
            "model_used": model.model_dump(mode="json"),
            "models_used": [model.model_dump(mode="json")],
            "fallback_used": self._fallback_used(request, model),
        }
        if request.inference_kind == InferenceKind.embedding:
            result["embedding"] = list(output.embedding or ())
        else:
            result["assistant_content"] = output.content
            result["result_markdown"] = output.content
        return result

    @staticmethod
    def _fallback_used(request: TaskCreateRequest, model: ModelReference) -> bool:
        target = request.model_requirements.target_model
        if target is not None:
            return (
                target.provider.lower(),
                target.deployment.lower(),
                target.model,
            ) != (
                model.provider.lower(),
                model.deployment.lower(),
                model.model,
            )
        preferred = request.model_requirements.preferred_model
        return preferred is not None and preferred != model.model

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
            f"{output.content or ''}\n"
        )

    def _safe_name(self, value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value).strip("-") or "item"

    def _attach_error_context(
        self,
        error: ProviderError,
        stage: str,
        model: ModelReference,
        role: str | None = None,
    ) -> None:
        error.stage = stage
        error.role = role or model.role
        error.provider = model.provider
        error.deployment = model.deployment
        error.model = model.model

    def _error_context(self, error: ProviderError, progress: dict[str, Any]) -> dict[str, Any]:
        context = {
            key: getattr(error, key)
            for key in ("stage", "role", "provider", "deployment", "model")
            if getattr(error, key, None) is not None
        }
        if context:
            return context
        active = progress.get("active_invocations") if isinstance(progress, dict) else None
        if isinstance(active, list) and len(active) == 1 and isinstance(active[0], dict):
            model = active[0]
            return {
                "stage": progress.get("phase"),
                "role": model.get("role"),
                "provider": model.get("provider"),
                "deployment": model.get("deployment"),
                "model": model.get("model"),
            }
        return {"stage": progress.get("phase")} if isinstance(progress, dict) and progress.get("phase") else {}

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
