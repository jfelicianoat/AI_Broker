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
    # La tarea declara depender de otras que aún no han terminado. Hermano de
    # `waiting_for_memory`: no es un fallo, no consume el workflow único y no
    # pierde el sitio en la cola. Se distingue de aquel en que aquí lo que falta
    # es trabajo ajeno, no máquina.
    waiting_for_dependencies = "waiting_for_dependencies"
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
#
# Cada una declara por dónde salen sus datos: "egress" saca contenido de la
# tarea de esta máquina, "local" se resuelve dentro (el sandbox de run_code
# corre con --network none). Sin esta tabla la frontera de datos era una
# promesa a medias: `local_only` apagaba los proveedores cloud por validación y
# acto seguido `web_search` mandaba el texto de la consulta a un buscador. La
# lista de skills se DERIVA de aquí a propósito, para que no pueda existir una
# skill sin declarar su frontera.
AGENT_SKILL_DATA_BOUNDARY: dict[str, Literal["local", "egress"]] = {
    "web_search": "egress",
    "fetch_url": "egress",
    "calculator": "local",
    "current_datetime": "local",
    "run_code": "local",
}
AGENT_SKILLS = tuple(AGENT_SKILL_DATA_BOUNDARY)
EGRESS_AGENT_SKILLS = frozenset(
    name for name, boundary in AGENT_SKILL_DATA_BOUNDARY.items() if boundary == "egress"
)
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
    # None = no enviar el campo, NO "enviar el valor por defecto del proveedor":
    # una petición sin semilla produce exactamente el mismo cuerpo que antes de
    # que estos dos campos existieran.
    #
    # Que el broker envíe la semilla no garantiza que el modelo la honre: eso
    # depende del runtime y no se puede comprobar desde fuera. Por eso la
    # telemetría de la invocación declara si llegó a enviarse (`seed_status`);
    # una semilla aceptada en silencio y descartada por debajo es peor que no
    # tenerla, porque hace creer que un resultado es reproducible cuando no lo es.
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    top_p: float | None = Field(default=None, ge=0, le=1)


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
    # El árbitro no es un proposer más: no aporta una respuesta, decide cuál de
    # las que hay es cierta. Ordenarlo por rapidez, como al resto, pone a
    # juzgar al modelo que antes conteste; `strongest_available` ordena por
    # tamaño declarado, que es el único indicio de capacidad que hay en el
    # catálogo. `fastest_available` conserva el orden por tiempo esperado.
    arbiter_policy: Literal["strongest_available", "fastest_available"] = "strongest_available"
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
    # Servidores MCP (ids de `mcp.servers` en la configuración del broker) cuyas
    # herramientas se ofrecen al modelo en esta tarea. Opt-in explícito: un
    # servidor configurado no se cuela en todas las tareas, porque cada
    # herramienta que se añade es contexto que el modelo tiene que leer y una
    # forma más de que la tarea se vaya por las ramas.
    mcp_servers: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_skills(self) -> AgentExecutionConfig:
        if not self.skills and not self.client_tools and not self.mcp_servers:
            raise ValueError("agent strategy requires at least one skill, client tool or MCP server")
        if len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError("mcp_servers must not repeat")
        # Solo se reasigna si la deduplicación cambia algo: asignar un atributo
        # lo marca como "puesto por el cliente" en model_fields_set, y de ese
        # dato depende la frontera de datos para distinguir una skill elegida de
        # un default del broker (ver TaskCreateRequest.enforce_data_boundary).
        deduped = list(dict.fromkeys(self.skills))
        if deduped != self.skills:
            self.skills = deduped
        names = [tool.name for tool in self.client_tools]
        if len(names) != len(set(names)):
            raise ValueError("client_tools names must be unique")
        # La colisión real es con las skills HABILITADAS en esta tarea: dos
        # definiciones del mismo nombre en la misma llamada al modelo. Si la
        # skill no está activa el nombre queda libre — una app que orquesta su
        # propia investigación necesita poder llamar `web_search` a su
        # herramienta, que es el nombre con el que los modelos vienen
        # entrenados, sin heredar la búsqueda que ejecuta el broker.
        shadowed = sorted(set(self.skills).intersection(names))
        if shadowed:
            raise ValueError(
                "client_tools cannot reuse an enabled skill name: " + ", ".join(shadowed)
            )
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
    # Esta tarea NO debe enseñarle nada al broker. Lo respetan las métricas por
    # modelo (app.model_stats), la cuarentena (app.model_quarantine), la
    # estimación de tiempo que se deriva de ellas (app.model_timing) y la
    # elección de aspirante del sondeo en sombra (app.shadow_probe).
    #
    # Existe porque medir un modelo lo degradaba en producción: quien lo somete
    # a prompts deliberadamente difíciles —contexto extremo, instrucciones
    # contradictorias, entradas adversariales— le bajaba la tasa de fiabilidad
    # al router y podía llegar a apartarlo del catálogo por acumulación de
    # fallos. El broker no podía distinguir "falló porque el modelo es malo" de
    # "falló porque el cliente estaba intentando que fallara". Le pasa igual al
    # probador de prompts del propio panel cuando se usa para reproducir un fallo.
    #
    # Es un campo de primera clase y no una cadena en content.metadata a
    # propósito: hacer que el router adaptativo dependa de metadata opaca del
    # cliente sería un acoplamiento oculto que nadie que lea model_stats.py
    # podría sospechar.
    #
    # Excluido del APRENDIZAJE, no de la CONTABILIDAD: sigue consumiendo
    # presupuesto, cuotas, semáforo y VRAM, y sigue apareciendo en uso, coste y
    # panel. Lo único que no hace es mover los indicadores que alimentan
    # decisiones automáticas.
    exclude_from_model_learning: bool = False
    # --- Dependencias entre tareas ---
    # Etiqueta a la que pertenece esta tarea. Existe para que otra pueda
    # esperarla en bloque sin conocer sus identificadores: el caso real es un
    # documento que se indexa en cientos de embeddings y una pregunta que no
    # debe correr hasta que el índice esté entero. Enviar esos cientos de ids
    # en cada pregunta sería inutilizable.
    group: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")
    # Tareas concretas que deben haber terminado antes que esta.
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    # Grupo que debe estar drenado (ninguna tarea suya sin terminar) antes que
    # esta. Se evalúa al reclamar, así que solo cuentan las que ya existen.
    depends_on_group: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$"
    )

    @model_validator(mode="after")
    def validate_dependencies(self) -> TaskCreateRequest:
        if self.depends_on_group is not None and self.depends_on_group == self.group:
            # Esperar a tu propio grupo es esperarte a ti mismo con pasos
            # extra: las 259 tareas de un índice se bloquearían entre ellas.
            raise ValueError("depends_on_group must not be the task's own group")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on must not repeat task ids")
        return self

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
            # Las skills con salida a red se rechazan en vez de apagarse en
            # silencio: quitarlas dejaría correr una investigación mutilada que
            # el cliente cree completa, y aquí lo que pidió y lo que se puede
            # hacer son incompatibles de verdad. Mismo criterio que target_model
            # cloud unas líneas más arriba.
            #
            # Solo cuenta lo que el cliente PIDIÓ. `agent.skills` trae defaults
            # que viajan en toda petición: hacer fallar la tarea por un default
            # del broker sería culpar al cliente de una elección que no hizo, así
            # que ahí la lista se acota a las skills locales y punto. Lo que el
            # cliente nombró explícitamente sí es incompatible y se rechaza.
            agent = self.execution.agent
            chose_skills = "skills" in agent.model_fields_set
            requested_skills: set[str] = set(self.execution.proposer_skills)
            if chose_skills:
                requested_skills |= set(agent.skills)
            offending = sorted(requested_skills & EGRESS_AGENT_SKILLS)
            if offending:
                raise ValueError(
                    f"{classification.value} does not allow skills that send task content "
                    f"off this machine: {', '.join(offending)}"
                )
            if not chose_skills:
                agent.skills = [  # type: ignore[assignment]
                    skill for skill in agent.skills if skill not in EGRESS_AGENT_SKILLS
                ]
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


