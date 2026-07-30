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
    # Estado propio del carril de ingesta: la conversión de un fichero a
    # Markdown está en marcha. Ocupa la máquina, así que cuenta como trabajo
    # activo, pero no consume el workflow único de la inferencia.
    converting = "converting"
    waiting_for_tools = "waiting_for_tools"
    # La tarea cabe en el presupuesto, pero ahora mismo no hay memoria libre:
    # algo la ocupa (otro proceso, un modelo cargado a mano en LM Studio). No
    # es un fallo y no consume el workflow único — espera su turno mientras el
    # resto de la cola sigue avanzando.
    waiting_for_memory = "waiting_for_memory"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}


class TaskKind(str, Enum):
    """Carril al que pertenece una unidad de trabajo.

    Los dos comparten cola, historial y panel —era el objetivo: ver la carga
    real de la máquina en un solo sitio— pero NO comparten capacidad. La
    inferencia mantiene su invariante de un único workflow activo; la ingesta
    tiene su propia concurrencia. Sin esta separación, subir un PDF congelaría
    la cola de inferencia.
    """

    inference = "inference"
    ingestion = "ingestion"

# Política de privacidad fail-closed: solo lo que se ejecuta en esta máquina
# cuenta como local ("bootstrap" es el proveedor fake in-process). Cualquier
# otro deployment ("cloud", "api" o valores futuros) se trata como externo y
# exige cloud_allowed=true.
LOCAL_DEPLOYMENTS = {"local", "bootstrap"}


def is_local_deployment(deployment: str | None) -> bool:
    return str(deployment or "").strip().lower() in LOCAL_DEPLOYMENTS


class ExecutionStrategy(str, Enum):
    single = "single"
    mixture_of_agents = "mixture_of_agents"
    agent = "agent"
    # El broker elige la estrategia concreta (meta-router); requiere
    # strategy_router.enabled en la configuración.
    auto = "auto"


# Skills técnicas genéricas ejecutables por el broker en la estrategia agent.
# Son deliberadamente neutrales al dominio: la orquestación de negocio sigue
# perteneciendo a las apps cliente.
AGENT_SKILLS = ("web_search", "fetch_url", "calculator", "current_datetime", "run_code")
# run_code queda fuera del default: requiere sandbox.enabled y Docker en
# marcha, así que solo participa cuando la tarea lo pide explícitamente.
DEFAULT_AGENT_SKILLS = ("web_search", "fetch_url", "calculator", "current_datetime")


class ExecutionPreset(str, Enum):
    fast = "fast"
    slow = "slow"
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


# Clasificaciones que obligan a quedarse en esta máquina. `confidential` entra
# aquí desde 2026-07-26: hasta entonces el nombre prometía una frontera que no
# cumplía (solo marcaba high_stakes para el meta-router de estrategia, y la
# tarea podía salir a cloud igual que una `internal`).
LOCAL_ONLY_CLASSIFICATIONS = frozenset(
    {DataClassification.confidential, DataClassification.local_only}
)


def classification_allows_cloud(classification: DataClassification) -> bool:
    """Única fuente de verdad de la frontera de datos.

    `data_classification` es el mando de privacidad del contrato: lo declara la
    app cliente y de él se derivan los demás cerrojos (cloud_allowed y
    allowed_providers) cuando el cliente no se pronuncia sobre ellos."""
    return classification not in LOCAL_ONLY_CLASSIFICATIONS


# Adjuntos de fichero ingeridos por el broker (POST /api/v1/files). El único
# tipo soportado: referencia por file_id a un fichero ya convertido a Markdown.
BROKER_FILE_ATTACHMENT_TYPE = "broker_file"
BROKER_FILE_URI_PREFIX = "broker://files/"


class FileStatus(str, Enum):
    received = "received"
    converting = "converting"
    ready = "ready"
    failed = "failed"


class ContentAttachment(StrictBaseModel):
    type: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=255)
    content: str | None = None
    uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_content_or_uri(self) -> ContentAttachment:
        if not self.content and not self.uri and not self.metadata.get("file_id"):
            raise ValueError("attachment requires content, uri or metadata.file_id")
        return self


