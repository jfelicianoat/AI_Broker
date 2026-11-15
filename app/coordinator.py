from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.artifacts import ArtifactStore
from app.coordinator_agent import AgentMixin
from app.coordinator_consensus import ConsensusMixin
from app.coordinator_map_reduce import MapReduceMixin
from app.coordinator_single import SingleMixin
from app.db import Database, dumps_json
from app.providers import BootstrapModelProvider, ModelOutput, ProviderError
from app.providers.routing import remote_only_scope
from app.repository import _utc_now_iso
from app.resource_scheduler import (
    ResourceScheduler,
)
from app.schemas import (
    ExecutionPreset,
    ExecutionStrategy,
    InferenceKind,
    ModelReference,
    SchedulingPolicy,
    SelectionMode,
    TaskCreateRequest,
    TaskKind,
    TaskStatus,
    is_local_deployment,
    run_code_available,
)
from app.shadow_probe import ShadowProbe
from app.strategy_router import (
    RoutingDecision,
    classify_request,
    recommend_from_cases,
    signal_bucket,
)
from app.task_classifier import classify_task_type, task_type_scope

logger = logging.getLogger("ai_broker.coordinator")

# Estados de los que ya no se sale: la tarea no volverá a pedir turno.
_TERMINAL_STATUSES = frozenset({
    TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled,
})

# Errores de un proveedor caído o reiniciándose, no de la tarea: con
# retryable=true la tarea vuelve a la cola con espera en vez de fallar.
_OUTAGE_ERRORS = frozenset({
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_ENGINE_CRASHED",
    "MODEL_LOAD_TIMEOUT",
    "MODEL_NOT_LOADED",
    "MODEL_UNAVAILABLE",
    # Los cloud gratuitos limitan la frecuencia; al rato vuelven a aceptar.
    "RATE_LIMITED",
})