class ExecutionFingerprintComponent(StrictBaseModel):
    """Un componente de la huella con su procedencia (app.execution_fingerprint).

    `no_disponible` es un resultado legítimo y hay que verlo: en un modelo cloud
    la plantilla y el tokenizador no son observables, y eso es información real
    sobre el límite de lo que se puede afirmar de esa ejecución."""

    value: str | None = None
    status: Literal["verificado", "declarado", "no_disponible"] = "no_disponible"


class ExecutionFingerprint(StrictBaseModel):
    """Configuración efectiva que produce (o produjo) una respuesta.

    `hash` es canónico y estable: dos arranques con la misma configuración dan
    el mismo valor, así que sirve para comparar dos respuestas separadas en el
    tiempo sin mirar componente a componente."""

    hash: str
    components: dict[str, ExecutionFingerprintComponent] = Field(default_factory=dict)


# Estado de un parámetro de generación en la petición que llegó al proveedor.
# "sent" significa que el broker lo incluyó en el cuerpo, no que el modelo lo
# haya honrado: eso último no es observable desde fuera y no se afirma.
GenerationParameterStatus = Literal["not_requested", "sent", "unsupported"]


class EffectiveGeneration(StrictBaseModel):
    """Parámetros con los que se invocó de verdad al modelo.

    Puede diferir de lo pedido: `max_output_tokens` se recorta cuando el prompt
    y la reserva de salida no caben juntos en la ventana del modelo (ver
    app.providers.base.request_with_context_capped_output)."""

    temperature: float | None = None
    max_output_tokens: int | None = None
    seed: int | None = None
    seed_status: GenerationParameterStatus = "not_requested"
    top_p: float | None = None
    top_p_status: GenerationParameterStatus = "not_requested"


