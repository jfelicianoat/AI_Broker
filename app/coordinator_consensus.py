"""Estrategia mixture_of_agents: propuestas, síntesis, árbitro de repuesto y segunda ronda.

Mixin de ConsensusCoordinator (app/coordinator.py), separado al dividir el
coordinador (AB-08). Los métodos son los de antes, sin cambios. El bloque
TYPE_CHECKING declara lo que usan de la clase anfitriona para que mypy
compruebe que esas firmas siguen coincidiendo con las de verdad.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.artifacts import ArtifactStore
from app.coordinator_support import (
    _ARBITER_FATAL_ERRORS,
    _ARBITER_SUBSTITUTE_POOL,
    _TASK_FATAL_ERRORS,
    AgentLoopResult,
    _ConsensusCheckpoint,
    _output_from_checkpoint,
    _output_to_checkpoint,
    _RoundOutcome,
    _SecondRound,
    _Synthesis,
)
from app.providers import ModelOutput, ProviderError
from app.providers.base import (
    ROLE_SYSTEM_PROMPTS,
    context_fits_with_capped_output,
    multimodal_content,
    neutralize_consensus_delimiters,
    role_system_prompt,
    with_output_language,
)
from app.resource_scheduler import ModelFootprint, ResourcePlan, ResourcePlanningError, ResourceScheduler
from app.schemas import ExecutionPreset, ExecutionStrategy, ModelReference, TaskCreateRequest, TaskStatus
from app.skills import skill_definitions
from app.task_classifier import classify_task_type

# Mismo logger que el coordinador: los eventos conservan su origen.
logger = logging.getLogger("ai_broker.coordinator")


class ConsensusMixin:
    if TYPE_CHECKING:
        artifacts: ArtifactStore
        ingestion: Any
        provider: Any
        scheduler: ResourceScheduler
        _AGENT_SYSTEM_PROMPT: str

        def _attach_error_context(
            self, error: ProviderError, stage: str, model: ModelReference, role: str | None = None
        ) -> None: ...
        def _compression_echo(self, request: TaskCreateRequest) -> dict[str, str] | None: ...
        def _enforce_budget(self, request: TaskCreateRequest, outputs: list[ModelOutput]) -> None: ...
        @staticmethod
        def _fallback_used(request: TaskCreateRequest, model: ModelReference) -> bool: ...
        async def _invocation_fingerprint(self, model: ModelReference) -> dict[str, Any] | None: ...
        async def _judge_confidence(
            self, repository, task_id: str, request: TaskCreateRequest, answer: str
        ) -> tuple[float, ModelOutput | None]: ...
        async def _mcp_tool_defs(self, request: TaskCreateRequest) -> list[dict[str, Any]]: ...
        @staticmethod
        def _model_identity(model: ModelReference) -> tuple[str, str, str]: ...
        async def _model_loaded_state(self, model: ModelReference) -> bool | None: ...
        def _model_markdown(self, task_id: str, role: str, model: ModelReference, output: ModelOutput) -> str: ...
        def _record_artifact_safely(
            self, repository, task_id: str, run_id: str | None, invocation_id: str | None, artifact_type: str, write
        ) -> None: ...
        def _record_images_safely(
            self,
            repository,
            task_id: str,
            run_id: str | None,
            invocation_id: str | None,
            output: ModelOutput,
            folder: str,
        ) -> None: ...
        async def _run_agent_loop(
            self,
            repository,
            task_id: str,
            request: TaskCreateRequest,
            model: ModelReference,
            *,
            run_id: str | None,
            messages: list[dict[str, Any]],
            skills: list[Any],
            max_iterations: int,
            role: str,
            tools: list[dict[str, Any]] | None = None,
            client_tool_names: set[str] | None = None,
            allow_parallel: bool = False,
            on_iteration: Any | None = None,
            iteration_offset: int = 0,
            sandbox_files: dict[str, Path] | None = None,
            mcp_servers: list[str] | None = None,
            should_yield: Any | None = None,
        ) -> AgentLoopResult: ...
        async def _run_cancellable(self, repository, task_id: str, operation) -> ModelOutput: ...
        def _safe_name(self, value: str) -> str: ...
        def _usage(self, outputs: list[ModelOutput]) -> dict: ...
        def _with_remaining_budget(
            self, request: TaskCreateRequest, outputs: list[ModelOutput]
        ) -> TaskCreateRequest: ...
        def _write_request_artifacts(
            self, repository, task_id: str, run_id: str, request: TaskCreateRequest, resource_plan: dict
        ) -> None: ...
        def _yield_check(
            self, repository, task_id: str, request: TaskCreateRequest, strategy: ExecutionStrategy
        ) -> Callable[[], str | None] | None: ...
        def initialize_run(self, task_id: str, request: TaskCreateRequest) -> str | None: ...
        def provider_user_prompt(self, request: TaskCreateRequest) -> str: ...

    def _load_consensus_checkpoint(self, repository, task_id: str) -> _ConsensusCheckpoint | None:
        """El punto de control que dejó la tarea al ceder el turno, si lo hay.

        Uno que no se puede leer se descarta y la tarea empieza de cero: rehacer
        las propuestas cuesta tiempo, retomar con un estado a medias costaría
        una respuesta equivocada.
        """
        state = repository.load_consensus_state(task_id)
        if not state:
            return None
        try:
            round_one = _RoundOutcome(
                proposals=[
                    (ModelReference.model_validate(item["model"]), _output_from_checkpoint(item["output"]))
                    for item in state.get("proposals") or []
                ],
                skipped=list(state.get("skipped") or []),
                max_parallel_launched=int(state.get("max_parallel_launched") or 0),
                waves_done=int(state.get("waves_done") or 0),
            )
            synthesis = None
            saved = state.get("synthesis")
            if saved:
                synthesis = _Synthesis(
                    model=ModelReference.model_validate(saved["model"]),
                    output=_output_from_checkpoint(saved["output"]),
                    invocation_id=saved.get("invocation_id"),
                    degraded=bool(saved.get("degraded")),
                    failures=list(saved.get("failures") or []),
                )
            return _ConsensusCheckpoint(
                proposers=[ModelReference.model_validate(item) for item in state["proposers"]],
                arbiter=ModelReference.model_validate(state["arbiter"]),
                resource_plan=ResourcePlan.model_validate(state["resource_plan"]),
                round_one=round_one,
                synthesis=synthesis,
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("consensus.checkpoint_unreadable", exc_info=True, extra={
                "event": "consensus.checkpoint_unreadable", "task_id": task_id,
            })
            return None

    def _yield_consensus(
        self,
        repository,
        task_id: str,
        checkpoint: _ConsensusCheckpoint,
        progress: dict[str, Any],
        urgent_task_id: str,
    ) -> None:
        round_one = checkpoint.round_one
        synthesis = checkpoint.synthesis
        state = {
            "proposers": [model.model_dump(mode="json") for model in checkpoint.proposers],
            "arbiter": checkpoint.arbiter.model_dump(mode="json"),
            "resource_plan": checkpoint.resource_plan.model_dump(mode="json"),
            "waves_done": round_one.waves_done,
            "proposals": [
                {"model": model.model_dump(mode="json"), "output": _output_to_checkpoint(output)}
                for model, output in round_one.proposals
            ],
            "skipped": round_one.skipped,
            "max_parallel_launched": round_one.max_parallel_launched,
            "synthesis": None if synthesis is None else {
                "model": synthesis.model.model_dump(mode="json"),
                "output": _output_to_checkpoint(synthesis.output),
                "invocation_id": synthesis.invocation_id,
                "degraded": synthesis.degraded,
                "failures": synthesis.failures,
            },
        }
        paid = [output for _, output in round_one.proposals]
        if synthesis is not None and not synthesis.degraded:
            paid.append(synthesis.output)
        repository.yield_consensus_turn(
            task_id, state,
            progress={
                **progress,
                "phase": TaskStatus.queued.value,
                "invocations_completed": len(paid),
                "wave_current": round_one.waves_done,
                "active_invocations": [],
                "skipped_proposers": round_one.skipped,
                "cost_actual_usd": sum(output.cost_usd for output in paid),
                "yielded_to": urgent_task_id,
            },
            urgent_task_id=urgent_task_id,
        )
        logger.info("consensus.yielded", extra={
            "event": "consensus.yielded", "task_id": task_id,
            "waves_done": round_one.waves_done, "synthesized": synthesis is not None,
            "urgent_task_id": urgent_task_id,
        })

    async def _first_synthesis(
        self,
        repository,
        task_id: str,
        run_id: str,
        request: TaskCreateRequest,
        proposers: list[ModelReference],
        arbiter: ModelReference,
        resource_plan: ResourcePlan,
        progress: dict[str, Any],
        round_one: _RoundOutcome,
    ) -> _Synthesis | None:
        """Quorum y síntesis de la primera ronda. None = la tarea ya quedó
        escrita (fallida por quorum o cancelada) y no hay nada más que hacer."""
        proposals = round_one.proposals
        skipped_proposers = round_one.skipped
        quorum = min(2, len(proposers))
        if len(proposals) < quorum:
            repository.set_stage_status(task_id, run_id, "proposing", "failed")
            causes = ", ".join(
                f"{item.get('model')}: {item.get('code')}" for item in skipped_proposers
            ) or "sin detalle de las bajas"
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={**progress, "phase": TaskStatus.failed.value},
                error={
                    "code": "CONSENSUS_QUORUM_NOT_REACHED",
                    # El contrato (Client_API §8) promete {code, message,
                    # retryable} en todo error, y este era el único que no los
                    # traía: el cliente no sabía si reintentar.
                    "message": (
                        f"Solo {len(proposals)} de {len(proposers)} proponentes respondieron y "
                        f"hacen falta {quorum} ({causes})"
                    ),
                    # Reintentar sirve si alguna baja fue transitoria (timeout,
                    # proveedor caído, memoria). Si todas fueron definitivas
                    # —eco del prompt, salida degenerada, capacidad—, repetir
                    # con los mismos modelos fallaría igual.
                    "retryable": any(item.get("retryable") for item in skipped_proposers),
                    "completed": len(proposals),
                    "required": quorum,
                    # Sin esto, una tarea en la que fallaron TODOS los
                    # proposers solo diría "0 de 2": el porqué de cada baja se
                    # quedaba en el log de eventos.
                    "skipped_proposers": skipped_proposers,
                },
            )
            return None

        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return None

        repository.set_stage_status(task_id, run_id, "proposing", "completed")
        repository.set_stage_status(task_id, run_id, "synthesizing", "running")
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
                "max_parallel_invocations_launched": round_one.max_parallel_launched,
                "cost_actual_usd": sum(output.cost_usd for _, output in proposals),
            },
        )
        synthesis_request = self._with_remaining_budget(request, [output for _, output in proposals])
        synthesis = await self._synthesize_with_recovery(
            repository, task_id, run_id, request, synthesis_request, arbiter, proposals,
        )
        if repository.is_cancel_requested(task_id):
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return None
        return synthesis

    async def _process_consensus(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        run_id = repository.get_consensus_run_id(task_id) or self.initialize_run(task_id, request)
        assert run_id is not None
        should_yield = self._yield_check(
            repository, task_id, request, ExecutionStrategy.mixture_of_agents,
        )
        # Si cedió el turno antes, se retoma con LO MISMO que se eligió
        # entonces: proponentes, árbitro y plan de oleadas. Volver a elegirlos
        # podría cambiar el reparto y desemparejar las propuestas ya pagadas de
        # las oleadas que faltan.
        checkpoint = self._load_consensus_checkpoint(repository, task_id)
        repository.set_stage_status(task_id, run_id, "resource_planning", "running")
        if checkpoint is not None:
            proposers = checkpoint.proposers
            arbiter = checkpoint.arbiter
            resource_plan = checkpoint.resource_plan
            repository.add_event(task_id, "consensus.resumed", {
                "waves_done": checkpoint.round_one.waves_done,
                "proposals": len(checkpoint.round_one.proposals),
                "synthesized": checkpoint.synthesis is not None,
            })
        else:
            proposers = await self._select_proposers(request)
            arbiter = await self._select_arbiter(request)
            if request.execution.proposer_skills:
                ensure_capable = getattr(self.provider, "ensure_agent_capable", None)
                if ensure_capable is not None:
                    for proposer in proposers:
                        await ensure_capable(proposer)
            plan_execution = request.execution.model_copy(update={"max_proposers": len(proposers)})
            plan_request = request.model_copy(update={"execution": plan_execution})
            try:
                resource_plan = self.scheduler.plan(
                    plan_request, await self._proposer_footprints(proposers)
                )
            except ResourcePlanningError as error:
                repository.set_stage_status(task_id, run_id, "resource_planning", "failed")
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
        if checkpoint is None:
            # Al retomar ya están escritos: son de la petición, no del tramo.
            self._write_request_artifacts(
                repository, task_id, run_id, request, resource_plan.model_dump(mode="json"),
            )

        repository.set_stage_status(task_id, run_id, "resource_planning", "completed")
        if checkpoint is not None and checkpoint.synthesis is not None:
            # Cedió tras la primera síntesis: la ronda 1 está entera y
            # sintetizada, y lo que falta es la segunda.
            synthesis = checkpoint.synthesis
            round_one = checkpoint.round_one
            proposals = round_one.proposals
            skipped_proposers = round_one.skipped
            max_parallel_launched = round_one.max_parallel_launched
            repository.set_stage_status(task_id, run_id, "proposing", "completed")
            repository.set_stage_status(task_id, run_id, "synthesizing", "running")
        else:
            repository.set_stage_status(task_id, run_id, "proposing", "running")
            repository.update_task(
                task_id, TaskStatus.proposing, progress={**progress, "phase": TaskStatus.proposing.value},
            )
            round_one = await self._run_proposal_round(
                repository, task_id, run_id, request, request, proposers, resource_plan, progress,
                resume=checkpoint.round_one if checkpoint is not None else None,
                should_yield=should_yield,
            )
            if round_one.cancelled:
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            if round_one.yielded_to is not None:
                self._yield_consensus(
                    repository, task_id,
                    _ConsensusCheckpoint(proposers, arbiter, resource_plan, round_one, None),
                    progress, round_one.yielded_to,
                )
                return
            proposals = round_one.proposals
            skipped_proposers = round_one.skipped
            max_parallel_launched = round_one.max_parallel_launched
            first = await self._first_synthesis(
                repository, task_id, run_id, request, proposers, arbiter, resource_plan,
                progress, round_one,
            )
            if first is None:
                return
            synthesis = first
            # Ceder tras la síntesis solo compensa si queda segunda ronda: sin
            # ella lo que falta es escribir el resultado, no otra inferencia.
            if (
                should_yield is not None
                and request.execution.max_rounds > 1
                and not synthesis.degraded
            ):
                urgent = should_yield()
                if urgent is not None:
                    self._yield_consensus(
                        repository, task_id,
                        _ConsensusCheckpoint(proposers, arbiter, resource_plan, round_one, synthesis),
                        progress, urgent,
                    )
                    return

        # Segunda ronda: los proponentes ven la síntesis anterior y la mejoran.
        # `synthesis`, `proposals` y los acumuladores se reemplazan solo si la
        # ronda sale bien; una ronda 2 fallida deja la tarea exactamente como la
        # dejaba una sola ronda.
        rounds = 1
        confidence: float | None = None
        extra_outputs: list[ModelOutput] = []
        if request.execution.max_rounds > 1 and not synthesis.degraded:
            second = await self._run_second_round(
                repository, task_id, run_id, request, proposers, arbiter,
                resource_plan, progress, proposals, synthesis,
            )
            if second is None:
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            confidence = second.confidence
            extra_outputs.extend(second.extra_outputs)
            if second.synthesis is not None:
                rounds = 2
                proposals = second.proposals
                skipped_proposers = skipped_proposers + second.skipped
                max_parallel_launched = max(max_parallel_launched, second.max_parallel_launched)
                synthesis = second.synthesis

        # Una propuesta degradada ya está contada entre las propuestas: sumarla
        # otra vez inflaría tokens y coste con una inferencia que no existió.
        all_outputs = [output for _, output in proposals] + extra_outputs
        if not synthesis.degraded:
            all_outputs.append(synthesis.output)
        self._enforce_budget(request, all_outputs)
        if not synthesis.degraded:
            self._record_artifact_safely(
                repository, task_id, run_id, synthesis.invocation_id, "synthesis_output",
                lambda: self.artifacts.write_markdown(
                    task_id,
                    f"synthesis/arbiter_{self._safe_name(synthesis.model.model)}.md",
                    self._model_markdown(task_id, "arbiter", synthesis.model, synthesis.output),
                ),
            )
        self._record_artifact_safely(
            repository, task_id, run_id, None, "final_output",
            lambda: self.artifacts.write_markdown(
                task_id, "synthesis/final.md", synthesis.output.content or ""
            ),
        )
        self._record_images_safely(
            repository, task_id, run_id, None, synthesis.output, "synthesis",
        )

        warnings = [self._skipped_proposer_warning(item) for item in skipped_proposers]
        warnings.extend(self._arbiter_failure_warning(item) for item in synthesis.failures)
        if synthesis.degraded:
            warnings.append(
                "sin síntesis: responde la propuesta de "
                f"{synthesis.model.provider}/{synthesis.model.deployment}/{synthesis.model.model}"
            )
        result: dict[str, Any] = {
            "result_markdown": synthesis.output.content,
            "assistant_content": synthesis.output.content,
            "inference_kind": "chat",
            "output_format": request.output.format.value,
            "consensus": {
                "level": request.execution.preset.value,
                "confidence": confidence,
                "proposers_completed": len(proposals),
                "proposers_failed": len(skipped_proposers),
                "rounds": rounds,
                "remaining_disagreements": [],
                "warnings": warnings,
                "synthesized": not synthesis.degraded,
            },
            "arbiter_failures": synthesis.failures,
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
            + ([] if synthesis.degraded else [synthesis.model.model_dump(mode="json")]),
            "model_used": synthesis.model.model_dump(mode="json"),
            "fallback_used": self._fallback_used(request, synthesis.model),
            "artifacts_root": str(self.artifacts.root / task_id),
        }
        repository.set_stage_status(
            task_id, run_id, "synthesizing", "degraded" if synthesis.degraded else "completed",
        )
        repository.update_task(
            task_id,
            TaskStatus.completed,
            progress={
                **progress,
                "phase": TaskStatus.completed.value,
                "invocations_completed": len(all_outputs),
                # El total se planificó para una ronda; al cerrar se sabe cuántas
                # invocaciones hubo de verdad y una barra no puede terminar por
                # encima de su propio techo.
                "invocations_total": max(len(proposers) + 1, len(all_outputs)),
                "rounds": rounds,
                "active_invocations": [],
                "skipped_proposers": skipped_proposers,
                "max_parallel_invocations_launched": max_parallel_launched,
                "cost_actual_usd": result["usage"]["cost_usd"],
            },
            result=result,
        )

    _SECOND_ROUND_PROMPT = (
        "<original_request>\n{request}\n</original_request>\n\n"
        "<previous_synthesis>\n{synthesis}\n</previous_synthesis>"
    )

    async def _run_second_round(
        self,
        repository,
        task_id: str,
        run_id: str,
        request: TaskCreateRequest,
        proposers: list[ModelReference],
        arbiter: ModelReference,
        resource_plan: Any,
        progress: dict[str, Any],
        proposals: list[tuple[ModelReference, ModelOutput]],
        synthesis: _Synthesis,
    ) -> _SecondRound | None:
        """Segunda capa del consenso: los proponentes ven la síntesis anterior y
        la mejoran. Devuelve None solo si la tarea se canceló.

        `max_rounds` es un TECHO, no una orden, igual que `max_proposers` o
        `max_iterations`. Quién decide si la ronda se gasta:

        - `max_judges >= 1` (por defecto): el juez de confianza puntúa la
          síntesis y solo se paga otra ronda si baja del umbral. Es el mismo
          juez y el mismo umbral que usa la escalada `single` → mixture.
        - `max_judges == 0`: el cliente ha dicho que no quiere pagar juez, así
          que la ronda se gasta siempre. Es la lectura literal de pedir dos.

        **Invariante: esta ronda no puede empeorar el resultado.** Cualquier
        fallo —sin presupuesto, sin quorum, árbitro caído, contexto desbordado
        por la síntesis añadida— se traga aquí y la tarea entrega la ronda 1.
        Sin esto un CONTEXT_LIMIT_EXCEEDED de la ronda 2 tumbaría la tarea
        entera, que es justo lo que se estaba evitando al añadirla.
        """
        outcome = _SecondRound()
        router = self.scheduler.config.strategy_router
        answer = (synthesis.output.content or "").strip()
        if not answer:
            return outcome

        if request.execution.max_judges >= 1:
            score, judge_output = await self._judge_confidence(repository, task_id, request, answer)
            outcome.confidence = score
            if judge_output is not None:
                outcome.extra_outputs.append(judge_output)
            enough = score >= router.escalation_min_confidence
            repository.add_event(task_id, "consensus.round_gate", {
                "score": score, "threshold": router.escalation_min_confidence,
                "second_round": not enough,
            })
            if enough:
                return outcome
        else:
            repository.add_event(task_id, "consensus.round_gate", {
                "score": None, "threshold": None, "second_round": True,
                "reason": "max_judges=0: segunda ronda sin juzgar la primera",
            })

        if repository.is_cancel_requested(task_id):
            return None

        paid = [output for _, output in proposals] + list(outcome.extra_outputs)
        if not synthesis.degraded:
            paid.append(synthesis.output)
        try:
            round_request = self._round_two_request(request, synthesis, paid)
        except ProviderError as error:
            repository.add_event(task_id, "consensus.round_skipped", {
                "round": 2, "code": error.code, "detail": str(error),
            })
            return outcome

        refiners = [model.model_copy(update={"role": "refiner"}) for model in proposers]
        # El total declarado sube al empezar la ronda de verdad, no antes: si se
        # anunciara desde el principio, la barra de una tarea que se queda en
        # una ronda —el caso normal— se pararía a mitad para siempre.
        judged = 1 if request.execution.max_judges >= 1 else 0
        already_paid = len(proposals) + 1 + judged  # propuestas + árbitro + juez
        round_progress = {
            **progress,
            "invocations_total": len(proposers) * 2 + 2 + judged,
        }
        try:
            second = await self._run_proposal_round(
                repository, task_id, run_id, request, round_request, refiners,
                resource_plan, round_progress, round_index=2, prior_proposals=proposals,
                completed_offset=already_paid,
            )
        except ProviderError as error:
            repository.add_event(task_id, "consensus.round_failed", {
                "round": 2, "code": error.code, "detail": str(error),
            })
            return outcome
        if second.cancelled:
            return None

        outcome.skipped = second.skipped
        outcome.max_parallel_launched = second.max_parallel_launched
        if len(second.proposals) < min(2, len(proposers)):
            repository.add_event(task_id, "consensus.round_failed", {
                "round": 2, "code": "CONSENSUS_QUORUM_NOT_REACHED",
                "completed": len(second.proposals), "required": min(2, len(proposers)),
            })
            return outcome

        paid.extend(output for _, output in second.proposals)
        repository.update_task(
            task_id,
            TaskStatus.synthesizing,
            progress={
                **round_progress,
                "phase": TaskStatus.synthesizing.value,
                "invocations_completed": already_paid + len(second.proposals),
                "round_current": 2,
                "active_invocations": [arbiter.model_dump(mode="json")],
                "skipped_proposers": second.skipped,
                "max_parallel_invocations_launched": second.max_parallel_launched,
                "cost_actual_usd": sum(output.cost_usd for output in paid),
            },
        )
        try:
            synthesis_request = self._with_remaining_budget(request, paid)
            second_synthesis = await self._synthesize_with_recovery(
                repository, task_id, run_id, request, synthesis_request, arbiter, second.proposals,
            )
        except ProviderError as error:
            repository.add_event(task_id, "consensus.round_failed", {
                "round": 2, "code": error.code, "detail": str(error),
            })
            return outcome
        if second_synthesis.degraded:
            # Una síntesis degradada de la ronda 2 es una propuesta suelta; la
            # síntesis real de la ronda 1 es mejor que eso.
            repository.add_event(task_id, "consensus.round_failed", {
                "round": 2, "code": "ARBITER_DEGRADED",
                "detail": "sin síntesis en la segunda ronda; se conserva la primera",
            })
            return outcome

        # A partir de aquí manda la ronda 2: lo de la 1 queda pagado pero fuera
        # del resultado, así que viaja en extra_outputs para el coste.
        outcome.extra_outputs.extend(output for _, output in proposals)
        outcome.extra_outputs.append(synthesis.output)
        outcome.proposals = second.proposals
        outcome.synthesis = second_synthesis
        self._record_artifact_safely(
            repository, task_id, run_id, None, "round_output",
            lambda: self.artifacts.write_markdown(
                task_id, "synthesis/round_01.md", answer,
            ),
        )
        return outcome

    def _round_two_request(
        self,
        request: TaskCreateRequest,
        synthesis: _Synthesis,
        paid: list[ModelOutput],
    ) -> TaskCreateRequest:
        """Petición de la segunda ronda: la original más la síntesis anterior.

        La síntesis viaja neutralizada y sin compresión, por el mismo motivo que
        las candidatas del árbitro: es contenido generado que no puede cerrar su
        propio tag ni perder cifras por una poda léxica.
        """
        composed = self._SECOND_ROUND_PROMPT.format(
            request=neutralize_consensus_delimiters(self.provider_user_prompt(request)),
            synthesis=neutralize_consensus_delimiters(synthesis.output.content or ""),
        )
        base = self._with_remaining_budget(request, paid)
        return base.model_copy(update={
            "content": request.content.model_copy(update={"prompt": composed, "attachments": []}),
            "prompt_compression": "off",
        })

    async def _run_proposal_round(
        self,
        repository,
        task_id: str,
        run_id: str,
        request: TaskCreateRequest,
        round_request: TaskCreateRequest,
        proposers: list[ModelReference],
        resource_plan: Any,
        progress: dict[str, Any],
        *,
        round_index: int = 1,
        prior_proposals: list[tuple[ModelReference, ModelOutput]] | None = None,
        completed_offset: int | None = None,
        resume: _RoundOutcome | None = None,
        should_yield: Callable[[], str | None] | None = None,
    ) -> _RoundOutcome:
        """Una ronda completa de propuestas, oleada a oleada.

        `request` es la petición de la tarea (manda en presupuesto y preset);
        `round_request` es la que viaja a los modelos, y en la segunda ronda
        lleva además la síntesis anterior. En la primera son la misma.

        `prior_proposals` son las de rondas anteriores: no entran en la síntesis
        de esta, pero sí en el presupuesto y en el coste que muestra el panel,
        porque ya se pagaron.

        `completed_offset` es desde qué número sigue contando la barra. Por
        defecto son las propuestas anteriores, pero en la segunda ronda hay
        además un árbitro y puede haber un juez ya pagados, y sin contarlos la
        barra retrocedería al empezar la ronda.

        `resume` es lo que dejó esta misma ronda antes de ceder el turno: sus
        oleadas hechas (`waves_done`) se saltan y sus propuestas cuentan como
        ya cobradas. `should_yield` se consulta al cerrar cada oleada; si
        devuelve una tarea, la ronda para con `yielded_to` y `waves_done`.
        """
        outcome = _RoundOutcome()
        start_wave = 0
        if resume is not None:
            outcome.proposals = list(resume.proposals)
            outcome.skipped = list(resume.skipped)
            outcome.max_parallel_launched = resume.max_parallel_launched
            start_wave = resume.waves_done
        previous = list(prior_proposals or [])
        prior_cost = sum(output.cost_usd for _, output in previous)
        completed = len(previous) if completed_offset is None else completed_offset
        completed += len(outcome.proposals)
        attempted = 0

        def snapshot(wave_index: int, active: list[ModelReference]) -> dict[str, Any]:
            return {
                **progress,
                "phase": TaskStatus.proposing.value,
                "invocations_completed": completed,
                "wave_current": wave_index,
                "round_current": round_index,
                "active_invocations": [item.model_dump(mode="json") for item in active],
                "skipped_proposers": outcome.skipped,
                "max_parallel_invocations_launched": outcome.max_parallel_launched,
                "cost_actual_usd": prior_cost + sum(item.cost_usd for _, item in outcome.proposals),
            }

        for wave_index, labels in enumerate(resource_plan.waves, start=1):
            active_models = proposers[attempted : attempted + len(labels)]
            attempted += len(active_models)
            if wave_index <= start_wave:
                continue
            if repository.is_cancel_requested(task_id):
                outcome.cancelled = True
                return outcome
            outcome.max_parallel_launched = max(
                outcome.max_parallel_launched,
                len(active_models) if request.execution.preset == ExecutionPreset.slow else min(1, len(active_models)),
            )
            launch_models = active_models if request.execution.preset == ExecutionPreset.slow else active_models[:1]
            repository.update_task(task_id, TaskStatus.proposing, progress=snapshot(wave_index, launch_models))
            wave_outputs, wave_skipped = await self._run_proposer_wave(
                repository,
                task_id,
                run_id,
                round_request,
                active_models,
                previous + outcome.proposals,
            )
            outcome.skipped.extend(wave_skipped)
            if wave_skipped and not wave_outputs:
                repository.update_task(task_id, TaskStatus.proposing, progress=snapshot(wave_index, []))

            if repository.is_cancel_requested(task_id):
                outcome.cancelled = True
                return outcome

            for model, output, invocation_id in wave_outputs:
                ordinal = len(outcome.proposals) + 1
                role = model.role or "proposer"
                # La segunda ronda escribe en su propio prefijo: sin él, sus
                # artefactos pisarían los de la primera y se perdería la
                # evidencia de cómo cambió la respuesta entre rondas.
                folder = "proposers" if round_index == 1 else f"proposers_r{round_index}"
                self._record_artifact_safely(
                    repository, task_id, run_id, invocation_id, "proposer_output",
                    lambda o=ordinal, r=role, m=model, out=output, f=folder: self.artifacts.write_markdown(
                        task_id,
                        f"{f}/{o:02d}_{self._safe_name(r)}_{self._safe_name(m.model)}.md",
                        self._model_markdown(task_id, r, m, out),
                    ),
                )
                outcome.proposals.append((model, output))
                completed += 1
                self._enforce_budget(
                    request, [item for _, item in previous + outcome.proposals],
                )
                repository.update_task(task_id, TaskStatus.proposing, progress=snapshot(wave_index, []))
            outcome.waves_done = wave_index
            # Con la oleada cerrada y cobrada, que es donde pausar no tira nada.
            # También tras la última: lo que queda, la síntesis, es otra llamada.
            if should_yield is not None:
                urgent = should_yield()
                if urgent is not None:
                    outcome.yielded_to = urgent
                    return outcome
        outcome.waves_done = len(resource_plan.waves)
        return outcome

    async def _run_proposer_wave(
        self,
        repository,
        task_id: str,
        run_id: str | None,
        request: TaskCreateRequest,
        active_models: list[ModelReference],
        completed_proposals: list[tuple[ModelReference, ModelOutput]],
    ) -> tuple[list[tuple[ModelReference, ModelOutput, str]], list[dict[str, Any]]]:
        completed_outputs = [output for _, output in completed_proposals]
        invocation_request = self._with_wave_budget(request, completed_outputs, len(active_models))
        proposer_skills = list(request.execution.proposer_skills)
        mcp_servers = list(request.execution.agent.mcp_servers)
        mcp_tools = await self._mcp_tool_defs(request) if mcp_servers else []
        allow_parallel = request.execution.preset == ExecutionPreset.slow and len(active_models) > 1
        sandbox_files = (
            self.ingestion.tabular_sandbox_files(request)
            if self.ingestion is not None and "run_code" in proposer_skills
            else {}
        )

        async def invoke_agent(ordinal: int, model: ModelReference):
            role = model.role or "proposer"
            system = role_system_prompt(role) or ROLE_SYSTEM_PROMPTS["proposer"]
            messages = [
                {"role": "system", "content": with_output_language(
                    f"{system}\n\n{self._AGENT_SYSTEM_PROMPT}", invocation_request,
                )},
                {"role": "user", "content": multimodal_content(
                    self.provider_user_prompt(invocation_request),
                    invocation_request.inline_images,
                )},
            ]
            try:
                loop = await self._run_agent_loop(
                    repository, task_id, invocation_request, model, run_id=run_id, messages=messages,
                    skills=proposer_skills, max_iterations=request.execution.agent.max_iterations,
                    role=role, allow_parallel=allow_parallel, sandbox_files=sandbox_files,
                    tools=skill_definitions(proposer_skills) + mcp_tools,
                    mcp_servers=mcp_servers,
                )
            except ProviderError as error:
                return model, None, None, error
            if loop.stop_reason == "cancelled":
                return model, None, loop.last_invocation_id, ProviderError("TASK_CANCELLED", "Cancelada")
            aggregated = ModelOutput(
                loop.content,
                sum(o.tokens_input for o in loop.outputs), sum(o.tokens_output for o in loop.outputs),
                round(sum(o.cost_usd for o in loop.outputs), 8), sum(o.latency_ms for o in loop.outputs),
            )
            return model, aggregated, loop.last_invocation_id, None

        async def invoke_single(ordinal: int, model: ModelReference):
            role = model.role or "proposer"
            # Checkpoint pre-vuelo (véase start_invocation): la respuesta se
            # persiste aquí mismo al llegar, no al final de la ola, para que
            # un crash a mitad de ola no pierda las propuestas ya cobradas.
            invocation_id = repository.start_invocation(
                task_id, run_id, role, model, classify_task_type(request),
                await self._model_loaded_state(model),
                excluded_from_learning=request.exclude_from_model_learning,
                fingerprint=await self._invocation_fingerprint(model),
                compression=self._compression_echo(invocation_request),
            )
            try:
                output = await self._run_cancellable(
                    repository,
                    task_id,
                    self.provider.propose(invocation_request, model, len(completed_proposals) + ordinal),
                )
                repository.complete_invocation(invocation_id, task_id, output)
                return model, output, invocation_id, None
            except ProviderError as error:
                self._attach_error_context(error, "proposing", model)
                repository.fail_invocation(invocation_id, task_id, error.code, str(error), error.output)
                return model, None, invocation_id, error

        # Con servidores MCP el proponente necesita el bucle de tools aunque no
        # se le hayan dado skills del broker: las herramientas de terceros son
        # el único motivo de que haya bucle.
        invoke = invoke_agent if (proposer_skills or mcp_tools) else invoke_single

        if request.execution.preset == ExecutionPreset.slow and len(active_models) > 1:
            results = await asyncio.gather(
                *(invoke(ordinal, model) for ordinal, model in enumerate(active_models, start=1))
            )
        else:
            results = []
            for ordinal, model in enumerate(active_models, start=1):
                results.append(await invoke(ordinal, model))

        outputs: list[tuple[ModelReference, ModelOutput, str]] = []
        skipped: list[dict[str, Any]] = []
        for model, output, invocation_id, error in results:
            if error is not None:
                # Un proposer caído es una baja, no el final del consenso: el
                # quorum de más abajo decide si con los que quedan se puede
                # seguir. Antes cualquier error definitivo (un modelo que
                # devuelve basura, sin ir más lejos) tumbaba la tarea entera
                # aunque las otras propuestas ya estuvieran cobradas.
                if error.code in _TASK_FATAL_ERRORS:
                    raise error
                skipped_item = self._skipped_proposer_detail(model, error)
                repository.add_event(task_id, "proposer.skipped", skipped_item)
                skipped.append(skipped_item)
                continue
            assert output is not None
            outputs.append((model, output, invocation_id))
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

    def _arbiter_failure_warning(self, item: dict[str, Any]) -> str:
        model_name = f"{item.get('provider')}/{item.get('deployment')}/{item.get('model')}"
        return f"árbitro descartado ({model_name}): {item.get('code')}"

    async def _synthesize_with_recovery(
        self,
        repository,
        task_id: str,
        run_id: str | None,
        request: TaskCreateRequest,
        synthesis_request: TaskCreateRequest,
        arbiter: ModelReference,
        proposals: list[tuple[ModelReference, ModelOutput]],
    ) -> _Synthesis:
        """Sintetiza; y si el árbitro no responde, no tira la tarea.

        Un árbitro caído mataba el consenso entero con las propuestas ya
        hechas y cobradas: minutos de máquina y una respuesta válida en la mano
        se iban a la basura por el último eslabón (task_c8da5dd6…, donde el
        árbitro devolvió su propio prompt y las tres propuestas eran buenas).
        Aquí se prueba un árbitro distinto y, si tampoco, responde la mejor
        propuesta tal cual: todos los roles de proposer contestan al usuario,
        no al árbitro, así que su salida es una respuesta completa —peor que
        una síntesis, incomparablemente mejor que un error.

        Los errores de _ARBITER_FATAL_ERRORS siguen tumbando la tarea: hablan
        de la petición (cancelada, sin presupuesto), no del árbitro, y cambiar
        de modelo no arregla ninguno. Un árbitro sin ventana para la síntesis,
        en cambio, es un defecto de ese árbitro y se recupera como los demás.
        """
        outputs = [output for _, output in proposals]
        failures: list[dict[str, Any]] = []
        attempted: list[ModelReference] = []
        candidate: ModelReference | None = arbiter
        while candidate is not None:
            attempted.append(candidate)
            invocation_id = repository.start_invocation(
                task_id, run_id, "arbiter", candidate, classify_task_type(request),
                await self._model_loaded_state(candidate),
                # Sin esto, una tarea excluida del aprendizaje seguía moviendo
                # el historial del árbitro (cuarentena, aptitud por rol).
                excluded_from_learning=request.exclude_from_model_learning,
                compression=self._compression_echo(synthesis_request),
            )
            try:
                output = await self._run_cancellable(
                    repository, task_id, self.provider.synthesize(synthesis_request, candidate, outputs),
                )
            except ProviderError as error:
                self._attach_error_context(error, "synthesizing", candidate, role="arbiter")
                repository.fail_invocation(
                    invocation_id, task_id, error.code, str(error), error.output
                )
                if error.code in _ARBITER_FATAL_ERRORS or repository.is_cancel_requested(task_id):
                    raise
                detail = self._skipped_proposer_detail(candidate, error)
                repository.add_event(task_id, "arbiter.failed", detail)
                failures.append(detail)
                candidate = await self._substitute_arbiter(
                    request, attempted, synthesis_request, proposals,
                )
                continue
            # El checkpoint se cierra en cuanto hay respuesta: cancelación o
            # presupuesto excedido no deben dejar la fila en 'started'.
            repository.complete_invocation(invocation_id, task_id, output)
            return _Synthesis(candidate, output, invocation_id, degraded=False, failures=failures)

        model, output = self._fallback_proposal(proposals)
        repository.add_event(task_id, "arbiter.fallback", {
            "answered_by": model.model_dump(mode="json"),
            "role": model.role or "proposer",
            "attempts": [item["model"] for item in failures],
            "code": failures[-1]["code"] if failures else None,
        })
        logger.warning(
            "arbiter.fallback",
            extra={
                "event": "arbiter.fallback",
                "task_id": task_id,
                "answered_by": model.model,
                "code": failures[-1]["code"] if failures else None,
            },
        )
        return _Synthesis(model, output, None, degraded=True, failures=failures)

    async def _substitute_arbiter(
        self,
        request: TaskCreateRequest,
        attempted: list[ModelReference],
        sizing_request: TaskCreateRequest,
        proposals: list[tuple[ModelReference, ModelOutput]],
    ) -> ModelReference | None:
        """Otro árbitro, distinto de los ya intentados; None si no procede.

        Un árbitro pedido a mano (selection.arbiter, preferred_arbiter,
        target_model) no se cambia: ahí el modelo es parte de lo pedido y no
        una decisión del broker. Solo se sustituye una vez, para que un
        catálogo entero de modelos rotos no convierta una tarea en una ronda
        interminable de intentos que el usuario paga en espera.

        El repuesto tiene que poder leer la síntesis entera: si ninguno de los
        candidatos tiene ventana, se devuelve None y responde la mejor
        propuesta, sin gastar una invocación que se sabe perdida.
        """
        selection = request.execution.selection
        if len(attempted) > 1 or not selection.allow_substitution:
            return None
        if selection.arbiter is not None or selection.preferred_arbiter is not None:
            return None
        requirements = request.model_requirements
        if requirements.target_model is not None or requirements.preferred_model is not None:
            return None
        used = {self._model_identity(model) for model in attempted}
        # Más de uno por si los primeros no tienen ventana para la síntesis.
        count = len(used) + _ARBITER_SUBSTITUTE_POOL
        try:
            ranked = await self.provider.select(request, count, ["arbiter"] * count)
        except ProviderError:
            # Quedarse sin árbitro de repuesto no es un fallo: la degradación
            # de más abajo sigue teniendo respuesta que entregar.
            return None
        windows = await self._context_windows()
        prompt = self._synthesis_sizing_prompt(request, proposals)
        for model in ranked:
            if self._model_identity(model) in used:
                continue
            window = windows.get(self._model_identity(model))
            # Ventana desconocida: no se descarta por lo que no se sabe.
            if window is None or context_fits_with_capped_output(sizing_request, window, prompt):
                return model
            used.add(self._model_identity(model))
            logger.info(
                "arbiter.substitute_too_small",
                extra={"event": "arbiter.substitute_too_small", "model": model.model},
            )
        return None

    async def _context_windows(self) -> dict[tuple[str, str, str], int | None]:
        """Ventana de cada modelo del catálogo; vacío si no se puede consultar."""
        try:
            catalog = await self.provider.models()
        except Exception:
            return {}
        return {
            (str(item.get("provider") or "").lower(), str(item.get("deployment") or "").lower(),
             str(item.get("name") or "")): item.get("context_window")
            for item in catalog
        }

    @staticmethod
    def _synthesis_sizing_prompt(
        request: TaskCreateRequest, proposals: list[tuple[ModelReference, ModelOutput]],
    ) -> str:
        """Lo que el árbitro va a leer: la petición y todas las propuestas.

        El router mide el contexto con la petición original, y la síntesis
        pesa varias veces más: con tres propuestas de apuntes, de unos 3k
        tokens se pasa a más de 20k. Por eso un árbitro de 8k superaba el
        filtro y luego no cabía (qwen/qwen3.6-35b-a3b, 16 de 16 rechazos).
        """
        parts = [ROLE_SYSTEM_PROMPTS["arbiter"], request.content.prompt]
        parts += [output.content or "" for _, output in proposals]
        return "\n\n".join(parts)

    def _fallback_proposal(
        self, proposals: list[tuple[ModelReference, ModelOutput]]
    ) -> tuple[ModelReference, ModelOutput]:
        """La propuesta que responde cuando no hay síntesis.

        Las propuestas llegan en el orden del router (la primera es el modelo
        mejor situado, y por posición el rol generalista, el que da la
        respuesta más equilibrada). Se salta las vacías: un candidato sin texto
        no es una respuesta.
        """
        return next(
            ((model, output) for model, output in proposals if (output.content or "").strip()),
            proposals[0],
        )

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

    async def _proposer_footprints(
        self, proposers: list[ModelReference]
    ) -> list[ModelFootprint] | None:
        """Tamaño real de cada proposer para que el plan reparta por memoria.

        El árbitro queda fuera a propósito: corre en una fase posterior, cuando
        los proposers ya han liberado, así que reservarle sitio ahora quitaría
        capacidad justo donde hace falta.
        """
        loader = getattr(self.provider, "local_footprints", None)
        if loader is None:
            return None
        try:
            sizes = list(await loader(proposers))
        except Exception:
            # Sin tamaños se sigue con el reparto por conteo de siempre: peor
            # plan, pero nunca una tarea caída por no saber cuánto pesa algo.
            return None
        if len(sizes) != len(proposers):
            return None
        return [
            ModelFootprint(
                label=f"proposer_{index}", model=proposer.model, size_bytes=int(size or 0)
            )
            for index, (proposer, size) in enumerate(zip(proposers, sizes, strict=True), start=1)
        ]

    async def _select_arbiter(self, request: TaskCreateRequest) -> ModelReference:
        selection = request.execution.selection
        if selection.arbiter is not None:
            return selection.arbiter
        if selection.preferred_arbiter is not None:
            return selection.preferred_arbiter
        return (await self.provider.select(request, 1, ["arbiter"]))[0]
