from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.config import BrokerConfig
from app.schemas import ExecutionPreset, ExecutionStrategy, SchedulingPolicy, TaskCreateRequest


class ResourcePlanningError(RuntimeError):
    pass


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


class ResourceScheduler:
    def __init__(self, config: BrokerConfig) -> None:
        self.config = config

    def plan(self, request: TaskCreateRequest) -> ResourcePlan:
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

    def _max_parallel_invocations(self) -> int:
        configured = self.config.processing.max_parallel_invocations
        if isinstance(configured, int):
            return configured
        usable_vram = max(
            1.0,
            self.config.resources.local_vram_budget_gb - self.config.resources.vram_safety_margin_gb,
        )
        # Conservative bootstrap estimate until real model telemetry exists.
        return max(1, min(3, int(usable_vram // 18)))

    def max_parallel_invocations(self) -> int:
        return self._max_parallel_invocations()