class TaskExecutionSummary(StrictBaseModel):
    """Quién sirvió la tarea, bajo contrato.

    Hasta ahora `fallback_used` solo existía en la superficie de dashboard, que
    es administrativa y tiene otra autenticación: un cliente de la API v1 que
    quisiera saber si le respondió el modelo que pidió tenía que parsear
    `result`, que es un diccionario sin contrato."""

    requested_model: ModelReference | None = None
    served_by: ModelReference | None = None
    models_used: list[ModelReference] = Field(default_factory=list)
    fallback_used: bool | None = None


class TaskInvocationItem(StrictBaseModel):
    """Una llamada a un modelo dentro de una tarea.

    No se recoge ningún dato nuevo: es lo que `model_invocations` ya almacenaba
    reexpuesto bajo contrato estable, fuera de la superficie de dashboard."""

    invocation_id: str
    role: str
    model: ModelReference
    status: str
    error_code: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    latency_ms: float | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Parámetros efectivos de esta invocación concreta. None en filas anteriores
    # a esta versión y en las que fallaron antes de llegar al proveedor.
    generation: EffectiveGeneration | None = None
    # Huella vigente en el momento de servir, no la del catálogo actual: si el
    # proveedor cambia a mitad de una tirada larga, tiene que verse aquí.
    execution_fingerprint: ExecutionFingerprint | None = None
    # Esta invocación no alimenta las métricas del router (ver
    # TaskCreateRequest.exclude_from_model_learning).
    excluded_from_model_learning: bool = False
    # De qué campo de la respuesta salió el texto. "reasoning_content" significa
    # que el modelo dejó el contenido vacío y se rescató su razonamiento: la
    # respuesta es válida, pero el modelo no está entregándola donde debe, y al
    # comparar modelos eso importa.
    content_source: str | None = None


class TaskInvocationsResponse(StrictBaseModel):
    task_id: str
    items: list[TaskInvocationItem] = Field(default_factory=list)


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
    # Resumen tipado de la ejecución. `result` se queda como está, sin tipo y
    # sin garantía de forma, por compatibilidad: esto no rompe a nadie, solo da
    # una vía con contrato a lo que antes había que adivinar parseando.
    execution_summary: TaskExecutionSummary | None = None


