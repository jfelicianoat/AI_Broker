from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.config import (
    BrokerConfig,
    effective_max_parallel_invocations,
    local_memory_budget_bytes,
)
from app.schemas import ExecutionPreset, ExecutionStrategy, SchedulingPolicy, TaskCreateRequest


class ResourcePlanningError(RuntimeError):
    pass


def _gb(value: float) -> str:
    return f"{value / 1024**3:.1f}"


class SchedulingMode(str, Enum):
    parallel = "parallel"
    waves = "waves"
    sequential = "sequential"


class ResourcePlan(BaseModel):
    mode: SchedulingMode
    waves: list[list[str]]
    invocations_total: int
    peak_vram_reserved_gb: float = 0
    reasons: list[str] = Field(default_factory=list)


class ModelFootprint(BaseModel):
    """Lo que un proposer concreto le va a costar al pool de memoria local.

    Los modelos cloud pesan 0: no compiten por la memoria de la máquina.
    """

    label: str
    model: str
    size_bytes: int = 0


class ResourceScheduler:
    def __init__(self, config: BrokerConfig) -> None:
        self.config = config

    def plan(
        self, request: TaskCreateRequest, footprints: list[ModelFootprint] | None = None
    ) -> ResourcePlan:
        if request.execution.strategy == ExecutionStrategy.single or request.execution.preset != ExecutionPreset.slow:
            model = request.model_requirements.preferred_model or "auto"
            count = 1 if request.execution.strategy == ExecutionStrategy.single else request.execution.max_proposers
            labels = [model] if count == 1 else [f"proposer_{index}" for index in range(1, count + 1)]
            return ResourcePlan(
                mode=SchedulingMode.sequential,
                waves=[[label] for label in labels],
                invocations_total=count,
                reasons=["single and mixture_of_agents/fast are serial"],
            )

        count = request.execution.max_proposers
        labels = [f"proposer_{index}" for index in range(1, count + 1)]
        if request.execution.scheduling == SchedulingPolicy.sequential:
            return ResourcePlan(
                mode=SchedulingMode.sequential,
                waves=[[label] for label in labels],
                invocations_total=count,
                reasons=["sequential scheduling requested"],
            )
        max_parallel = self._max_parallel_invocations()

        if request.execution.scheduling == SchedulingPolicy.parallel and max_parallel < count:
            raise ResourcePlanningError(
                f"parallel requires {count} slots but only {max_parallel} are safely available"
            )

        # El reparto por tamaño manda sobre el de conteo cuando se conocen los
        # modelos elegidos: max_parallel supone un tamaño medio por modelo, y un
        # solo modelo grande basta para desmentirlo. Sin esto, el plan promete
        # una ola que la admisión de Ollama rechaza en cuanto arranca.
        if footprints:
            memory_plan = self._plan_by_memory(request, footprints, max_parallel)
            if memory_plan is not None:
                return memory_plan

        if max_parallel >= count:
            return ResourcePlan(
                mode=SchedulingMode.parallel,
                waves=[labels],
                invocations_total=count,
                reasons=["configured parallel capacity covers all proposers"],
            )

        if self.config.resources.allow_execution_waves and max_parallel > 1:
            waves = [labels[index : index + max_parallel] for index in range(0, count, max_parallel)]
            return ResourcePlan(
                mode=SchedulingMode.waves,
                waves=waves,
                invocations_total=count,
                reasons=["parallel capacity requires execution waves"],
            )

        return ResourcePlan(
            mode=SchedulingMode.sequential,
            waves=[[label] for label in labels],
            invocations_total=count,
            reasons=["parallel execution disabled or capacity limited to one"],
        )

    def _plan_by_memory(
        self,
        request: TaskCreateRequest,
        footprints: list[ModelFootprint],
        max_parallel: int,
    ) -> ResourcePlan | None:
        """Agrupa los proposers en olas que quepan de verdad en el presupuesto.

        Se recorre en el orden en que vienen, sin reordenar: el coordinador
        asigna los modelos a las olas por posición y ese orden es el de los
        roles (generalist, specialist, skeptic...). Reordenar aquí empaquetaría
        algo mejor a cambio de darle a cada rol el modelo de otro.
        """
        budget = local_memory_budget_bytes(self.config)
        if budget <= 0:
            return None
        waves: list[list[ModelFootprint]] = []
        current: list[ModelFootprint] = []
        current_bytes = 0
        for footprint in footprints:
            # Un modelo repetido dentro de la ola ya está pagado: Ollama lo
            # sirve del que tiene cargado en vez de traer una segunda copia.
            already_loaded = any(item.model == footprint.model for item in current)
            cost = 0 if already_loaded else footprint.size_bytes
            fits_memory = current_bytes + cost <= budget
            fits_slots = len(current) < max_parallel
            if current and not (fits_memory and fits_slots):
                waves.append(current)
                current, current_bytes = [], 0
                cost = footprint.size_bytes
            current.append(footprint)
            current_bytes += cost
        if current:
            waves.append(current)

        if len(waves) == 1:
            mode = SchedulingMode.parallel if len(waves[0]) > 1 else SchedulingMode.sequential
            reason = "los modelos elegidos caben juntos en el presupuesto de memoria"
        elif all(len(wave) == 1 for wave in waves):
            mode = SchedulingMode.sequential
            reason = "el tamaño de los modelos elegidos solo permite uno a la vez"
        else:
            mode = SchedulingMode.waves
            reason = "el tamaño de los modelos elegidos obliga a escalonar en olas"

        if mode is not SchedulingMode.parallel:
            # Escalonar es la respuesta correcta salvo que el cliente haya
            # pedido paralelo estricto: ahí prefiere enterarse a esperar.
            if request.execution.scheduling == SchedulingPolicy.parallel:
                total = sum(item.size_bytes for item in footprints)
                raise ResourcePlanningError(
                    f"parallel requires {_gb(total)} GB of local memory "
                    f"but only {_gb(budget)} GB are safely available"
                )
            if not self.config.resources.allow_execution_waves and mode is SchedulingMode.waves:
                waves = [[item] for item in footprints]
                mode = SchedulingMode.sequential
                reason = "execution waves disabled: se ejecuta uno a uno"

        peak = max(self._wave_bytes(wave) for wave in waves)
        oversized = [item.model for item in footprints if item.size_bytes > budget]
        reasons = [reason]
        if oversized:
            # No se puede arreglar escalonando; que quede dicho en el plan para
            # que el salto del proposer no parezca un fallo aleatorio.
            reasons.append(
                "no caben ni con la máquina vacía: " + ", ".join(sorted(set(oversized)))
            )
        return ResourcePlan(
            mode=mode,
            waves=[[item.label for item in wave] for wave in waves],
            invocations_total=len(footprints),
            peak_vram_reserved_gb=round(peak / 1024**3, 1),
            reasons=reasons,
        )

    @staticmethod
    def _wave_bytes(wave: list[ModelFootprint]) -> int:
        """Memoria que reserva una ola. Un mismo modelo en dos roles se paga
        una vez: es la misma copia cargada, y así lo cuenta la admisión."""
        loaded: dict[str, int] = {}
        for item in wave:
            loaded[item.model] = max(loaded.get(item.model, 0), item.size_bytes)
        return sum(loaded.values())

    def _max_parallel_invocations(self) -> int:
        # Fórmula única compartida con RoutedModelProvider (semáforo de
        # ejecución): plan y ejecución deben prometer la misma capacidad.
        return effective_max_parallel_invocations(self.config)

    def max_parallel_invocations(self) -> int:
        return self._max_parallel_invocations()

