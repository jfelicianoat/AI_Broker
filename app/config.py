from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    workers: int = 1
    cors_enabled: bool = False
    # Token de administración del dashboard: si existe (env o keyring), las
    # acciones mutables de /dashboard/actions/* exigen sesión admin.
    admin_token_env: str | None = Field(default="AI_BROKER_ADMIN_TOKEN", max_length=120)
    admin_keyring_service: str = Field(default="ai-broker", max_length=120)
    admin_keyring_username: str = Field(default="dashboard_admin_token", max_length=120)

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


class ResourceConfig(BaseModel):
    local_vram_budget_gb: float = 64.0
    vram_safety_margin_gb: float = 6.0
    max_loaded_local_models: int | str = "auto"
    scheduling_policy: str = "adaptive"
    allow_execution_waves: bool = True


class HealthConfig(BaseModel):
    sqlite_interval_seconds: int = 10
    local_dependencies_interval_seconds: int = 30
    external_providers_interval_seconds: int = 300
    disk_free_alert_gb: int = 10


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


class HuggingFaceLocalModelConfig(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)
    context_window: int = Field(default=32_768, gt=0)
    capabilities: list[str] = Field(default_factory=lambda: ["completion"])
    device: str | None = Field(default=None, max_length=80)
    dtype: str | None = Field(default=None, max_length=40)
    trust_remote_code: bool | None = None
    compatibility: Literal["unknown", "compatible", "incompatible"] = "compatible"
    compatibility_checked_at: str | None = None
    compatibility_error: str | None = Field(default=None, max_length=2000)


class HuggingFaceLocalConfig(BaseModel):
    enabled: bool = False
    models_dir: str = ".local/models"
    timeout_seconds: float = Field(default=300, gt=0)
    default_context_window: int = Field(default=32_768, gt=0)
    default_device: str = Field(default="auto", max_length=80)
    default_dtype: str | None = Field(default=None, max_length=40)
    trust_remote_code: bool = False
    models: list[HuggingFaceLocalModelConfig] = Field(default_factory=list)


class OpenAICompatibleModelConfig(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    context_window: int = Field(default=128_000, gt=0)
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)
    capabilities: list[str] = Field(default_factory=lambda: ["completion"])
    compatibility: Literal["unknown", "compatible", "incompatible"] = "unknown"
    compatibility_checked_at: str | None = None
    compatibility_error: str | None = Field(default=None, max_length=2000)


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
    huggingface_local: HuggingFaceLocalConfig = Field(default_factory=HuggingFaceLocalConfig)
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
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)


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