class TaskGroupStateResponse(StrictBaseModel):
    """Estado agregado de una tirada de tareas (`TaskCreateRequest.group`).

    El contrato ya dejaba etiquetar tareas con un grupo, y el broker lo usaba
    por dentro para resolver `depends_on_group`, pero desde fuera no había forma
    de preguntar por él: un cliente que encolaba doscientas tareas de una tirada
    tenía que sondear doscientos identificadores para saber si había terminado.
    Eso empujaba a mandarlas de una en una —esperando cada una antes de enviar
    la siguiente—, que es justo lo que la cola durable existe para evitar.
    """

    group: str
    total: int
    pending: int
    active: int
    completed: int
    failed: int
    cancelled: int
    # Ninguna tarea del grupo sigue viva. Es la pregunta que de verdad hace
    # quien lanza una tirada, y se responde aquí para que nadie tenga que
    # deducirla sumando contadores (y equivocarse con los estados de espera).
    finished: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


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
    # Configuración efectiva con la que se ejecutaría este modelo ahora mismo.
    # None cuando el proveedor no aporta nada (ver app.execution_fingerprint).
    execution_fingerprint: ExecutionFingerprint | None = None


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
    execution_fingerprint: ExecutionFingerprint | None = None


class ModelAvailabilityResponse(StrictBaseModel):
    checked_at: datetime
    items: list[ModelAvailabilityItem]
    counts: dict[str, int]


class MCPServerCapability(StrictBaseModel):
    """Un servidor MCP disponible, tal como lo ve una app cliente.

    Se publica la frontera de datos porque es lo que decide si ese servidor
    puede usarse en una tarea `confidential`/`local_only`, y el cliente prefiere
    saberlo antes de pedirlo que recibir un 409.
    """
    id: str
    data_boundary: Literal["local", "egress"]


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
    # Subconjunto de agent_skills que saca contenido de la tarea de esta
    # máquina. Una clasificación con frontera local (`confidential`,
    # `local_only`) las rechaza igual que rechaza un proveedor cloud, así que el
    # cliente necesita saber cuáles son antes de pedirlas, no al recibir un 422.
    agent_skills_egress: list[str]
    # Los proponentes de un mixture pueden usar esas mismas skills antes de proponer.
    proposer_skills: bool
    # Passthrough: la estrategia agent acepta tools del cliente y pausa la tarea
    # (waiting_for_tools) hasta que el cliente las resuelve vía tool_results.
    client_tool_passthrough: bool
    # Dependencias entre tareas: `group`, `depends_on` y `depends_on_group` en la
    # petición, y estado `waiting_for_dependencies` mientras espera.
    task_dependencies: bool
    # GET /api/v1/groups/{group}: recuento por estado de una tirada entera, para
    # encolarla de golpe y sondear una sola cosa en vez de N identificadores.
    task_group_status: bool = False
    # Servidores MCP disponibles para `execution.agent.mcp_servers`, con la
    # frontera de datos que declaró el operador para cada uno. Lista vacía = MCP
    # apagado o sin servidores; pedir uno daría 409.
    mcp_servers: list[MCPServerCapability] = Field(default_factory=list)
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
    # Contrato 2.9 — evaluación reproducible.
    # La petición puede fijar `generation.seed` y `generation.top_p`, y la
    # telemetría declara si llegaron a enviarse al proveedor.
    generation_determinism: bool = False
    # La petición puede marcarse con `exclude_from_model_learning`: consume
    # recursos y se contabiliza, pero no mueve las métricas del router.
    exclude_from_model_learning: bool = False
    # GET /api/v1/tasks/{id}/invocations: telemetría por invocación bajo
    # contrato v1, y bloque `execution_summary` en el estado de tarea.
    invocation_telemetry: bool = False
    # El catálogo y cada invocación declaran la configuración efectiva que
    # produce la respuesta (app.execution_fingerprint).
    execution_fingerprint: bool = False


