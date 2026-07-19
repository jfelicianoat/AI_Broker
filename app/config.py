from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    workers: int = 1
    cors_enabled: bool = False
    # Token de administración: si existe (env o keyring), las mutaciones y las
    # lecturas con prompts/resultados (API y dashboard) exigen credencial admin.
    admin_token_env: str | None = Field(default="AI_BROKER_ADMIN_TOKEN", max_length=120)
    admin_keyring_service: str = Field(default="ai-broker", max_length=120)
    admin_keyring_username: str = Field(default="dashboard_admin_token", max_length=120)
    # Opt-out explícito del arranque fail-closed: permite escuchar fuera de
    # loopback sin token admin (p. ej. una demo en LAN aislada). Sin este flag,
    # el broker se niega a arrancar expuesto a la red sin credencial.
    allow_unauthenticated_lan: bool = False

    @model_validator(mode="after")
    def validate_single_worker(self) -> ServerConfig:
        # Con N workers habría N dispatchers y N recuperaciones de arranque sobre la
        # misma SQLite: re-encolado de tareas en ejecución y gasto duplicado.
        if self.workers != 1:
            raise ValueError("MVP requires server.workers to be 1 (multi-worker duplicates the dispatcher)")
        return self


class PersistenceConfig(BaseModel):
    database: str = "state/broker.db"
    journal_mode: str = "WAL"
    # Días que se conservan los eventos de tareas terminales (0 = sin poda).
    events_retention_days: int = Field(default=30, ge=0)
    # Días que se conservan los artefactos de tareas terminales (0 = nunca borrar).
    artifacts_retention_days: int = Field(default=0, ge=0)
    # Días que se conservan los ficheros ingeridos ya convertidos o fallidos
    # (0 = nunca borrar). Las conversiones en curso no se podan jamás.
    files_retention_days: int = Field(default=0, ge=0)


class ProcessingConfig(BaseModel):
    max_active_workflows: int = 1
    max_parallel_invocations: int | str = "auto"
    queue_max_size: int = 1000
    task_timeout_seconds: int = 300
    max_task_attempts: int = Field(default=3, ge=1, le=100)
    unload_after_task: bool = True
    auto_dispatch: bool = True
    dispatcher_interval_seconds: float = Field(default=0.1, gt=0, le=60)
    provider_mode: Literal["real", "bootstrap"] = "real"

    @model_validator(mode="after")
    def validate_single_workflow(self) -> ProcessingConfig:
        if self.max_active_workflows != 1:
            raise ValueError("MVP requires processing.max_active_workflows to be 1")
        if isinstance(self.max_parallel_invocations, int) and self.max_parallel_invocations < 1:
            raise ValueError("max_parallel_invocations must be 'auto' or >= 1")
        if isinstance(self.max_parallel_invocations, str) and self.max_parallel_invocations != "auto":
            raise ValueError("max_parallel_invocations must be 'auto' or >= 1")
        return self


class PromptCompressionConfig(BaseModel):
    # Reduce tokens del prompt antes de enviarlo a los LLMs (técnica caveman
    # adaptada a español). Desactivable desde el dashboard o este YAML.
    enabled: bool = True
    level: Literal["light", "medium", "aggressive"] = "medium"
    # Prompts más cortos que esto se envían tal cual: cada palabra cuenta.
    min_chars: int = Field(default=40, ge=0)


class IngestionImagesConfig(BaseModel):
    # Descripción de figuras/imágenes embebidas con un LLM de visión vía un
    # endpoint OpenAI-compatible (LM Studio, Ollama /v1...). La descripción se
    # inserta en el Markdown en la posición de la figura; los modelos solo-texto
    # reciben así el contenido visual del documento.
    enabled: bool = False
    base_url: str = Field(default="http://127.0.0.1:11434/v1", min_length=1, max_length=512)
    model: str = Field(default="", max_length=128)
    api_key_env: str | None = Field(default=None, max_length=120)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # Tope de figuras descritas por documento; el resto se marca como omitida.
    max_images: int = Field(default=20, ge=0, le=200)


class IngestionTranscriptionConfig(BaseModel):
    # Transcripción local de audio (y del audio extraído de vídeo) con
    # faster-whisper. El modelo se descarga y cachea en el primer uso.
    enabled: bool = True
    model_size: str = Field(default="small", min_length=1, max_length=64)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    # None = autodetección de idioma por Whisper.
    language: str | None = Field(default=None, max_length=8)
    # Ruta del binario ffmpeg para extraer el audio de los vídeos.
    ffmpeg_path: str = Field(default="ffmpeg", min_length=1, max_length=512)


