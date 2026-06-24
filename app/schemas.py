from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(str, Enum):
    queued = "queued"
    routing = "routing"
    planning = "planning"
    resource_planning = "resource_planning"
    chunking = "chunking"
    generating = "generating"
    proposing = "proposing"
    evaluating = "evaluating"
    debating = "debating"
    synthesizing = "synthesizing"
    verifying = "verifying"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}


class ExecutionStrategy(str, Enum):
    single = "single"
    mixture_of_agents = "mixture_of_agents"


class ExecutionPreset(str, Enum):
    fast = "fast"
    standard = "standard"
    verified = "verified"
    high_stakes = "high_stakes"


class SchedulingPolicy(str, Enum):
    adaptive = "adaptive"
    parallel = "parallel"
    waves = "waves"
    sequential = "sequential"


class SelectionMode(str, Enum):
    auto = "auto"
    manual = "manual"
    hybrid = "hybrid"


class OutputFormat(str, Enum):
    markdown = "markdown"
    json = "json"
    text = "text"


class InferenceKind(str, Enum):
    chat = "chat"
    embedding = "embedding"


class DataClassification(str, Enum):
    public = "public"
    internal = "internal"
    confidential = "confidential"
    local_only = "local_only"


class ContentAttachment(StrictBaseModel):
    type: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    content: str | None = None
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_content_or_uri(self) -> "ContentAttachment":
        if not self.content and not self.uri:
            raise ValueError("attachment requires content or uri")
        return self


class TaskContent(StrictBaseModel):
    prompt: str = Field(min_length=1)
    attachments: list[ContentAttachment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskOutput(StrictBaseModel):
    format: OutputFormat = OutputFormat.markdown
    json_schema: dict[str, Any] | None = None
    language: str = Field(default="es", min_length=2, max_length=16)

    @model_validator(mode="after")
    def require_schema_for_json(self) -> "TaskOutput":
        if self.format == OutputFormat.json and self.json_schema is None:
            raise ValueError("output.json_schema is required when output.format is json")
        return self


class GenerationConfig(StrictBaseModel):
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_output_tokens: int = Field(default=4000, ge=1)


class ModelReference(StrictBaseModel):
    provider: str = Field(min_length=1, max_length=64)
    deployment: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    role: str | None = Field(default=None, max_length=64)
    required: bool = False


class ModelRequirements(StrictBaseModel):
    preferred_model: str | None = Field(default=None, max_length=128)
    fallback_allowed: bool = True
    cloud_allowed: bool = False
    allowed_providers: list[str] = Field(default_factory=lambda: ["ollama"])
    max_cost_usd: float | None = Field(default=None, ge=0)

    @field_validator("allowed_providers")
    @classmethod
    def providers_must_be_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("allowed_providers must not be empty")
        return value

    @model_validator(mode="after")
    def enforce_cloud_policy(self) -> "ModelRequirements":
        if not self.cloud_allowed:
            cloudish = {"deepseek", "ollama_cloud", "openai", "anthropic", "google"}
            if cloudish.intersection({provider.lower() for provider in self.allowed_providers}):
                raise ValueError("cloud providers are not allowed when cloud_allowed is false")
        return self


class SelectionPolicy(StrictBaseModel):
    mode: SelectionMode = SelectionMode.auto
    diversity_policy: str = "different_families"
    arbiter_policy: str = "strongest_available"
    preferred_arbiter: ModelReference | None = None
    allow_substitution: bool = True
    proposers: list[ModelReference] = Field(default_factory=list)
    required_proposers: list[ModelReference] = Field(default_factory=list)
    arbiter: ModelReference | None = None
    proposer_count: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def validate_mode(self) -> "SelectionPolicy":
        if self.mode == SelectionMode.manual:
            if not self.proposers:
                raise ValueError("manual selection requires proposers")
            if self.arbiter is None:
                raise ValueError("manual selection requires arbiter")
        if self.mode == SelectionMode.hybrid and self.proposer_count < len(self.required_proposers):
            raise ValueError("proposer_count must cover required_proposers")
        return self


class ExecutionConfig(StrictBaseModel):
    strategy: ExecutionStrategy = ExecutionStrategy.single
    preset: ExecutionPreset = ExecutionPreset.fast
    scheduling: SchedulingPolicy = SchedulingPolicy.adaptive
    max_proposers: int = Field(default=3, ge=1, le=5)
    max_judges: int = Field(default=1, ge=0, le=2)
    max_rounds: int = Field(default=1, ge=1, le=2)
    timeout_seconds: int = Field(default=600, ge=1)
    early_stop: bool = True
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mode_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "strategy" not in data and "mode" in data:
            data = dict(data)
            data["strategy"] = data.pop("mode")
        return data

    @model_validator(mode="after")
    def validate_strategy(self) -> "ExecutionConfig":
        if self.strategy == ExecutionStrategy.single and self.preset != ExecutionPreset.fast:
            raise ValueError("single strategy only supports preset fast")
        if self.strategy == ExecutionStrategy.mixture_of_agents:
            self.max_proposers = min(self.max_proposers, self.selection.proposer_count)
        return self


class RiskConfig(StrictBaseModel):
    data_classification: DataClassification = DataClassification.internal
    human_review_required: bool = False


class TaskCreateRequest(StrictBaseModel):
    idempotency_key: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9._:-]+$")
    request_id: str | None = Field(default=None, max_length=240)
    inference_kind: InferenceKind = InferenceKind.chat
    content: TaskContent
    output: TaskOutput = Field(default_factory=TaskOutput)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    model_requirements: ModelRequirements = Field(default_factory=ModelRequirements)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    priority: int = Field(default=100, ge=0, le=1000)

    @model_validator(mode="after")
    def enforce_data_boundary(self) -> "TaskCreateRequest":
        if self.risk.data_classification == DataClassification.local_only:
            self.model_requirements.cloud_allowed = False
            self.model_requirements.allowed_providers = [
                provider
                for provider in self.model_requirements.allowed_providers
                if provider.lower() == "ollama"
            ] or ["ollama"]
        if self.content.attachments:
            raise ValueError("attachments are not supported until a lossless provider mapping exists")
        if self.inference_kind == InferenceKind.embedding:
            if self.execution.strategy != ExecutionStrategy.single:
                raise ValueError("embedding only supports single strategy")
            if self.output.format != OutputFormat.json:
                raise ValueError("embedding requires output.format=json")
        return self