class FileAcceptedResponse(StrictBaseModel):
    file_id: str
    status: FileStatus
    filename: str
    size_bytes: int
    sha256: str
    # False cuando la subida dedujo a un fichero ya ingerido (mismo SHA-256 y
    # una política de imágenes que sirve para la pedida).
    created: bool
    status_url: str
    # Política con la que se convierte (o se convirtió, si se reutilizó): ya
    # resuelta, para que el cliente sepa qué va a recibir sin consultar la
    # configuración del broker.
    describe_images: bool = False


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
    # Si el Markdown lleva descripciones de las figuras del documento.
    describe_images: bool = False


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
    # A qué tareas o grupo espera, y desde cuándo, con estado
    # `waiting_for_dependencies`. None en cualquier otro caso.
    dependency_wait: dict[str, Any] | None = None
    invocations: int
    tokens_input: int
    tokens_output: int
    cost_actual_usd: float
    # La tarea declaró no querer enseñarle nada al router. Se marca en el panel
    # para que nadie lea un indicador de fiabilidad sin saber qué se contó.
    excluded_from_model_learning: bool = False
    created_at: datetime
    updated_at: datetime


class DashboardTaskPage(StrictBaseModel):
    items: list[DashboardTaskItem]
    page: int
    page_size: int
    total: int
    total_pages: int


class DashboardHistoryItem(StrictBaseModel):
    """Una tarea terminada, tal como se busca en el histórico.

    No reutiliza `DashboardTaskItem` porque las preguntas son otras. Allí lo que
    importa es qué está pasando ahora (posición en cola, bloqueo de memoria, a
    qué espera); aquí lo que importa es qué pasó: **por qué falló**, cuánto
    tardó y qué modelos intervinieron —en plural, porque un mixture usa varios
    y buscar «tareas de tal modelo» tiene que encontrarlas también.
    """

    task_id: str
    kind: TaskKind = TaskKind.inference
    label: str | None = None
    request_id: str | None = None
    status: TaskStatus
    origin: str | None = None
    execution_strategy: ExecutionStrategy | None = None
    execution_preset: ExecutionPreset | None = None
    # Todos los que intervinieron, no solo el que firmó la respuesta.
    models: list[ModelReference] = Field(default_factory=list)
    fallback_used: bool | None = None
    # Código del fallo cuando `status` es `failed`. Es la columna que convierte
    # una lista de fallos en un diagnóstico: cien tareas rojas no dicen nada,
    # cien tareas rojas con el mismo código sí.
    error_code: str | None = None
    invocations: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_actual_usd: float = 0.0
    duration_seconds: float | None = None
    excluded_from_model_learning: bool = False
    created_at: datetime
    updated_at: datetime


class DashboardHistoryPage(StrictBaseModel):
    items: list[DashboardHistoryItem] = Field(default_factory=list)
    page: int
    page_size: int
    total: int
    total_pages: int


class DashboardHistoryOptions(StrictBaseModel):
    """Valores que de verdad existen en el histórico, para los desplegables.

    Se leen de los datos y no de la configuración a propósito: un proveedor
    configurado que nunca sirvió nada no ayuda a filtrar, y uno que se borró de
    la configuración pero dejó mil tareas detrás sí hace falta."""

    providers: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    origins: list[str] = Field(default_factory=list)
    # Tiradas recientes (`TaskCreateRequest.group`), de la más nueva a la más
    # antigua: es la unidad en la que se lanza una evaluación y en la que se
    # quiere revisarla después.
    groups: list[str] = Field(default_factory=list)


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
    error_code: str | None = None
    # Hash de la huella con la que se sirvió (app.execution_fingerprint). El
    # detalle completo va en /api/v1/tasks/{id}/invocations; aquí basta el hash
    # para ver de un vistazo que dos invocaciones no salieron de lo mismo.
    execution_fingerprint_hash: str | None = None
    excluded_from_model_learning: bool = False


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
    # Presupuesto que gobierna la admisión local, ya sea VRAM o pool unificado
    # (ver config.local_memory_budget_bytes). `memory_pool` dice cuál es, para
    # que el panel no etiquete como VRAM lo que es memoria compartida.
    vram_budget_bytes: int
    vram_safety_margin_bytes: int
    memory_pool: Literal["vram", "unified"] = "vram"
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

