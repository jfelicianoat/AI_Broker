"""Estrategia single: inferencia con relevos, embeddings y escalado por juez de confianza.

Mixin de ConsensusCoordinator (app/coordinator.py), separado al dividir el
coordinador (AB-08). Los métodos son los de antes, sin cambios. El bloque
TYPE_CHECKING declara lo que usan de la clase anfitriona para que mypy
compruebe que esas firmas siguen coincidiendo con las de verdad.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from app.artifacts import ArtifactStore
from app.coordinator_support import _SINGLE_FAILOVER_MODELS, _SINGLE_FATAL_ERRORS
from app.db import dumps_json
from app.providers import ModelOutput, ProviderError
from app.resource_scheduler import ResourceScheduler
from app.schemas import (
    ExecutionPreset,
    ExecutionStrategy,
    InferenceKind,
    ModelReference,
    SchedulingPolicy,
    SelectionMode,
    TaskCreateRequest,
    TaskStatus,
)
from app.task_classifier import classify_task_type

# Mismo logger que el coordinador: los eventos conservan su origen.
logger = logging.getLogger("ai_broker.coordinator")


class SingleMixin:
    if TYPE_CHECKING:
        artifacts: ArtifactStore
        provider: Any
        scheduler: ResourceScheduler

        def _attach_error_context(
            self, error: ProviderError, stage: str, model: ModelReference, role: str | None = None
        ) -> None: ...
        def _compression_echo(self, request: TaskCreateRequest) -> dict[str, str] | None: ...
        async def _invocation_fingerprint(self, model: ModelReference) -> dict[str, Any] | None: ...
        @staticmethod
        def _model_identity(model: ModelReference) -> tuple[str, str, str]: ...
        async def _model_loaded_state(self, model: ModelReference) -> bool | None: ...
        async def _process_consensus(self, repository, task_id: str, request: TaskCreateRequest) -> None: ...
        async def _process_single_map_reduce(self, repository, task_id: str, request: TaskCreateRequest) -> bool: ...
        def _record_images_safely(
            self,
            repository,
            task_id: str,
            run_id: str | None,
            invocation_id: str | None,
            output: ModelOutput,
            folder: str,
        ) -> None: ...
        async def _run_cancellable(self, repository, task_id: str, operation) -> ModelOutput: ...
        def _skipped_proposer_detail(self, model: ModelReference, error: ProviderError) -> dict[str, Any]: ...
        def _technical_result(
            self,
            request: TaskCreateRequest,
            model: ModelReference,
            output: ModelOutput,
            embedding_model: dict[str, Any] | None = None,
        ) -> dict: ...
        def _with_remaining_budget(
            self, request: TaskCreateRequest, outputs: list[ModelOutput]
        ) -> TaskCreateRequest: ...

    async def _single_inference(
        self, repository, task_id: str, request: TaskCreateRequest, role: str = "single",
    ) -> tuple[ModelReference, ModelOutput, str] | None:
        """Ejecuta una inferencia single con reintentos y cancelación. Devuelve
        (modelo, salida, invocation_id) o None si la tarea fue cancelada tras
        ejecutar. Reutilizado por single normal y por el escalado por confianza.

        Si el modelo falla para bien, se pasa al siguiente candidato del ranking
        (hasta _SINGLE_FAILOVER_MODELS). Sin esto, cada modelo roto del
        catálogo costaba una tarea fallida: con cloud primero, HCNSEC anunciaba
        DeepSeek-V4-Pro y lo desviaba a un modelo que respondía 400 «not
        supported» (task_4389236e…, 2026-10-25), mientras quedaban decenas de
        candidatos sanos detrás."""
        candidates = await self._single_candidates(request, role)
        for position, model in enumerate(candidates):
            try:
                return await self._single_inference_on(repository, task_id, request, role, model)
            except ProviderError as error:
                last = position == len(candidates) - 1
                if last or error.code in _SINGLE_FATAL_ERRORS or repository.is_cancel_requested(task_id):
                    raise
                detail = self._skipped_proposer_detail(model, error)
                repository.add_event(task_id, "single.failover", {
                    **detail, "next": candidates[position + 1].model_dump(mode="json"),
                })
                logger.warning(
                    "single.failover",
                    extra={"event": "single.failover", "task_id": task_id,
                           "model": model.model, "code": error.code},
                )
        raise AssertionError("sin candidatos")  # select() nunca devuelve una lista vacía

    async def _single_candidates(
        self, request: TaskCreateRequest, role: str,
    ) -> list[ModelReference]:
        """El modelo elegido y, si la petición lo admite, sus relevos.

        Sin relevo cuando el modelo es parte de lo pedido (target_model, o
        fallback_allowed=false) y en embeddings: otro modelo daría vectores
        de otro espacio, incompatibles con los que el cliente ya tiene.
        """
        requirements = request.model_requirements
        fixed = (
            request.inference_kind == InferenceKind.embedding
            or requirements.target_model is not None
            or not requirements.fallback_allowed
        )
        count = 1 if fixed else _SINGLE_FAILOVER_MODELS
        ranked = await self.provider.select(request, count, [role] * count)
        unique: list[ModelReference] = []
        seen: set[tuple[str, str, str]] = set()
        for model in ranked:
            if self._model_identity(model) not in seen:
                seen.add(self._model_identity(model))
                unique.append(model)
        return unique

    async def _single_inference_on(
        self, repository, task_id: str, request: TaskCreateRequest, role: str,
        model: ModelReference,
    ) -> tuple[ModelReference, ModelOutput, str] | None:
        repository.update_task(
            task_id,
            TaskStatus.generating,
            progress={
                "phase": TaskStatus.generating.value,
                "invocations_completed": 0,
                "invocations_total": 1,
                "active_invocations": [model.model_dump(mode="json")],
                "budget_limit_usd": request.model_requirements.max_cost_usd,
                "cost_estimated_usd": None,
                "cost_actual_usd": 0.0,
            },
            clear_queue_position=True,
        )
        # Un blip transitorio del provider (429/5xx) no debe tirar la tarea:
        # el campo retryable del error deja de ser una promesa vacía al cliente.
        # El plazo agotado NO es un blip: se pasa al siguiente candidato (ver
        # provider_error_from_transport).
        retry_delays = (0.5, 1.0)
        output: ModelOutput | None = None
        invocation_id: str | None = None
        for attempt_index in range(len(retry_delays) + 1):
            # Checkpoint pre-vuelo: cada intento tiene su propia fila de
            # invocación; si el proceso muere con la llamada en el aire, la
            # recuperación la encontrará en 'started' y la tratará como ambigua.
            invocation_id = repository.start_invocation(
                task_id, None, role, model, classify_task_type(request),
                await self._model_loaded_state(model),
                excluded_from_learning=request.exclude_from_model_learning,
                fingerprint=await self._invocation_fingerprint(model),
                compression=self._compression_echo(request),
            )
            try:
                output = await self._run_cancellable(
                    repository,
                    task_id,
                    self.provider.propose(request, model, 1),
                )
                break
            except ProviderError as error:
                self._attach_error_context(error, "generating", model)
                repository.fail_invocation(invocation_id, task_id, error.code, str(error), error.output)
                if (
                    not error.retryable
                    or error.code == "TASK_CANCELLED"
                    or error.details.get("timed_out")
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
        assert invocation_id is not None
        if repository.is_cancel_requested(task_id):
            # La inferencia sí ocurrió: se cierra el checkpoint con su coste
            # real antes de registrar la cancelación.
            repository.complete_invocation(invocation_id, task_id, output)
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return None
        return model, output, invocation_id

    def _finalize_single(
        self, repository, task_id: str, request: TaskCreateRequest,
        model: ModelReference, output: ModelOutput, invocation_id: str,
        embedding_model: dict[str, Any] | None = None,
    ) -> None:
        progress = {
            "phase": TaskStatus.completed.value,
            "invocations_completed": 1,
            "invocations_total": 1,
            "budget_limit_usd": request.model_requirements.max_cost_usd,
            "cost_estimated_usd": None,
            "cost_actual_usd": output.cost_usd,
        }
        result = self._technical_result(request, model, output, embedding_model)
        repository.complete_single_task(
            task_id, invocation_id, model, output, progress=progress, result=result,
        )
        try:
            if output.embeddings is not None:
                # El artefacto final lleva el lote entero y la identidad del
                # modelo: quien lo descargue debe poder saber con qué espacio
                # vectorial se hizo sin cruzarlo con nada más.
                artifact = self.artifacts.write_text(task_id, "single/embeddings.json", dumps_json({
                    "embeddings": [list(vector) for vector in output.embeddings],
                    "embedding_model": embedding_model,
                }))
                artifact_type = "embedding_output"
            elif output.embedding is not None:
                artifact = self.artifacts.write_text(task_id, "single/embedding.json", dumps_json({
                    "embedding": list(output.embedding),
                    "embedding_model": embedding_model,
                }))
                artifact_type = "embedding_output"
            else:
                suffix = "json" if request.output.format.value == "json" else "md"
                artifact = self.artifacts.write_text(task_id, f"single/final.{suffix}", output.content or "")
                artifact_type = "single_output"
            repository.record_artifact(task_id, None, invocation_id, artifact_type, artifact)
            self._record_images_safely(repository, task_id, None, invocation_id, output, "single")
        except Exception as error:
            try:
                repository.add_event(task_id, "artifact.failed", {"message": str(error)})
            except Exception:
                pass

    async def _process_single(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        if request.execution.long_context == "map_reduce":
            handled = await self._process_single_map_reduce(repository, task_id, request)
            if handled:
                return
        inference = await self._single_inference(repository, task_id, request)
        if inference is None:
            return
        model, output, invocation_id = inference
        embedding_model = None
        if request.inference_kind == InferenceKind.embedding:
            embedding_model = await self._embedding_model_identity(model, output)
        self._finalize_single(
            repository, task_id, request, model, output, invocation_id, embedding_model,
        )

    async def _embedding_model_identity(
        self, model: ModelReference, output: ModelOutput,
    ) -> dict[str, Any]:
        """Con qué espacio vectorial se calcularon los vectores (contrato 2.12).

        Mezclar en un índice vectores de dos modelos —o de dos versiones del
        mismo nombre— da búsquedas basura sin ningún error visible. El digest
        es lo que delata que `bge-m3:latest` ya no es el de ayer; sale de la
        misma huella que se guarda con la invocación, no de otra consulta con
        otra verdad."""
        vectors = output.embeddings if output.embeddings is not None else (
            (output.embedding,) if output.embedding is not None else ()
        )
        fingerprint = await self._invocation_fingerprint(model) or {}
        components = fingerprint.get("components") or {}
        digest = (components.get("digest") or {}).get("value")
        return {
            "provider": model.provider,
            "deployment": model.deployment,
            "model": model.model,
            "dimensions": len(vectors[0]) if vectors else 0,
            "digest": digest,
            "fingerprint_hash": fingerprint.get("hash"),
        }

    _CONFIDENCE_JUDGE_PROMPT = (
        "Eres un evaluador de calidad. Se te da una PREGUNTA y una RESPUESTA "
        "candidata. Estima, del 0.0 al 1.0, la probabilidad de que la respuesta "
        "sea correcta, completa y suficiente. Responde SOLO con el número (por "
        "ejemplo 0.8), sin explicaciones.\n\n"
        "PREGUNTA:\n{question}\n\nRESPUESTA:\n{answer}"
    )

    async def _judge_confidence(
        self, repository, task_id: str, request: TaskCreateRequest,
        answer: str,
    ) -> tuple[float, ModelOutput | None]:
        """Puntúa la confianza (0-1) de una respuesta con un modelo juez. Falla
        abierto: si el juez no da un número usable, devuelve 1.0 (no escalar)."""
        judge_prompt = self._CONFIDENCE_JUDGE_PROMPT.format(
            question=request.content.prompt, answer=answer,
        )
        judge_request = request.model_copy(update={
            "content": request.content.model_copy(update={"prompt": judge_prompt}),
            "prompt_compression": "off",
            "execution": request.execution.model_copy(update={
                "strategy": ExecutionStrategy.single, "preset": ExecutionPreset.fast,
            }),
        })
        try:
            model = (await self.provider.select(judge_request, 1, ["arbiter"]))[0]
        except ProviderError:
            return 1.0, None
        invocation_id = repository.start_invocation(
            task_id, None, "confidence_judge", model, classify_task_type(request),
            await self._model_loaded_state(model),
            excluded_from_learning=request.exclude_from_model_learning,
            fingerprint=await self._invocation_fingerprint(model),
            compression=self._compression_echo(judge_request),
        )
        try:
            output = await self._run_cancellable(
                repository, task_id, self.provider.propose(judge_request, model, 1),
            )
            repository.complete_invocation(invocation_id, task_id, output)
        except ProviderError as error:
            repository.fail_invocation(invocation_id, task_id, error.code, str(error), error.output)
            return 1.0, None
        match = re.search(r"(?<![\w.])(0(?:\.\d+)?|1(?:\.0+)?)(?![\w.])", output.content or "")
        if match is None:
            return 1.0, output
        return max(0.0, min(1.0, float(match.group(1)))), output

    async def _process_single_with_escalation(
        self, repository, task_id: str, request: TaskCreateRequest,
    ) -> None:
        """Pieza 2 del meta-router: single primero; si el juez puntúa la
        respuesta por debajo del umbral, escala a mixture."""
        router = self.scheduler.config.strategy_router
        inference = await self._single_inference(repository, task_id, request)
        if inference is None:
            return
        model, output, invocation_id = inference
        if repository.is_cancel_requested(task_id):
            repository.complete_invocation(invocation_id, task_id, output)
            repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
            return

        score, judge_output = await self._judge_confidence(
            repository, task_id, request, output.content or "",
        )
        prior_outputs = [output] + ([judge_output] if judge_output is not None else [])
        repository.add_event(task_id, "strategy.confidence", {
            "score": score,
            "threshold": router.escalation_min_confidence,
            "escalated": score < router.escalation_min_confidence,
        })
        if score >= router.escalation_min_confidence:
            # _finalize_single cierra la invocación single (aún en 'started').
            self._finalize_single(repository, task_id, request, model, output, invocation_id)
            return

        # Escala: se cierra la invocación single (el mixture no la toca) y se
        # deja constancia antes de arrancar el consenso.
        repository.complete_invocation(invocation_id, task_id, output)
        repository.add_event(task_id, "strategy.escalated", {
            "from": "single", "to": "mixture_of_agents", "confidence": score,
        })
        # El coste del single + juez se descuenta del presupuesto del mixture.
        mixture_request = self._with_remaining_budget(request, prior_outputs).model_copy(update={
            "execution": request.execution.model_copy(update={
                "strategy": ExecutionStrategy.mixture_of_agents, "preset": ExecutionPreset.fast,
                "scheduling": SchedulingPolicy.sequential,
                "selection": request.execution.selection.model_copy(update={"mode": SelectionMode.auto}),
            }),
        })
        await self._process_consensus(repository, task_id, mixture_request)