class TaskAcceptedResponse(StrictBaseModel):
    task_id: str
    status: TaskStatus
    execution_strategy: ExecutionStrategy
    execution_preset: ExecutionPreset
    selection_mode: SelectionMode
    status_url: str
    cancel_url: str


class TaskStateResponse(StrictBaseModel):
    task_id: str
    status: TaskStatus
    request_id: str | None
    created_at: datetime
    updated_at: datetime
    execution_strategy: ExecutionStrategy
    execution_preset: ExecutionPreset
    selection_mode: SelectionMode
    progress: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class QueueItem(StrictBaseModel):
    task_id: str
    status: TaskStatus
    request_id: str | None
    priority: int
    queue_position: int | None
    created_at: datetime
    updated_at: datetime


class QueueResponse(StrictBaseModel):
    pending: list[QueueItem]
    active: list[QueueItem]
    terminal: list[QueueItem]


class QueueReorderRequest(StrictBaseModel):
    task_ids: list[str] = Field(min_length=1)


class UsageResponse(StrictBaseModel):
    month: str
    providers: dict[str, dict[str, float]]


class HealthDependency(StrictBaseModel):
    status: Literal["healthy", "degraded", "unavailable"]
    checked_at: datetime
    detail: str | None = None
    latency_ms: float | None = None


class HealthResponse(StrictBaseModel):
    status: Literal["healthy", "degraded", "unavailable"]
    checked_at: datetime
    dependencies: dict[str, HealthDependency]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