class IngestionConfig(BaseModel):
    """Ingesta de ficheros adjuntos: conversión a Markdown antes de inferencia.

    PDF (y escaneos, con OCR por página) via Docling; Office/EPUB/HTML via
    MarkItDown; texto/código passthrough; imágenes por OCR + descripción de
    visión; audio/vídeo por transcripción Whisper. Los motores se importan en
    perezoso: si falta el paquete, el fichero falla con ENGINE_MISSING sin
    afectar al arranque del broker.
    """
    enabled: bool = True
    storage_dir: str = "state/files"
    # La subida es en streaming a disco (nunca se carga el fichero entero en
    # RAM), así que el tope real es espacio en disco, no memoria.
    max_file_mb: int = Field(default=100, ge=1, le=32768)
    max_pdf_pages: int = Field(default=500, ge=1, le=10000)
    ocr_enabled: bool = True
    ocr_languages: list[str] = Field(default_factory=lambda: ["es", "en"])
    # La conversión corre en un hilo; al agotarse el plazo la tarea de ingesta
    # se marca failed aunque el hilo siga drenando en segundo plano.
    conversion_timeout_seconds: int = Field(default=900, ge=10, le=43200)
    images: IngestionImagesConfig = Field(default_factory=IngestionImagesConfig)
    transcription: IngestionTranscriptionConfig = Field(default_factory=IngestionTranscriptionConfig)


class SandboxConfig(BaseModel):
    """Sandbox de ejecución de código (skill run_code de la estrategia agent).

    Contenedores Docker efímeros SIEMPRE sin red, sin volúmenes del host y sin
    privilegios: esas fronteras no son configurables a propósito. Requiere
    Docker Desktop en marcha; si no responde, la skill devuelve un error claro
    al modelo sin afectar al resto del broker.
    """
    enabled: bool = False
    docker_path: str = Field(default="docker", min_length=1, max_length=512)
    image: str = Field(default="python:3.12-slim", min_length=1, max_length=256)
    timeout_seconds: float = Field(default=60.0, ge=5, le=600)
    memory_mb: int = Field(default=1024, ge=64, le=16384)
    cpus: float = Field(default=2.0, gt=0, le=32)
    pids_limit: int = Field(default=256, ge=16, le=4096)
    max_output_chars: int = Field(default=8000, ge=500, le=100_000)


class ResourceConfig(BaseModel):
    local_vram_budget_gb: float = 64.0
    vram_safety_margin_gb: float = 6.0
    max_loaded_local_models: int | str = "auto"
    scheduling_policy: str = "adaptive"
    allow_execution_waves: bool = True


class ModelEnrichmentConfig(BaseModel):
    # Enriquecimiento del catálogo con metadatos de models.dev (gratuito, sin
    # clave): contexto real, precios de referencia, corte de conocimiento y
    # capacidades declaradas. Nunca pisa datos verificados por sondeo/runtime.
    # Opt-in: los tests y despliegues sin salida a internet no descargan nada.
    enabled: bool = False
    url: str = Field(default="https://models.dev/api.json", min_length=1, max_length=512)
    refresh_hours: float = Field(default=24.0, gt=0, le=24 * 30)
    timeout_seconds: float = Field(default=20.0, gt=0, le=300)