def attachment_file_id(attachment: ContentAttachment) -> str | None:
    """file_id de un adjunto broker_file (metadata.file_id o uri broker://files/...)."""
    candidate = attachment.metadata.get("file_id")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    if attachment.uri and attachment.uri.startswith(BROKER_FILE_URI_PREFIX):
        return attachment.uri[len(BROKER_FILE_URI_PREFIX):] or None
    return None


class TaskContent(StrictBaseModel):
    prompt: str = Field(min_length=1)
    attachments: list[ContentAttachment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskOutput(StrictBaseModel):
    format: OutputFormat = OutputFormat.markdown
    json_schema: dict[str, Any] | None = None
    language: str = Field(default="es", min_length=2, max_length=16)

    @model_validator(mode="after")
    def require_schema_for_json(self) -> TaskOutput:
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
    target_model: ModelReference | None = None
    fallback_allowed: bool = True
    # Tri-estado deliberado. None = "no me pronuncio": lo deriva el broker de
    # risk.data_classification (ver TaskCreateRequest.enforce_data_boundary).
    # True/False son declaraciones explícitas del cliente y siguen mandando,
    # salvo que la clasificación imponga frontera local, que no se puede ceder.
    cloud_allowed: bool | None = None
    # None = sin restricción por proveedor. El default anterior era ["ollama"],
    # que dejaba fuera a todo proveedor cloud aunque cloud_allowed fuese true:
    # era un segundo cerrojo silencioso que la app cliente tenía que acertar.
    allowed_providers: list[str] | None = None
    max_cost_usd: float | None = Field(default=None, ge=0)

    @field_validator("allowed_providers")
    @classmethod
    def providers_must_be_non_empty(cls, value: list[str] | None) -> list[str] | None:
        # None es "sin restricción"; la lista vacía sigue siendo un error,
        # porque significa "ningún proveedor" y deja la petición inatendible.
        if value is not None and not value:
            raise ValueError("allowed_providers must not be empty")
        return value

    @model_validator(mode="after")
    def enforce_cloud_policy(self) -> ModelRequirements:
        # Solo se comprueba la negativa EXPLÍCITA del cliente: con None la
        # decisión aún no se ha tomado (la toma enforce_data_boundary con la
        # clasificación delante), y prohibir aquí sería negar lo que ese
        # validador está a punto de conceder.
        explicit_local = self.cloud_allowed is False
        allowed = (
            {provider.lower() for provider in self.allowed_providers}
            if self.allowed_providers is not None
            else None
        )
        if explicit_local and allowed is not None:
            cloudish = {"deepseek", "ollama_cloud", "openai", "anthropic", "google"}
            if cloudish.intersection(allowed):
                raise ValueError("cloud providers are not allowed when cloud_allowed is false")
        if self.target_model is not None:
            target_provider = self.target_model.provider.lower()
            if allowed is not None and target_provider not in allowed:
                raise ValueError("target_model.provider must be included in allowed_providers")
            if self.preferred_model is not None and self.preferred_model != self.target_model.model:
                raise ValueError("preferred_model and target_model.model must match when both are provided")
            target_is_cloud = (
                not is_local_deployment(self.target_model.deployment)
                or target_provider in {"deepseek", "ollama_cloud", "openai", "anthropic", "google"}
            )
            if target_is_cloud and explicit_local:
                raise ValueError("target_model requires cloud_allowed=true")
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
    def validate_mode(self) -> SelectionPolicy:
        if self.mode == SelectionMode.manual:
            if not self.proposers:
                raise ValueError("manual selection requires proposers")
            if self.arbiter is None:
                raise ValueError("manual selection requires arbiter")
        if self.mode == SelectionMode.hybrid and self.proposer_count < len(self.required_proposers):
            raise ValueError("proposer_count must cover required_proposers")
        return self


class ClientToolDefinition(StrictBaseModel):
    """Herramienta de dominio que aporta el cliente: el broker la ofrece al
    modelo pero NO la ejecuta — pausa la tarea y devuelve la llamada para que
    el cliente la resuelva (passthrough)."""
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(min_length=1, max_length=1024)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class AgentExecutionConfig(StrictBaseModel):
    # Guardarraíles del loop agéntico: sin tope de iteraciones un modelo que
    # nunca concluye consumiría presupuesto y tiempo indefinidamente.
    skills: list[Literal["web_search", "fetch_url", "calculator", "current_datetime", "run_code"]] = Field(
        default_factory=lambda: list(DEFAULT_AGENT_SKILLS), max_length=8,  # type: ignore[arg-type]
    )
    max_iterations: int = Field(default=6, ge=1, le=20)
    # Tools del cliente (passthrough): al llamarlas, la tarea pasa a
    # waiting_for_tools y el cliente las resuelve vía POST /tasks/{id}/tool_results.
    client_tools: list[ClientToolDefinition] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_skills(self) -> AgentExecutionConfig:
        if not self.skills and not self.client_tools:
            raise ValueError("agent strategy requires at least one skill or client tool")
        self.skills = list(dict.fromkeys(self.skills))
        names = [tool.name for tool in self.client_tools]
        if len(names) != len(set(names)):
            raise ValueError("client_tools names must be unique")
        reserved = set(AGENT_SKILLS)
        if reserved.intersection(names):
            raise ValueError("client_tools cannot reuse built-in skill names")
        return self


class ExecutionConfig(StrictBaseModel):
    strategy: ExecutionStrategy = ExecutionStrategy.single
    preset: ExecutionPreset = ExecutionPreset.fast
    # Política cuando los documentos adjuntos no caben en el contexto de ningún
    # modelo elegible. "fail" (default) conserva el contrato clásico: error
    # explícito, nunca se divide en silencio. "map_reduce" AUTORIZA al broker a
    # trocear los documentos, procesar cada fragmento y sintetizar (algoritmo
    # técnico versionado, como el árbitro del consenso). Solo estrategia
    # single/auto con inference chat.
    long_context: Literal["fail", "map_reduce"] = "fail"
    scheduling: SchedulingPolicy = SchedulingPolicy.adaptive
    max_proposers: int = Field(default=3, ge=1, le=5)
    max_judges: int = Field(default=1, ge=0, le=2)
    max_rounds: int = Field(default=1, ge=1, le=2)
    timeout_seconds: int = Field(default=600, ge=1)
    early_stop: bool = True
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)
    agent: AgentExecutionConfig = Field(default_factory=AgentExecutionConfig)
    # Skills que cada proponente del mixture puede usar antes de proponer
    # (verifica datos, calcula, consulta fecha). Vacío = comportamiento clásico:
    # una sola generación por proponente, sin tools. No afecta al árbitro.
    proposer_skills: list[Literal["web_search", "fetch_url", "calculator", "current_datetime", "run_code"]] = Field(
        default_factory=list, max_length=8,
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mode_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "strategy" not in data and "mode" in data:
            data = dict(data)
            data["strategy"] = data.pop("mode")
        return data

    @model_validator(mode="after")
    def validate_strategy(self) -> ExecutionConfig:
        if self.strategy == ExecutionStrategy.single and self.preset != ExecutionPreset.fast:
            raise ValueError("single strategy only supports preset fast")
        if self.strategy == ExecutionStrategy.agent and self.preset != ExecutionPreset.fast:
            raise ValueError("agent strategy only supports preset fast")
        if self.strategy == ExecutionStrategy.mixture_of_agents:
            self.max_proposers = min(self.max_proposers, self.selection.proposer_count)
            self.proposer_skills = list(dict.fromkeys(self.proposer_skills))
        elif self.proposer_skills:
            raise ValueError("proposer_skills solo aplica a la estrategia mixture_of_agents")
        return self


def run_code_available(execution: ExecutionConfig) -> bool:
    """True si esta ejecución podría llegar a invocar la skill run_code.

    single no tiene mecanismo de tool-calling en absoluto: nunca es True.
    auto se resuelve más tarde (ver strategy_router); antes de resolverse no
    se puede saber con certeza, así que se trata como True (no bloquear
    prematuramente una tarea que auto podría resolver a agent/mixture con
    run_code) — el bloqueo real para auto ocurre después de resolver, en
    ConsensusCoordinator._process_task_request.
    """
    if execution.strategy == ExecutionStrategy.agent:
        return "run_code" in execution.agent.skills
    if execution.strategy == ExecutionStrategy.mixture_of_agents:
        return "run_code" in execution.proposer_skills
    if execution.strategy == ExecutionStrategy.auto:
        return True
    return False


def uses_tool_loop(execution: ExecutionConfig) -> bool:
    """True si el prompt del usuario va a viajar dentro de un loop agéntico.

    En ese loop el prompt no es un enunciado que se lee una vez: se reenvía en
    cada iteración como las instrucciones que el modelo debe seguir mientras
    decide qué tool llamar e interpreta lo que devuelve. Lo usa `user_prompt`
    para no comprimirlo tan agresivamente como una generación de un solo turno
    (ver RoutedModelProvider.user_prompt).

    auto se trata como True por la misma razón que en run_code_available: aún
    no se sabe a qué estrategia resolverá, y equivocarse hacia la compresión
    prudente solo cuesta unos tokens.
    """
    if execution.strategy == ExecutionStrategy.agent:
        return True
    if execution.strategy == ExecutionStrategy.mixture_of_agents:
        return bool(execution.proposer_skills)
    return execution.strategy == ExecutionStrategy.auto


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
    # Override por tarea de la compresión de prompts. None = usar la
    # configuración global del broker; "off" = enviar el prompt tal cual.
    prompt_compression: Literal["off", "light", "medium", "aggressive"] | None = None

    @model_validator(mode="after")
    def validate_agent_constraints(self) -> TaskCreateRequest:
        if self.execution.strategy == ExecutionStrategy.agent:
            if self.inference_kind != InferenceKind.chat:
                raise ValueError("agent strategy only supports chat inference")
            if self.output.format == OutputFormat.json:
                raise ValueError("agent strategy does not support json output format yet")
        if self.execution.long_context == "map_reduce":
            if self.execution.strategy not in {ExecutionStrategy.single, ExecutionStrategy.auto}:
                raise ValueError("long_context=map_reduce only supports single or auto strategy")
            if self.inference_kind != InferenceKind.chat:
                raise ValueError("long_context=map_reduce only supports chat inference")
            if self.output.format == OutputFormat.json:
                raise ValueError("long_context=map_reduce does not support json output format")
        return self

    @model_validator(mode="after")
    def enforce_data_boundary(self) -> TaskCreateRequest:
        # La clasificación de datos es el mando único: de ella se derivan los
        # cerrojos que el cliente no haya declarado. Derivar en un solo sitio
        # evita el fallo de antes, donde una app tenía que acertar tres campos
        # a la vez para que un modelo cloud fuese siquiera candidato.
        requirements = self.model_requirements
        classification = self.risk.data_classification
        if classification_allows_cloud(classification):
            if requirements.cloud_allowed is None:
                # Sin frontera declarada, el silencio se resuelve a favor de la
                # velocidad: el catálogo cloud entra en la selección.
                requirements.cloud_allowed = True
        else:
            local_provider_names = {"ollama", "lmstudio"}
            target = requirements.target_model
            if target is not None and (
                target.provider.lower() not in local_provider_names
                or target.deployment.lower() == "cloud"
            ):
                raise ValueError(
                    f"{classification.value} target_model must be a local deployment"
                )
            # La frontera gana a un cloud_allowed=true explícito: pedir ambas
            # cosas es contradictorio, y de las dos la que no se puede ceder es
            # la privacidad. Se degrada en silencio, como se venía haciendo.
            requirements.cloud_allowed = False
            if requirements.allowed_providers is not None:
                requirements.allowed_providers = [
                    provider
                    for provider in requirements.allowed_providers
                    if provider.lower() in local_provider_names
                ] or ["ollama"]
        for attachment in self.content.attachments:
            # Solo adjuntos ya ingeridos por el broker: el mapeo es sin pérdida
            # porque el fichero llega al modelo como Markdown dentro del prompt.
            if attachment.type != BROKER_FILE_ATTACHMENT_TYPE or attachment_file_id(attachment) is None:
                raise ValueError(
                    "only broker_file attachments referencing an ingested file_id are supported "
                    "(upload via POST /api/v1/files first)"
                )
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
    kind: TaskKind = TaskKind.inference
    status: TaskStatus
    request_id: str | None
    created_at: datetime
    updated_at: datetime
    # None en el carril de ingesta: una conversión de fichero no tiene
    # estrategia, ni preset, ni modo de selección. Rellenarlas con "single" para
    # que el modelo validara sería inventarle una ejecución que no existe.
    execution_strategy: ExecutionStrategy | None = None
    execution_preset: ExecutionPreset | None = None
    selection_mode: SelectionMode | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class QueueItem(StrictBaseModel):
    task_id: str
    # Default por compatibilidad: todo lo que existía antes de los carriles es
    # inferencia, y las filas antiguas no llevan la columna.
    kind: TaskKind = TaskKind.inference
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


class ToolResultItem(StrictBaseModel):
    tool_call_id: str = Field(min_length=1, max_length=128)
    content: str = Field(max_length=200_000)


class ToolResultsRequest(StrictBaseModel):
    tool_results: list[ToolResultItem] = Field(min_length=1, max_length=16)


class UsageResponse(StrictBaseModel):
    month: str
    providers: dict[str, dict[str, float]]


class ModelContextResponse(StrictBaseModel):
    provider: str
    deployment: str
    model: str
    context_window: int | None
    context_window_known: bool
    capabilities: list[str] = Field(default_factory=list)
    features: dict[str, dict[str, Literal["supported", "unsupported", "unknown"]]] = Field(default_factory=dict)
    feature_notes: list[str] = Field(default_factory=list)
    compatibility: str | None = None
    compatibility_checked_at: str | None = None
    compatibility_error: str | None = None


class ModelAvailabilityItem(StrictBaseModel):
    provider: str
    deployment: str
    model: str
    availability: Literal["online", "offline", "unknown", "incompatible"]
    dispatchable: bool
    reason: str
    provider_status: Literal["healthy", "degraded", "unavailable", "unknown"]
    model_status: str | None = None
    compatibility: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    context_window: int | None = None
    compatibility_error: str | None = None


class ModelAvailabilityResponse(StrictBaseModel):
    checked_at: datetime
    items: list[ModelAvailabilityItem]
    counts: dict[str, int]


class BrokerCapabilitiesResponse(StrictBaseModel):
    contract_version: str
    strategies: list[ExecutionStrategy]
    presets: dict[str, list[ExecutionPreset]]
    scheduling_by_preset: dict[str, list[SchedulingPolicy]]
    max_active_workflows: int
    max_parallel_invocations: int
    exact_target_model: bool
    task_timeout: bool
    # La tarea puede fijar su propia compresión de prompt (campo opcional
    # prompt_compression: off/light/medium/aggressive; ausente = config global).
    prompt_compression_override: bool
    # Estrategia agent con skills técnicas ejecutadas por el broker.
    agent_skills: list[str]
    # Los proponentes de un mixture pueden usar esas mismas skills antes de proponer.
    proposer_skills: bool
    # Passthrough: la estrategia agent acepta tools del cliente y pausa la tarea
    # (waiting_for_tools) hasta que el cliente las resuelve vía tool_results.
    client_tool_passthrough: bool
    # El broker elige la estrategia si se pide strategy: auto (meta-router).
    auto_strategy: bool
    # Pieza 2: single puede escalar a mixture si sale de baja confianza.
    confidence_escalation: bool
    # Pieza 3: el router aprende de casos previos para afinar la estrategia.
    adaptive_strategy_learning: bool
    # Ingesta de ficheros: POST /api/v1/files convierte a Markdown y las tareas
    # adjuntan por file_id (attachments type=broker_file).
    file_ingestion: bool = False
    # Skill run_code disponible (sandbox Docker habilitado en la configuración).
    sandbox_run_code: bool = False
    # La tarea puede autorizar map-reduce cuando los adjuntos exceden el
    # contexto (execution.long_context: "map_reduce"); default sigue siendo fail.
    long_context_map_reduce: bool = False
    # Extensiones admitidas por la ingesta, agrupadas por tipo.
    ingestion_formats: dict[str, list[str]] = Field(default_factory=dict)
    # Contrato 2.6. La frontera de datos se declara en UN solo campo,
    # risk.data_classification, y de él se derivan cloud_allowed y
    # allowed_providers cuando el cliente no se pronuncia. Un cliente que vea
    # esta capacidad puede omitir los dos campos derivados y confiar en su
    # clasificación; sin ella, tiene que seguir enviando los tres a la vez.
    derived_data_boundary: bool = False
    # Carriles de trabajo: las conversiones de fichero son tareas visibles
    # (kind=ingestion) con su propia concurrencia, y se consultan por las
    # mismas vías que la inferencia.
    work_lanes: list[TaskKind] = Field(default_factory=list)


class FileAcceptedResponse(StrictBaseModel):
    file_id: str
    status: FileStatus
    filename: str
    size_bytes: int
    sha256: str
    # False cuando la subida dedujo a un fichero ya ingerido (mismo SHA-256).
    created: bool
    status_url: str


class FileStateResponse(StrictBaseModel):
    file_id: str
    status: FileStatus
    filename: str
    kind: str
    engine: str
    size_bytes: int
    sha256: str
    meta: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    # Solo presente cuando status=ready.
    markdown_url: str | None = None


class LaneOccupancy(StrictBaseModel):
    """Ocupación de un carril de trabajo: la carga real de la máquina.

    Existe porque antes solo se veía la inferencia. Las conversiones corrían al
    margen de toda proyección, así que el panel podía decir «sin tarea activa»
    con dos Doclings comiéndose la CPU."""

    kind: TaskKind
    active: int
    queued: int
    capacity: int


class DashboardSummaryResponse(StrictBaseModel):
    checked_at: datetime
    window_hours: int
    queued: int
    active: int
    completed: int
    failed: int
    cancelled: int
    success_rate: float | None
    invocations: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    tokens_input: int
    tokens_output: int
    cost_actual_usd: float
    oldest_queued_seconds: float | None
    lanes: list[LaneOccupancy] = Field(default_factory=list)


class DashboardTaskItem(StrictBaseModel):
    task_id: str
    kind: TaskKind = TaskKind.inference
    # Nombre legible del trabajo cuando no es una inferencia: el fichero que se
    # está convirtiendo. Una conversión no tiene prompt que enseñar.
    label: str | None = None
    request_id: str | None
    status: TaskStatus
    priority: int
    queue_position: int | None
    origin: str | None
    # None en el carril de ingesta, como en TaskStateResponse: una conversión
    # no tiene estrategia ni preset.
    execution_strategy: ExecutionStrategy | None = None
    execution_preset: ExecutionPreset | None = None
    requested_model: ModelReference | None
    effective_model: ModelReference | None
    fallback_used: bool | None
    # Por qué está esperando memoria y quién se la ocupa, cuando el estado es
    # `waiting_for_memory`. None en cualquier otro caso.
    memory_block: dict[str, Any] | None = None
    invocations: int
    tokens_input: int
    tokens_output: int
    cost_actual_usd: float
    created_at: datetime
    updated_at: datetime


class DashboardTaskPage(StrictBaseModel):
    items: list[DashboardTaskItem]
    page: int
    page_size: int
    total: int
    total_pages: int


class DashboardInvocationItem(StrictBaseModel):
    invocation_id: str
    role: str
    provider: str
    deployment: str
    model: str
    status: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    latency_ms: float | None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DashboardEventItem(StrictBaseModel):
    event_id: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class DashboardTaskDetail(StrictBaseModel):
    task: DashboardTaskItem
    request: dict[str, Any]
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    invocations: list[DashboardInvocationItem]
    events: list[DashboardEventItem]


class DashboardLoadedModel(StrictBaseModel):
    model: str
    size_vram_bytes: int
    context_length: int | None = None
    lease_count: int = 0


class DashboardResourcesResponse(StrictBaseModel):
    checked_at: datetime
    provider: str
    status: Literal["healthy", "unavailable"] = "healthy"
    detail: str | None = None
    vram_budget_bytes: int
    vram_safety_margin_bytes: int
    used_vram_bytes: int
    reserved_vram_bytes: int
    max_parallel_invocations: int
    loaded_models: list[DashboardLoadedModel]


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

