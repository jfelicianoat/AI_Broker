from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    workers: int = 1
    cors_enabled: bool = False


class PersistenceConfig(BaseModel):
    database: str = "state/broker.db"
    journal_mode: str = "WAL"


class ProcessingConfig(BaseModel):
    max_active_workflows: int = 1
    max_parallel_invocations: int | str = "auto"
    queue_max_size: int = 1000
    task_timeout_seconds: int = 300
    unload_after_task: bool = True
    auto_dispatch: bool = True
    dispatcher_interval_seconds: float = Field(default=0.1, gt=0, le=60)
    provider_mode: Literal["real", "bootstrap"] = "real"

    @model_validator(mode="after")
    def validate_single_workflow(self) -> "ProcessingConfig":
        if self.max_active_workflows != 1:
            raise ValueError("MVP requires processing.max_active_workflows to be 1")
        if isinstance(self.max_parallel_invocations, int) and self.max_parallel_invocations < 1:
            raise ValueError("max_parallel_invocations must be 'auto' or >= 1")
        if isinstance(self.max_parallel_invocations, str) and self.max_parallel_invocations != "auto":
            raise ValueError("max_parallel_invocations must be 'auto' or >= 1")
        return self


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


class ProvidersConfig(BaseModel):
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)


class BrokerConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
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