class StrategyRouterConfig(BaseModel):
    """Meta-router: elige estrategia (single/mixture/agent) para las tareas que
    llegan con `strategy: auto`. La clasificación es técnica (¿necesita datos
    actuales?, ¿es ambigua?, ¿pide visión?), nunca interpretación de dominio.

    Tres piezas activables por separado (plan escalonado):
    - heuristic_classifier (pieza 1): reglas deterministas, baratas y trazables.
    - confidence_escalation (pieza 2): single primero, escala a mixture si la
      respuesta sale de baja confianza.
    - adaptive_learning (pieza 3): afina la elección con la evidencia de casos
      previos. `record_cases` guarda esos casos desde el principio (piezas 1 y 2)
      para que la 3 no arranque de cero aunque se active más tarde.
    """
    enabled: bool = False
    heuristic_classifier: bool = True
    confidence_escalation: bool = False
    adaptive_learning: bool = False
    record_cases: bool = True
    # Umbrales del clasificador heurístico (caracteres del prompt).
    mixture_min_prompt_chars: int = Field(default=600, ge=0)
    # Por debajo de este presupuesto no se escala a mixture (es caro).
    mixture_min_budget_usd: float = Field(default=0.0, ge=0)
    # Pieza 2: si el juez puntúa la respuesta single por debajo de este umbral
    # (0-1), la tarea escala a mixture.
    escalation_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # Pieza 3: mínimo de casos por bucket para que el aprendizaje pueda decidir,
    # y umbrales de escalado/fracaso a partir de los cuales se cambia de estrategia.
    learning_min_cases: int = Field(default=5, ge=1, le=10000)
    learning_escalation_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    learning_failure_threshold: float = Field(default=0.4, ge=0.0, le=1.0)


class RoutingConfig(BaseModel):
    # Selección adaptativa: reordena los candidatos que ya pasaron los filtros
    # (proveedor permitido, cloud, capacidad, contexto) según la evidencia
    # operativa registrada en model_invocations. No altera qué modelos son
    # elegibles, solo en qué orden se prefieren.
    adaptive_selection: bool = True
    # Ventana de histórico considerada: invocaciones más antiguas no pesan.
    stats_window_days: int = Field(default=7, ge=1, le=365)
    # Por debajo de este número de invocaciones el modelo puntúa neutro
    # (arranque en frío): un modelo recién añadido nunca queda castigado.
    min_invocations: int = Field(default=3, ge=1)
    # Pesos del score multiobjetivo; se normalizan entre sí, así que solo
    # importa su proporción. success = fiabilidad (tasa de éxito suavizada),
    # latency y cost se normalizan min-max entre los candidatos de cada
    # selección.
    success_weight: float = Field(default=0.5, ge=0)
    latency_weight: float = Field(default=0.3, ge=0)
    cost_weight: float = Field(default=0.2, ge=0)

    @model_validator(mode="after")
    def validate_weights(self) -> RoutingConfig:
        if self.success_weight + self.latency_weight + self.cost_weight <= 0:
            raise ValueError("routing: al menos un peso debe ser mayor que cero")
        return self


class HealthConfig(BaseModel):
    # TTL de la caché de cada dependencia: /health y /health/ready reutilizan
    # el último resultado dentro del intervalo en vez de sondear en cada GET.
    sqlite_interval_seconds: int = 10
    local_dependencies_interval_seconds: int = 30
    # Los proveedores externos (cloud/api) se sondean con mucha menos frecuencia:
    # cada sonda es una llamada de red a un servicio de terceros.
    external_providers_interval_seconds: int = 300
    # Umbral del check de disco del volumen de la BD: por debajo, "degraded".
    disk_free_alert_gb: int = 10
    # Deadline de cada sonda de proveedor; al agotarse se reporta "unavailable"
    # sin bloquear la respuesta (los timeouts de inferencia llegan a 300 s).
    probe_timeout_seconds: float = Field(default=5.0, gt=0)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    directory: str = "logs"
    filename: str = "ai-broker.log"
    max_bytes: int = Field(default=10_485_760, ge=1024)
    backup_count: int = Field(default=5, ge=1, le=100)
    console_enabled: bool = True


class OllamaConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = Field(default=300, gt=0)
    unload_timeout_seconds: float = Field(default=10, gt=0)
    catalog_cache_seconds: float = Field(default=5.0, ge=0)


class DeepSeekConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = Field(default=300, gt=0)
    api_key_env: str = "DEEPSEEK_API_KEY"
    keyring_service: str = "ai-broker"
    keyring_username: str = "deepseek_api_key"
    default_model: str = "deepseek-chat"
    context_window: int = Field(default=64_000, gt=0)
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)
    catalog_cache_seconds: float = Field(default=30.0, ge=0)


class OpenAICompatibleModelConfig(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    context_window: int = Field(default=128_000, gt=0)
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)
    capabilities: list[str] = Field(default_factory=lambda: ["completion"])
    # "incompatible" = error definitivo del contrato (400/404/422): el modelo
    # queda vetado para cualquier uso. "error" = fallo temporal (5xx, timeout,
    # credenciales): sigue usable y se reintenta en la siguiente tanda.
    compatibility: Literal["unknown", "compatible", "incompatible", "error"] = "unknown"
    compatibility_checked_at: str | None = None
    compatibility_error: str | None = Field(default=None, max_length=2000)
    # Capacidades sondeadas contra el endpoint real (vision, json_mode, tools).
    # Clave ausente = sin sondear o sondeo no concluyente; True/False = verificado.
    features: dict[str, bool] = Field(default_factory=dict)
    features_checked_at: str | None = None


