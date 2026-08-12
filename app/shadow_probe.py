"""Sondeo en sombra: medir modelos sin evidencia sin que nadie espere.

El problema que resuelve: con un catálogo grande, casi ningún modelo llega al
mínimo de invocaciones que necesita para que su tiempo estimado sea creíble
(app.model_timing). Medirlos sirviendo peticiones reales significa hacer
esperar al usuario con un modelo del que no se sabe nada, que es exactamente
lo que se quiere evitar.

Aquí se separan las dos cosas que hasta ahora eran el mismo acto: **responder**
y **aprender**. La respuesta la da el modelo elegido; en paralelo, cuando la
tarea ya ha terminado, se invoca a UN aspirante con el mismo prompt solo para
cronometrarlo. Su salida se descarta. Nadie ha esperado por ella.

Reglas de la casa:

- **Un aspirante cada vez**, hasta completar su evidencia. Repartir sondeos
  entre decenas de modelos deja a todos con dos invocaciones sueltas, que no
  sirven para nada.
- **La sombra hereda la frontera de datos de la tarea que la origina.** Los
  candidatos salen de la misma cadena de elegibilidad que la selección real
  (RoutedModelProvider.eligible_catalog), así que una tarea confidencial solo
  puede sondear modelos locales. Sin esto, el sondeo sería una fuga de datos
  con otro nombre.
- **A los modelos locales solo se les sondea con la máquina ociosa**, porque
  compiten por la misma VRAM que las respuestas de verdad. Los cloud no
  consumen recursos locales y se sondean siempre que la clasificación lo
  permita.
- **Solo se sondea tras una tarea completada.** Con un prompt que acaba de
  fallar, el aspirante fallaría también y quedaría marcado como poco fiable
  por un defecto que no es suyo.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import BrokerConfig
from app.model_stats import ModelKey, ModelStats
from app.repository import TaskRepository
from app.schemas import (
    ExecutionPreset,
    ExecutionStrategy,
    InferenceKind,
    ModelReference,
    TaskCreateRequest,
    is_local_deployment,
)
from app.task_classifier import classify_task_type

logger = logging.getLogger("ai_broker.shadow_probe")

SHADOW_ROLE = "shadow_probe"


def choose_aspirant(
    candidates: list[dict[str, Any]],
    stats: dict[ModelKey, ModelStats],
    task_type: str,
    *,
    min_invocations: int,
    allow_local: bool,
) -> dict[str, Any] | None:
    """El siguiente modelo a medir, o None si no hay ninguno que toque.

    Se remata primero al que ya tiene medidas a medias y solo entonces se
    estrena a otro: así cada modelo alcanza evidencia utilizable en unos pocos
    sondeos en vez de quedarse todos a medio medir para siempre."""
    pending: list[tuple[int, dict[str, Any]]] = []
    for entry in candidates:
        if not allow_local and is_local_deployment(entry.get("deployment")):
            continue
        key = (
            str(entry.get("provider") or "").lower(),
            str(entry.get("deployment") or "").lower(),
            str(entry.get("name") or "").lower(),
            task_type,
        )
        stat = stats.get(key)
        attempts = stat.attempts if stat is not None else 0
        if attempts < min_invocations:
            pending.append((attempts, entry))
    if not pending:
        return None
    started = [item for item in pending if item[0] > 0]
    if started:
        # El más avanzado: es el que menos sondeos necesita para terminar.
        return max(started, key=lambda item: item[0])[1]
    # Ninguno empezado: se estrena el primero del catálogo, de forma
    # determinista, y el barrido avanza modelo a modelo.
    return pending[0][1]


class ShadowProbe:
    """Lanza y contabiliza los sondeos. Vive junto al coordinador."""

    def __init__(self, provider: Any, config: BrokerConfig) -> None:
        self.provider = provider
        self.config = config
        self._jobs: set[asyncio.Task] = set()

    @property
    def settings(self) -> Any:
        """Leído en vivo: el panel de Configuración aplica sin reiniciar."""
        return self.config.shadow_probe

    def schedule(
        self, repository: TaskRepository, task_id: str, request: TaskCreateRequest,
    ) -> None:
        """Encola el sondeo sin bloquear al llamante.

        El coordinador llama aquí cuando la tarea ya ha terminado: el sondeo no
        forma parte de la respuesta y no debe retrasar ni su entrega ni el
        despacho de la siguiente tarea."""
        if not self._is_probeable(request):
            return
        job = asyncio.create_task(self._run(repository, task_id, request))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def shutdown(self) -> None:
        """Al cerrar se cancelan: una medida a medias no vale nada y no merece
        retrasar el apagado."""
        for job in list(self._jobs):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)

    async def yield_to_real_work(self) -> bool:
        """Aborta los sondeos en vuelo porque llega trabajo de verdad.

        El sondeo comprueba que la máquina está ociosa ANTES de arrancar, pero
        esa comprobación no cubre lo que pase después: una tarea que entra a
        mitad de un sondeo se encuentra la VRAM ocupada por un modelo que
        además está arrendado, así que el desalojo por capacidad no puede
        tocarlo y la tarea muere con VRAM_INSUFFICIENT. Un aspirante de 48 GB
        tumbaba así una petición de 13 GB en una máquina de 62 GB.

        Cancelar no cuesta nada: la salida del sondeo se descarta siempre. Al
        soltarse el lease, el modelo deja de estar protegido y el desalojo
        normal puede descargarlo. La invocación queda en 'started' a
        propósito — una medida interrumpida no es evidencia, ni a favor ni en
        contra del aspirante, y ese estado ya está excluido de las
        estadísticas (app.model_stats).

        Devuelve si había algo que cancelar, para que quien llame pueda dejar
        constancia."""
        jobs = [job for job in self._jobs if not job.done()]
        if not jobs:
            return False
        logger.info("shadow_probe.preempted", extra={
            "event": "shadow_probe.preempted", "probes": len(jobs),
        })
        for job in jobs:
            job.cancel()
        # Se espera a que mueran de verdad: devolver el control antes de que
        # el `finally` del lease se haya ejecutado dejaría al llamante
        # compitiendo con la reserva que viene a liberar.
        await asyncio.gather(*jobs, return_exceptions=True)
        return True

    async def drain(self) -> None:
        """Espera a que terminen los sondeos en vuelo, sin cancelarlos.

        No la usa el broker en marcha —el sondeo existe precisamente para que
        nadie lo espere—; es la vía para que los tests observen el resultado."""
        while self._jobs:
            await asyncio.gather(*list(self._jobs), return_exceptions=True)

    def _is_probeable(self, request: TaskCreateRequest) -> bool:
        if not self.settings.enabled:
            return False
        # El proveedor de pruebas (bootstrap) no tiene catálogo ni métricas:
        # se comprueba aquí para no dejar un rastro de errores en el log por
        # algo que simplemente no aplica.
        if not all(
            callable(getattr(self.provider, name, None))
            for name in ("eligible_catalog", "model_stats_snapshot", "measure")
        ):
            return False
        if request.inference_kind != InferenceKind.chat:
            return False
        prompt = request.content.prompt or ""
        if not prompt or len(prompt) > self.settings.max_prompt_chars:
            # Un prompt gigante convierte cada sondeo en un trabajo caro y
            # largo. Se prefiere medir con peticiones normales; el sesgo hacia
            # prompts cortos queda acotado porque las métricas ya se segmentan
            # por tipo de tarea, y el contexto largo es un tipo aparte.
            return False
        return True

    async def _run(
        self, repository: TaskRepository, task_id: str, request: TaskCreateRequest,
    ) -> None:
        try:
            await self._probe(repository, task_id, request)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Un sondeo es trabajo opcional: que falle no puede afectar a nada.
            logger.warning("shadow_probe.failed", exc_info=True, extra={"event": "shadow_probe.failed"})

    async def _probe(
        self, repository: TaskRepository, task_id: str, request: TaskCreateRequest,
    ) -> None:
        allow_local = self.settings.probe_local_models and not await asyncio.to_thread(
            repository.has_active_task
        )
        _, _, candidates = await self.provider.eligible_catalog(request)
        if not candidates:
            return
        task_type = classify_task_type(request)
        aspirant = choose_aspirant(
            candidates,
            self.provider.model_stats_snapshot(),
            task_type,
            min_invocations=self.config.routing.min_invocations,
            allow_local=allow_local,
        )
        if aspirant is None:
            return
        model = ModelReference(
            provider=aspirant["provider"],
            deployment=aspirant["deployment"],
            model=aspirant["name"],
            role=SHADOW_ROLE,
        )
        probe_request = self._probe_request(request)
        was_loaded = await self._loaded_state(model)
        invocation_id = await asyncio.to_thread(
            repository.start_invocation,
            task_id, None, SHADOW_ROLE, model, task_type, was_loaded,
        )
        logger.info(
            "shadow_probe.started",
            extra={
                "event": "shadow_probe.started",
                "task_id": task_id,
                "task_type": task_type,
                "model": f"{model.provider}/{model.deployment}/{model.model}",
                "local": is_local_deployment(model.deployment),
            },
        )
        try:
            output = await self.provider.measure(probe_request, model)
        except Exception as error:
            # El fallo también es evidencia: un modelo que no responde no es
            # candidato, y eso debe verse en su tasa de éxito.
            code = getattr(error, "code", type(error).__name__)
            await asyncio.to_thread(
                repository.fail_invocation, invocation_id, task_id, str(code),
                str(error)[:2000], getattr(error, "output", None),
            )
            return
        await asyncio.to_thread(repository.complete_invocation, invocation_id, task_id, output)

    def _probe_request(self, request: TaskCreateRequest) -> TaskCreateRequest:
        """La misma petición, reducida a una única invocación medible.

        `model_copy` no revalida, así que la decisión de privacidad ya resuelta
        (cloud_allowed derivado de la clasificación) viaja intacta al sondeo en
        vez de recalcularse con otros valores por defecto."""
        return request.model_copy(update={
            "execution": request.execution.model_copy(update={
                "strategy": ExecutionStrategy.single,
                "preset": ExecutionPreset.fast,
                # El sondeo mide una llamada: trocear aquí mediría otra cosa.
                "long_context": "fail",
            }),
            # Los adjuntos ya vienen expandidos dentro del prompt en el momento
            # del despacho; dejarlos puestos los expandiría por segunda vez.
            "content": request.content.model_copy(update={"attachments": []}),
        })

    async def _loaded_state(self, model: ModelReference) -> bool | None:
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