class ConsensusCoordinator(SingleMixin, ConsensusMixin, MapReduceMixin, AgentMixin):
    algorithm_version = "fase-5-fast-slow-consensus-v2"

    def __init__(
        self,
        db: Database,
        scheduler: ResourceScheduler,
        artifacts: ArtifactStore | None = None,
        provider: Any | None = None,
        ingestion: Any | None = None,
        sandbox: Any | None = None,
        mcp: Any | None = None,
    ) -> None:
        self.db = db
        self.scheduler = scheduler
        self.artifacts = artifacts or ArtifactStore()
        self.provider = provider or BootstrapModelProvider()
        # Registro MCP opcional: herramientas de servidores de terceros para el
        # bucle agéntico. None = el broker corre sin MCP, como hasta ahora.
        self.mcp = mcp
        # IngestionService opcional: expande los adjuntos broker_file a Markdown
        # dentro del prompt en el momento del despacho.
        self.ingestion = ingestion
        # SandboxExecutor opcional: habilita la skill run_code del agente.
        self.sandbox = sandbox
        # Sondeo en sombra: mide aspirantes en segundo plano cuando la tarea ya
        # ha terminado. Gobernado por config.shadow_probe (off por defecto).
        self.shadow_probe = ShadowProbe(self.provider, scheduler.config)

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

    @staticmethod
    def _dependency_warnings(repository, task_id: str) -> list[str]:
        """Avisos en texto de las dependencias que no se cumplieron.

        Se reconstruyen de los eventos en vez de acarrearse por la ejecución:
        los eventos ya son el registro persistente de lo que pasó, y la espera
        ocurre mucho antes de que exista un resultado donde escribirlos.
        """
        warnings: list[str] = []
        for payload in repository.event_payloads(task_id, "task.dependency_unknown"):
            tasks = payload.get("tasks") or []
            if tasks:
                warnings.append(
                    "dependencias inexistentes, no se esperó por ellas: " + ", ".join(tasks)
                )
        for payload in repository.event_payloads(task_id, "task.dependency_timeout"):
            parts = []
            if payload.get("tasks"):
                parts.append(f"{len(payload['tasks'])} tarea(s) sin terminar")
            if payload.get("group_pending"):
                parts.append(f"{payload['group_pending']} del grupo «{payload.get('group')}»")
            detail = " y ".join(parts) or "dependencias sin cumplir"
            warnings.append(
                f"se ejecutó sin esperar a sus dependencias ({detail}) tras "
                f"{payload.get('waited_seconds')} s de espera: la respuesta puede "
                "estar construida sobre trabajo incompleto"
            )
        return warnings

    def _await_dependencies(self, repository, task_id: str, request: TaskCreateRequest) -> bool:
        """¿Puede correr ya? False = devuelta a la cola esperando a otras tareas.

        La espera tiene fecha de caducidad: `timeout_seconds` de la propia
        tarea. Una tarea no puede esperar por sus dependencias más de lo que se
        le permite tardar, y sin tope una dependencia que nunca termina —un id
        de una tarea que quedó colgada, un grupo que nadie drena— la dejaría en
        la cola para siempre.

        Al caducar NO falla: sigue adelante con un aviso. Es la política que se
        eligió para este mecanismo (la dependencia ordena, no condiciona), y por
        eso el aviso viaja al resultado además de al evento — una respuesta
        construida sobre un índice a medias es plausible y no se distingue de
        una buena mirándola.
        """
        if not request.depends_on and not request.depends_on_group:
            return True
        pending = repository.pending_dependencies(
            task_id, list(request.depends_on), request.depends_on_group,
        )
        unknown = pending.pop("unknown_tasks", None)
        if unknown:
            repository.add_event(task_id, "task.dependency_unknown", {"tasks": unknown})
        if not pending:
            repository.clear_dependency_wait(task_id)
            return True

        state = repository.dependency_wait_state(task_id)
        waiting_since = state.get("waiting_since")
        elapsed = 0.0
        if waiting_since:
            try:
                started = datetime.fromisoformat(str(waiting_since))
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            except ValueError:
                elapsed = 0.0
        if elapsed >= request.execution.timeout_seconds:
            repository.add_event(task_id, "task.dependency_timeout", {
                **pending, "waited_seconds": round(elapsed, 1),
                "timeout_seconds": request.execution.timeout_seconds,
            })
            repository.clear_dependency_wait(task_id)
            return True

        try:
            repository.defer_task_for_dependencies(
                task_id, pending,
                wait_seconds=self.scheduler.config.processing.dependency_wait_seconds,
            )
        except Exception:
            # Mismo criterio que el aplazamiento por memoria: si el aplazamiento
            # falla, se sigue por el camino normal. Lo intolerable sería dejar
            # la tarea en un limbo sin estado.
            logger.warning("task.dependency_defer_failed", exc_info=True, extra={
                "event": "task.dependency_defer_failed", "task_id": task_id,
            })
            return True
        logger.info("task.waiting_for_dependencies", extra={
            "event": "task.waiting_for_dependencies", "task_id": task_id,
        })
        return False

    async def _vision_available(self, request: TaskCreateRequest) -> bool:
        """¿Hay algún modelo que pueda mirar las imágenes de esta petición?

        Best-effort: si el proveedor no sabe responder (bootstrap, dobles de
        test) se asume que sí. Equivocarse por optimismo aquí no rompe nada —la
        imagen viaja como imagen y, si de verdad no hay quien la vea, el
        enrutado corta con `VISION_MODEL_UNAVAILABLE`, que es el mensaje
        correcto. Equivocarse por pesimismo, en cambio, degradaría a OCR una
        tarea que un modelo con visión podía atender bien.
        """
        checker = getattr(self.provider, "has_vision_model", None)
        if checker is None:
            return True
        try:
            return bool(await checker(request))
        except Exception:  # noqa: BLE001 - la duda se resuelve a favor de la imagen
            logger.warning(
                "task.vision_probe_failed",
                extra={"event": "task.vision_probe_failed"},
            )
            return True

    async def process_task(self, repository, task_id: str) -> None:
        cloud_lane = repository.get_task_kind(task_id) == TaskKind.cloud
        # Primero de todo: echar de la máquina a los sondeos en sombra. Son
        # trabajo opcional y su modelo puede estar ocupando la VRAM que esta
        # tarea necesita, con un lease que impide desalojarlo. Una tarea del
        # carril cloud no necesita VRAM: echarlos sería tirar su medida.
        if not cloud_lane:
            try:
                await self.shadow_probe.yield_to_real_work()
            except Exception:  # noqa: BLE001 - ceder es best-effort, nunca bloquea
                logger.warning("shadow_probe.preempt_failed", extra={
                    "event": "shadow_probe.preempt_failed", "task_id": task_id,
                })
        request = repository.get_task_request(task_id)
        # Antes de gastar nada: si la tarea declara dependencias y siguen sin
        # cumplirse, vuelve a la cola sin haber tocado un modelo.
        if not self._await_dependencies(repository, task_id, request):
            return
        if self.ingestion is not None and request.content.attachments:
            try:
                request = self.ingestion.apply_attachment_policy(
                    request, egress_server_ids=self.mcp.egress_server_ids() if self.mcp is not None else [],
                )
                # Con imágenes delante hay que saber si alguien puede mirarlas
                # ANTES de expandir: de eso depende que la imagen viaje como
                # imagen o como el texto que se le reconozca.
                vision_available = True
                if self.ingestion.has_image_attachments(request):
                    vision_available = await self._vision_available(request)
                # En un hilo: la expansión lee de disco el Markdown de cada adjunto
                # y, en el peor caso, corre un OCR.
                request = await asyncio.to_thread(
                    self.ingestion.expand_request,
                    request,
                    vision_available=vision_available,
                )
            except Exception as error:
                code = getattr(error, "code", "ATTACHMENT_EXPANSION_FAILED")
                repository.update_task(
                    task_id,
                    TaskStatus.failed,
                    progress={"phase": TaskStatus.failed.value},
                    error={"code": code, "message": str(error), "retryable": False},
                    clear_queue_position=True,
                )
                return
        effective_timeout = min(
            request.execution.timeout_seconds,
            self.scheduler.config.processing.task_timeout_seconds,
        )
        try:
            # Desde 3.12 wait_for corre en esta misma tarea: el ámbito se
            # restaura al salir para que no se cuele en la siguiente.
            with task_type_scope(classify_task_type(request)), (
                remote_only_scope() if cloud_lane else nullcontext()
            ):
                await asyncio.wait_for(
                    self._process_task_request(repository, task_id, request),
                    timeout=effective_timeout,
                )
        except asyncio.TimeoutError:
            if repository.is_cancel_requested(task_id):
                repository.update_task(task_id, TaskStatus.cancelled, clear_queue_position=True)
                return
            # Dónde se quedó. Un timeout a secas no distingue el caso más común
            # —un modelo local grande que tarda en cargar desde disco— de una
            # tarea que sí estaba generando: son dos problemas distintos y la
            # salida de cada uno también.
            stalled = self._timeout_context(repository, task_id)
            # Qué mando cortó la tarea. Decir "súbele el timeout" sin nombrar
            # cuál de los dos manda mandaba a tocar el que no era: el plazo
            # efectivo es el menor, y subir el otro no cambia nada.
            cap = self.scheduler.config.processing.task_timeout_seconds
            pedido = request.execution.timeout_seconds
            binding = (
                f"processing.task_timeout_seconds ({cap} s), el techo del broker"
                if cap <= pedido else
                f"execution.timeout_seconds de la petición ({pedido} s), cuyo valor "
                "cuando no se envía es processing.default_task_timeout_seconds"
            )
            detail = stalled["detail"]
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={"phase": TaskStatus.failed.value, "timeout_seconds": effective_timeout},
                error={
                    "code": "TASK_TIMEOUT",
                    "message": (
                        f"La tarea superó el timeout efectivo de {effective_timeout} segundos"
                        f"{detail if detail.endswith('.') else detail + '.'} "
                        f"El plazo lo fijó {binding}: súbelo o usa un modelo menor."
                    ),
                    "retryable": True,
                    "timeout_bound_by": binding,
                    **stalled["context"],
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
            if cloud_lane and self._demote_from_cloud_lane(repository, task_id, error):
                return
            if error.code in {"VRAM_INSUFFICIENT", "LOCAL_MODEL_SLOTS_BUSY"}:
                # No cabe AHORA, pero cabría en la máquina vacía: no es un
                # fallo. Cede el turno conservando su sitio en la cola y deja
                # pasar a quien sí quepa. VRAM_MODEL_TOO_LARGE no entra aquí a
                # propósito: ese no cabe nunca y esperar sería engañar.
                # `LOCAL_MODEL_SLOTS_BUSY` es el mismo caso con otro motivo: el
                # cupo de modelos cargados, no los bytes.
                if self._defer_for_memory(repository, task_id, error):
                    return
            if self._retry_after_outage(repository, task_id, error):
                return
            current = repository.get_task(task_id)
            context = self._error_context(error, current.progress)
            stage = context.get("stage")
            if stage in {"resource_planning", "proposing", "evaluating", "synthesizing"}:
                # Deja constancia en el checkpoint de etapa de dónde murió el run.
                repository.set_stage_status(
                    task_id, repository.get_consensus_run_id(task_id), str(stage), "failed"
                )
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
        finally:
            # Lo que siga abierto aquí ya no lo cerrará nadie: el trabajo de
            # esta pasada ha terminado, con éxito, fallo, timeout o reintento.
            try:
                repository.abandon_open_invocations(task_id)
            except Exception:
                logger.warning("task.abandon_invocations_failed", extra={"task_id": task_id})
            # Una tarea que ya terminó no arrastra su historial de espera: los
            # contadores solo tienen sentido mientras sigue peleando por un
            # turno. (En `waiting_for_memory` NO se limpia: ahí es donde vive.)
            try:
                if repository.get_task(task_id).status in _TERMINAL_STATUSES:
                    repository.clear_memory_wait(task_id)
            except Exception:
                logger.warning("task.clear_memory_wait_failed", extra={"task_id": task_id})
            # Los avisos de dependencias se cuelgan del resultado aquí, ya
            # redactado por la estrategia que tocara: quien sabe del aviso corre
            # antes de que exista el resultado, y quien escribe el resultado no
            # sabe del aviso.
            try:
                repository.append_result_warnings(
                    task_id, self._dependency_warnings(repository, task_id),
                )
            except Exception:
                logger.warning("task.dependency_warning_failed", extra={"task_id": task_id})
            # Registra el caso de enrutamiento (para el aprendizaje) sea cual sea
            # el desenlace; el helper filtra los estados no terminales.
            try:
                self._maybe_record_routing_case(repository, task_id, request)
            except Exception:
                logger.warning("routing_case.record_failed", extra={"task_id": task_id})
            # Sondeo en sombra: se encola aquí, con la tarea ya terminada, para
            # que la medida de un aspirante no se interponga jamás entre el
            # usuario y su respuesta. Solo tras un éxito: con un prompt que
            # acaba de fallar, el aspirante fallaría por un defecto ajeno.
            try:
                if repository.get_task(task_id).status == TaskStatus.completed:
                    self.shadow_probe.schedule(repository, task_id, request)
            except Exception:
                logger.warning("shadow_probe.schedule_failed", extra={"task_id": task_id})

    def _demote_from_cloud_lane(self, repository, task_id: str, error: ProviderError) -> bool:
        """True si la tarea pasa a la cola local porque ningún remoto la atendió.

        Antes del carril cloud, «cloud primero» dejaba los locales como relevo
        dentro de la misma lista. Aquí el relevo es la cola local entera: la
        tarea no puede caer a un local sin pedir el turno de la GPU.

        Lo que un local tampoco arreglaría falla aquí, como antes: el
        presupuesto agotado sigue agotado y una cancelación es una orden.
        Las invocaciones remotas fallidas ya están registradas, así que el
        ranking aprende de ellas igual que si la tarea hubiera fallado.
        """
        if error.code in {"TASK_CANCELLED", "BUDGET_EXCEEDED"}:
            return False
        try:
            demoted = repository.demote_to_inference_lane(
                task_id, {"code": error.code, "message": str(error)},
            )
        except Exception:  # noqa: BLE001 - si no se puede bajar, falla como antes
            logger.warning("task.lane_demote_failed", exc_info=True, extra={
                "event": "task.lane_demote_failed", "task_id": task_id,
            })
            return False
        if demoted:
            logger.warning("task.lane_demoted", extra={
                "event": "task.lane_demoted", "task_id": task_id, "code": error.code,
            })
        return demoted

    def _retry_after_outage(self, repository, task_id: str, error: ProviderError) -> bool:
        """True si la tarea vuelve a la cola porque cayó el proveedor, no ella."""
        if error.code not in _OUTAGE_ERRORS or not error.retryable:
            return False
        processing = self.scheduler.config.processing
        state = repository.schedule_outage_retry(
            task_id,
            {"code": error.code, "message": str(error)},
            base_wait_seconds=processing.outage_retry_seconds,
            max_attempts=processing.max_task_attempts,
        )
        if state is None:
            return False
        logger.warning(
            "task.retry_scheduled",
            extra={"event": "task.retry_scheduled", "task_id": task_id, "code": error.code},
        )
        return True

    def _defer_for_memory(self, repository, task_id: str, error: ProviderError) -> bool:
        """Devuelve la tarea a la cola en espera de memoria.

        Devuelve si el aplazamiento salió adelante: si algo falla aquí se sigue
        por el camino normal y la tarea acaba en `failed`, que es peor pero
        honesto — lo intolerable sería dejarla en un limbo sin estado."""
        resources = self.scheduler.config.resources
        block = dict(getattr(error, "memory_block", None) or {"message": str(error)})
        try:
            state = repository.defer_task_for_memory(
                task_id,
                block,
                wait_seconds=resources.memory_wait_seconds,
                reserve_after=resources.memory_reserve_after,
                reserve_window_seconds=resources.memory_reserve_window_seconds,
            )
        except Exception:
            logger.warning("task.defer_failed", exc_info=True, extra={
                "event": "task.defer_failed", "task_id": task_id,
            })
            return False
        logger.info("task.waiting_for_memory", extra={
            "event": "task.waiting_for_memory",
            "task_id": task_id,
            "deferrals": state.get("deferrals"),
            "reserved": bool(state.get("reserved_until")),
            "model": block.get("model"),
        })
        return True

    def _maybe_record_routing_case(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        """Guarda un caso (bucket → decisión → resultado) al terminar una tarea
        auto-enrutada, para alimentar el aprendizaje adaptativo (pieza 3)."""
        router = self.scheduler.config.strategy_router
        if request.execution.strategy != ExecutionStrategy.auto or not router.record_cases:
            return
        routed = repository.latest_event_payload(task_id, "strategy.routed")
        if routed is None:
            return
        status = repository.get_task(task_id).status.value
        if status not in {TaskStatus.completed.value, TaskStatus.failed.value}:
            return  # cancelada o en espera: no es señal de aprendizaje
        signals = routed.get("signals") or {}
        chosen = str(routed.get("chosen_strategy") or "single")
        escalated = repository.has_event(task_id, "strategy.escalated")
        final_strategy = "mixture_of_agents" if escalated else chosen
        cost, latency = repository.task_cost_and_latency(task_id)
        repository.record_routing_case(
            task_id, signal_bucket(signals), chosen, final_strategy,
            escalated, status, cost, latency,
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
        escalate = False
        if request.execution.strategy == ExecutionStrategy.auto:
            request, escalate = self._resolve_auto_strategy(repository, task_id, request)
        # A esta altura la estrategia ya está resuelta (nunca "auto"): un
        # adjunto tabular sin la skill run_code disponible nunca podría
        # procesarse, y sin este chequeo la tarea llegaría a expand_request/
        # routing y fallaría más tarde con CONTEXT_LIMIT_EXCEEDED, un error
        # que no explica la causa real (ver app.ingestion.detection.TABULAR_EXTENSIONS).
        if self.ingestion is not None and self.ingestion.has_tabular_attachments(request) \
                and not run_code_available(request.execution):
            repository.update_task(
                task_id,
                TaskStatus.failed,
                progress={"phase": TaskStatus.failed.value},
                error={
                    "code": "TABULAR_ATTACHMENT_REQUIRES_SANDBOX",
                    "message": (
                        "Esta tarea tiene un adjunto tabular (CSV/TSV/XLSX): su contenido no "
                        "se inyecta en el prompt, así que necesita la skill run_code "
                        "(estrategia agent con run_code en execution.agent.skills, o "
                        "mixture_of_agents con run_code en execution.proposer_skills) "
                        "para poder procesarlo."
                    ),
                    "retryable": False,
                },
                clear_queue_position=True,
            )
            return
        self._record_compressed_prompt(repository, task_id, request)
        if escalate and request.execution.strategy == ExecutionStrategy.single:
            await self._process_single_with_escalation(repository, task_id, request)
            return
        if request.execution.strategy == ExecutionStrategy.single:
            await self._process_single(repository, task_id, request)
            return
        if request.execution.strategy == ExecutionStrategy.agent:
            await self._process_agent(repository, task_id, request)
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

    def _record_compressed_prompt(self, repository, task_id: str, request: TaskCreateRequest) -> None:
        """Deja constancia del prompt que viajará a los modelos cuando la
        compresión lo altera: el detalle de la tarea muestra original y comprimido.

        Se persiste en el momento de ejecutar (no al renderizar) para que el
        detalle refleje lo que viajó aunque la config de compresión cambie después.
        """
        if request.inference_kind == InferenceKind.embedding:
            return
        # user_prompt es la misma función que usa la inferencia (incluye el
        # override por tarea): lo registrado coincide con lo que viaja.
        prompt_fn = getattr(self.provider, "user_prompt", None)
        if prompt_fn is None:
            return
        try:
            compressed = prompt_fn(request)
        except Exception:
            return
        if not compressed or compressed == request.content.prompt:
            return
        try:
            repository.add_event(task_id, "prompt.compressed", {
                "text": compressed,
                "original_chars": len(request.content.prompt),
                "compressed_chars": len(compressed),
            })
        except Exception:
            # El registro es informativo: nunca debe tirar la tarea.
            logger.warning("task.compressed_prompt_event_failed", extra={"task_id": task_id})

    def _resolve_auto_strategy(
        self, repository, task_id: str, request: TaskCreateRequest,
    ) -> tuple[TaskCreateRequest, bool]:
        """Traduce `strategy: auto` a una estrategia concreta según el meta-router
        y deja constancia del caso (señales + decisión) para trazabilidad y para
        el aprendizaje futuro (pieza 3). El segundo valor indica si, tratándose de
        single, debe aplicarse el escalado por confianza (pieza 2)."""
        router = self.scheduler.config.strategy_router
        if not router.enabled:
            decision = RoutingDecision("single", ["meta-router desactivado: se usa single"], {})
        elif router.heuristic_classifier:
            decision = classify_request(request, router)
        else:
            decision = RoutingDecision("single", ["sin clasificador activo: se usa single"], {})

        # Pieza 3: la evidencia de casos previos del mismo tipo puede cambiar la
        # decisión heurística (p. ej. si el single suele escalar aquí).
        if router.enabled and router.adaptive_learning and decision.signals:
            bucket = signal_bucket(decision.signals)
            cases = repository.routing_cases_for_bucket(bucket)
            recommendation = recommend_from_cases(
                decision.strategy, cases,
                min_cases=router.learning_min_cases,
                escalation_threshold=router.learning_escalation_threshold,
                failure_threshold=router.learning_failure_threshold,
            )
            if recommendation is not None:
                learned_strategy, reason = recommendation
                decision = RoutingDecision(
                    learned_strategy, [reason, *decision.reasons], decision.signals, learned=True,
                )

        chosen = decision.strategy
        if chosen == "agent":
            new_execution = request.execution.model_copy(update={
                "strategy": ExecutionStrategy.agent, "preset": ExecutionPreset.fast,
                "scheduling": SchedulingPolicy.sequential,
            })
        elif chosen == "mixture_of_agents":
            new_execution = request.execution.model_copy(update={
                "strategy": ExecutionStrategy.mixture_of_agents, "preset": ExecutionPreset.fast,
                "scheduling": SchedulingPolicy.sequential,
                "selection": request.execution.selection.model_copy(update={"mode": SelectionMode.auto}),
            })
        else:
            new_execution = request.execution.model_copy(update={
                "strategy": ExecutionStrategy.single, "preset": ExecutionPreset.fast,
                "scheduling": SchedulingPolicy.sequential,
            })

        if router.record_cases:
            repository.add_event(task_id, "strategy.routed", {
                "chosen_strategy": chosen,
                "reasons": decision.reasons,
                "signals": decision.signals,
                "router_enabled": router.enabled,
                "learned": decision.learned,
            })
        logger.info(
            "strategy.routed",
            extra={"event": "strategy.routed", "task_id": task_id, "chosen_strategy": chosen},
        )
        escalate = (
            chosen == "single"
            and router.enabled
            and router.confidence_escalation
            and request.inference_kind != InferenceKind.embedding
        )
        return request.model_copy(update={"execution": new_execution}), escalate


    # ------------------------------------------------ map-reduce de contexto largo


    def _yield_check(
        self, repository, task_id: str, request: TaskCreateRequest, strategy: ExecutionStrategy,
    ) -> Callable[[], str | None] | None:
        """Pregunta que hace una tarea larga entre pasos: ¿espera algo más
        urgente que yo? Devuelve None si esta tarea no debe ceder nunca.

        Solo cede quien pidió su estrategia de forma explícita. Una `auto` se
        vuelve a enrutar en cada tramo, el aprendizaje puede cambiar la decisión
        entre uno y otro, y el estado congelado se perdería en una estrategia
        que no sabe retomarlo.
        """
        if not self.scheduler.config.processing.yield_to_urgent_tasks:
            return None
        if repository.get_task_request(task_id).execution.strategy != strategy:
            return None

        def check() -> str | None:
            if repository.is_cancel_requested(task_id):
                return None
            return repository.urgent_task_waiting(request.priority, exclude_id=task_id)

        return check


    def _compression_echo(self, request: TaskCreateRequest) -> dict[str, str] | None:
        """Eco {requested, effective} de la compresión de ESTA invocación.

        Se captura al abrir el checkpoint, igual que la huella de ejecución, y
        con la petición que de verdad va a viajar: el broker fuerza `off` en las
        invocaciones que procesan contenido generado (fragmentos de map-reduce,
        síntesis de segunda ronda, juez de confianza), así que el valor de la
        tarea no describe lo que le pasó a cada llamada.

        None cuando el proveedor no declara compresión —el bootstrap de los
        tests—: es preferible dejar el campo vacío a inventar un eco.
        """
        echo = getattr(self.provider, "compression_echo", None)
        if echo is None:
            return None
        try:
            return echo(request)
        except Exception:
            return None

    def provider_user_prompt(self, request: TaskCreateRequest) -> str:
        """Prompt del usuario aplicando compresión si el proveedor la ofrece."""
        prompt_fn = getattr(self.provider, "user_prompt", None)
        if prompt_fn is None:
            return request.content.prompt
        try:
            return str(prompt_fn(request))
        except Exception:
            return request.content.prompt


    @staticmethod
    def _model_identity(model: ModelReference) -> tuple[str, str, str]:
        return (model.provider.lower(), model.deployment.lower(), model.model)


    def _record_images_safely(
        self,
        repository,
        task_id: str,
        run_id: str | None,
        invocation_id: str | None,
        output: ModelOutput,
        folder: str,
    ) -> None:
        """Guarda como artefactos las imágenes que devolvió el modelo.

        No van en el resultado JSON a propósito: ese documento se lee entero en
        cada consulta del estado de la tarea, y un PNG en base64 dentro lo
        convertiría en megabytes por lectura. Como artefacto se descarga cuando
        alguien lo pide, que es como se sirve el resto de la salida.
        """
        for ordinal, image in enumerate(output.images, start=1):
            self._record_artifact_safely(
                repository, task_id, run_id, invocation_id, "image_output",
                lambda o=ordinal, img=image, f=folder: self.artifacts.write_bytes(
                    task_id, f"{f}/image_{o:02d}{img.extension}",
                    base64.b64decode(img.data_base64),
                ),
            )

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
        spent = sum(output.cost_usd for output in outputs)
        remaining = maximum - spent
        # Un techo de 0 USD no es presupuesto agotado: significa "solo modelos
        # gratuitos". Mientras no se haya gastado nada quedan invocaciones
        # posibles (locales), así que solo se corta habiendo gasto real.
        if spent > 0 and remaining <= 0:
            raise ProviderError("BUDGET_EXCEEDED", "No queda presupuesto para otra invocación")
        requirements = request.model_requirements.model_copy(update={"max_cost_usd": remaining})
        return request.model_copy(update={"model_requirements": requirements})

    async def _run_cancellable(self, repository, task_id: str, operation) -> ModelOutput:
        return await self._run_cancellable_turn(repository, task_id, operation)

    async def _run_cancellable_turn(self, repository, task_id: str, operation: Any) -> Any:
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
        embedding_model: dict[str, Any] | None = None,
    ) -> dict:
        result: dict[str, Any] = {
            "inference_kind": request.inference_kind.value,
            "output_format": request.output.format.value,
            "usage": self._usage([output]),
            "model_used": model.model_dump(mode="json"),
            "models_used": [model.model_dump(mode="json")],
            "fallback_used": self._fallback_used(request, model),
        }
        if request.inference_kind == InferenceKind.embedding and output.embeddings is not None:
            result["embeddings"] = [list(vector) for vector in output.embeddings]
            result["dimensions"] = len(output.embeddings[0]) if output.embeddings else 0
            result["embedding_model"] = embedding_model
        elif request.inference_kind == InferenceKind.embedding:
            result["embedding"] = list(output.embedding or ())
            result["dimensions"] = len(output.embedding or ())
            result["embedding_model"] = embedding_model
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

    async def _model_loaded_state(self, model: ModelReference) -> bool | None:
        """¿Estaba el modelo en VRAM justo antes de invocarlo?

        Se registra en la invocación para poder separar después la latencia en
        caliente de la latencia en frío (app.model_timing): un modelo local que
        hay que cargar desde disco paga esa carga dentro de su propia latencia,
        y mezclar ambos casos en una media da un número que no describe
        ninguna de las dos situaciones. None = no aplica (cloud no carga nada)
        o no se pudo consultar; nunca se inventa el dato."""
        if not is_local_deployment(model.deployment):
            return None
        loaded_models = getattr(self.provider, "loaded_local_models", None)
        if loaded_models is None:
            return None
        try:
            loaded = await loaded_models()
        except Exception:
            return None
        return None if loaded is None else model.model in loaded

    def _timeout_context(self, repository, task_id: str) -> dict[str, Any]:
        """Qué estaba haciendo la tarea cuando venció el plazo.

        Se lee de la última invocación abierta: si quedó en 'started', el
        modelo nunca respondió —cargando o generando— y decir cuál era es la
        diferencia entre «algo tardó» y «este modelo tarda demasiado en esta
        máquina». Best-effort: un problema leyendo esto no puede impedir que la
        tarea se cierre."""
        try:
            invocations = repository.list_task_invocations(task_id).items
        except Exception:
            return {"detail": "", "context": {}}
        pendiente = next(
            (item for item in reversed(invocations) if item.status == "started"), None,
        )
        if pendiente is None:
            if not invocations:
                return {
                    "detail": ". No llegó a invocar a ningún modelo: se agotó antes, "
                              "seleccionando o esperando recursos.",
                    "context": {"stalled_at": "before_first_invocation"},
                }
            return {"detail": "", "context": {}}
        modelo = pendiente.model
        identidad = f"{modelo.provider}/{modelo.deployment}/{modelo.model}"
        return {
            "detail": (
                f". Se quedó esperando a {identidad} en el rol '{pendiente.role}', que nunca "
                "respondió. En un modelo local grande, lo habitual es que siga cargando desde "
                "disco."
            ),
            "context": {
                "stalled_at": "invocation",
                "stalled_model": identidad,
                "stalled_role": pendiente.role,
                "stalled_invocation_id": pendiente.invocation_id,
            },
        }

    async def _invocation_fingerprint(self, model: ModelReference) -> dict[str, Any] | None:
        """Configuración con la que se va a servir esta invocación.

        Se captura ANTES de invocar y se persiste con la fila, no se deduce
        después del catálogo: si el proveedor cambia a mitad de una tirada
        larga, las invocaciones de antes y las de después tienen que quedar
        distinguibles (ver app.execution_fingerprint).

        None es un resultado aceptable —un proveedor que no aporta nada, el
        fake de pruebas— y nunca es motivo para que falle una invocación."""
        loader = getattr(self.provider, "execution_fingerprint", None)
        if loader is None:
            return None
        try:
            return await loader(model)
        except Exception:
            return None

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