class OpenAICompatibleProviderConfig(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    enabled: bool = False
    adapter: Literal["openai_compatible"] = "openai_compatible"
    display_name: str | None = Field(default=None, max_length=120)
    base_url: str = Field(min_length=1, max_length=512)
    timeout_seconds: float = Field(default=300, gt=0)
    api_key_env: str | None = Field(default="NVIDIA_API_KEY", max_length=120)
    keyring_service: str = Field(default="ai-broker", max_length=120)
    keyring_username: str | None = Field(default=None, max_length=120)
    deployment: Literal["cloud", "api", "local"] = "cloud"
    auto_start: bool = False
    sync_models: bool = False
    catalog_cache_seconds: float = Field(default=5.0, ge=0)
    default_context_window: int = Field(default=128_000, gt=0)
    probe_max_output_tokens: int = Field(default=1, ge=1, le=1024)
    probe_delay_seconds: float = Field(default=1.0, ge=0, le=60)
    probe_max_models: int = Field(default=10, ge=1, le=1000)
    probe_skip_compatible: bool = True
    probe_skip_checked: bool = True
    # Tras verificar el chat, sondea también visión, JSON estructurado y tools
    # (3 peticiones extra de 1 token por modelo operativo).
    probe_features: bool = True
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)
    models: list[OpenAICompatibleModelConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_provider_id(self) -> OpenAICompatibleProviderConfig:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.id):
            raise ValueError("provider id only allows letters, numbers, underscore and dash")
        if self.api_key_env is not None and not self.api_key_env.strip():
            self.api_key_env = None
        if self.keyring_username is None:
            self.keyring_username = f"{self.id}_api_key"
        return self


class ProvidersConfig(BaseModel):
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    custom: list[OpenAICompatibleProviderConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_custom_ids(self) -> ProvidersConfig:
        ids = [item.id.lower() for item in self.custom]
        if len(ids) != len(set(ids)):
            raise ValueError("custom provider ids must be unique")
        reserved = {"ollama", "deepseek", "bootstrap"}
        if reserved.intersection(ids):
            raise ValueError("custom provider ids cannot use reserved provider names")
        return self


class BrokerConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    prompt_compression: PromptCompressionConfig = Field(default_factory=PromptCompressionConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    strategy_router: StrategyRouterConfig = Field(default_factory=StrategyRouterConfig)
    model_enrichment: ModelEnrichmentConfig = Field(default_factory=ModelEnrichmentConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)


def effective_max_parallel_invocations(config: BrokerConfig) -> int:
    """Capacidad paralela efectiva de inferencia.

    Fórmula única compartida por ResourceScheduler (planificación de olas) y
    RoutedModelProvider (semáforo de ejecución): si divergieran, el plan
    podría prometer un paralelismo que el router no concede, o al revés.
    """
    configured = config.processing.max_parallel_invocations
    if isinstance(configured, int):
        return configured
    usable_vram = max(
        1.0,
        config.resources.local_vram_budget_gb - config.resources.vram_safety_margin_gb,
    )
    # Estimación conservadora de arranque hasta tener telemetría real por modelo.
    return max(1, min(3, int(usable_vram // 18)))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path = "broker_config.yaml") -> BrokerConfig:
    config_path = Path(path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a YAML object")
        raw = loaded

    defaults = BrokerConfig().model_dump()
    return BrokerConfig.model_validate(_deep_merge(defaults, raw))


def save_config(config: BrokerConfig, path: str | Path = "broker_config.yaml") -> None:
    """Escritura atómica: un crash a mitad no puede dejar un YAML truncado que impida arrancar."""
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        config.model_dump(mode="json"),
        sort_keys=False,
        allow_unicode=True,
    )
    temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temp_path.write_text(payload, encoding="utf-8")
    if config_path.exists():
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")
        try:
            os.replace(config_path, backup_path)
        except OSError:
            pass
    os.replace(temp_path, config_path)

