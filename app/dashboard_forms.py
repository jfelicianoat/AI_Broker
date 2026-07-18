"""Formularios del dashboard: parseo, validación y aplicación de config.

Extraído de app.dashboard_web. Este módulo concentra el código de formularios
HTML → Pydantic que motivó la relajación de Mypy: el override de pyproject se
limita ahora a este archivo en vez de a todo el panel.
"""
from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.config import (
    BrokerConfig,
    OpenAICompatibleModelConfig,
    OpenAICompatibleProviderConfig,
)
from app.schemas import ModelReference, OutputFormat, TaskCreateRequest, is_local_deployment


class PromptTesterError(ValueError):
    pass


def _prompt_tester_defaults() -> dict[str, str]:
    return {
        "input_mode": "prompt",
        "prompt": "",
        "prompt_compression": "",
        "strategy": "single",
        "preset": "fast",
        "scheduling": "adaptive",
        "temperature": "0.3",
        "max_output_tokens": "4000",
        "output_format": "markdown",
        "json_schema": "",
        "data_classification": "internal",
        "cloud_allowed": "",
        "fallback_allowed": "",
        "timeout_seconds": "600",
        "max_cost_usd": "",
        "priority": "100",
        "single_model": "",
        "agent_model": "",
        "agent_max_iterations": "6",
        "agent_skill_web_search": "on",
        "agent_skill_fetch_url": "on",
        "agent_skill_calculator": "on",
        "agent_skill_current_datetime": "on",
        "arbiter_model": "",
        "proposer_model_1": "",
        "proposer_role_1": "generalist",
        "proposer_model_2": "",
        "proposer_role_2": "specialist",
        "proposer_model_3": "",
        "proposer_role_3": "skeptic",
        "proposer_model_4": "",
        "proposer_role_4": "analyst",
        "proposer_model_5": "",
        "proposer_role_5": "reviewer",
        "proposer_skills_enabled": "",
        "proposer_skill_web_search": "on",
        "proposer_skill_fetch_url": "on",
        "proposer_skill_calculator": "on",
        "proposer_skill_current_datetime": "on",
    }


def _config_review_items(current: BrokerConfig, updated: BrokerConfig) -> list[dict[str, str]]:
    current_data = current.model_dump(mode="json")
    updated_data = updated.model_dump(mode="json")
    checks = [
        ("processing.task_timeout_seconds", "Timeout global por tarea"),
        ("processing.queue_max_size", "Tamaño máximo de cola"),
        ("processing.max_parallel_invocations", "Máx. invocaciones paralelas slow"),
        ("resources.local_vram_budget_gb", "Presupuesto VRAM local"),
        ("resources.vram_safety_margin_gb", "Margen seguridad VRAM"),
        ("resources.max_loaded_local_models", "Máx. modelos locales cargados"),
        ("resources.allow_execution_waves", "Permitir waves"),
        ("prompt_compression.enabled", "Compresión de prompts activa"),
        ("prompt_compression.level", "Nivel de compresión de prompts"),
        ("prompt_compression.min_chars", "Mínimo de caracteres para comprimir"),
        ("providers.ollama.enabled", "Ollama activo"),
        ("providers.ollama.base_url", "Ollama base URL"),
        ("providers.ollama.timeout_seconds", "Ollama timeout"),
        ("providers.ollama.unload_timeout_seconds", "Ollama timeout de descarga"),
        ("providers.ollama.catalog_cache_seconds", "Ollama caché de catálogo"),
        ("providers.deepseek.enabled", "DeepSeek activo"),
        ("providers.deepseek.base_url", "DeepSeek base URL"),
        ("providers.deepseek.timeout_seconds", "DeepSeek timeout"),
        ("providers.deepseek.api_key_env", "DeepSeek variable API key"),
        ("providers.deepseek.default_model", "DeepSeek modelo por defecto"),
        ("providers.deepseek.context_window", "DeepSeek contexto"),
        ("providers.deepseek.input_cost_per_million", "DeepSeek coste input"),
        ("providers.deepseek.output_cost_per_million", "DeepSeek coste output"),
    ]
    changes: list[dict[str, str]] = []
    for path, label in checks:
        before = _nested_value(current_data, path)
        after = _nested_value(updated_data, path)
        if before != after:
            changes.append({"label": label, "before": _display_config_value(before), "after": _display_config_value(after)})

    current_custom = current_data.get("providers", {}).get("custom", [])
    updated_custom = updated_data.get("providers", {}).get("custom", [])
    for index in range(max(len(current_custom), len(updated_custom))):
        before_provider = current_custom[index] if index < len(current_custom) else {}
        after_provider = updated_custom[index] if index < len(updated_custom) else {}
        prefix = after_provider.get("id") or before_provider.get("id") or f"Proveedor {index + 1}"
        provider_checks = [
            ("enabled", "activo"),
            ("id", "id"),
            ("display_name", "nombre visible"),
            ("base_url", "base URL"),
            ("api_key_env", "variable API key"),
            ("deployment", "deployment"),
            ("auto_start", "autoarranque"),
            ("timeout_seconds", "timeout"),
            ("default_context_window", "contexto"),
            ("probe_max_output_tokens", "tokens probe"),
            ("probe_delay_seconds", "pausa probe"),
            ("probe_max_models", "máx. modelos por análisis"),
            ("sync_models", "sincronizar catalogo"),
            ("probe_skip_compatible", "omitir operativos"),
            ("probe_skip_checked", "omitir analizados"),
            ("probe_features", "sondear capacidades"),
        ]
        for key, label in provider_checks:
            before = before_provider.get(key)
            after = after_provider.get(key)
            if before != after:
                changes.append({
                    "label": f"{prefix}: {label}",
                    "before": _display_config_value(before),
                    "after": _display_config_value(after),
                })
        if _model_names(before_provider.get("models") or []) != _model_names(after_provider.get("models") or []):
            changes.append({
                "label": f"{prefix}: modelos",
                "before": _display_model_list(before_provider.get("models") or []),
                "after": _display_model_list(after_provider.get("models") or []),
            })
    return changes


def _nested_value(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _display_config_value(value: Any) -> str:
    if value is None or value == "":
        return "N/D"
    if isinstance(value, bool):
        return "si" if value else "no"
    return str(value)


def _model_names(models: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("name") or item.get("path") or "modelo") for item in models]


def _display_model_list(models: list[dict[str, Any]]) -> str:
    names = _model_names(models)
    if not names:
        return "0 modelos"
    preview = ", ".join(names[:3])
    suffix = f" +{len(names) - 3}" if len(names) > 3 else ""
    return f"{len(names)} modelos: {preview}{suffix}"


def _build_dashboard_config(current: BrokerConfig, form: dict[str, str]) -> BrokerConfig:
    payload = current.model_dump(mode="json")
    processing = dict(payload["processing"])
    resources = dict(payload["resources"])
    processing["task_timeout_seconds"] = _int_range_field(
        form, "task_timeout_seconds", minimum=30, maximum=86400
    )
    processing["queue_max_size"] = _int_range_field(
        form, "queue_max_size", minimum=1, maximum=100000
    )
    processing["max_parallel_invocations"] = _auto_or_int_field(
        form, "max_parallel_invocations", minimum=1, maximum=64
    )
    resources["local_vram_budget_gb"] = _float_range_field(
        form, "local_vram_budget_gb", minimum=1.0, maximum=1024.0
    )
    resources["vram_safety_margin_gb"] = _float_range_field(
        form, "vram_safety_margin_gb", minimum=0.0, maximum=512.0
    )
    resources["max_loaded_local_models"] = _auto_or_int_field(
        form, "max_loaded_local_models", minimum=1, maximum=64
    )
    resources["allow_execution_waves"] = _checked(form, "allow_execution_waves")
    if resources["vram_safety_margin_gb"] >= resources["local_vram_budget_gb"]:
        raise PromptTesterError("El margen de VRAM debe ser menor que el presupuesto total de VRAM.")
    level = form.get("prompt_compression_level", "medium").strip().lower() or "medium"
    if level not in {"light", "medium", "aggressive"}:
        raise PromptTesterError("prompt_compression_level debe ser light, medium o aggressive.")
    payload["prompt_compression"] = {
        "enabled": _checked(form, "prompt_compression_enabled"),
        "level": level,
        "min_chars": _int_range_field(form, "prompt_compression_min_chars", minimum=0, maximum=100000),
    }
    payload["processing"] = processing
    payload["resources"] = resources
    if form.get("strategy_router_mixture_min_prompt_chars") is not None:
        payload["strategy_router"] = {
            "enabled": _checked(form, "strategy_router_enabled"),
            "heuristic_classifier": _checked(form, "strategy_router_heuristic"),
            "confidence_escalation": _checked(form, "strategy_router_confidence"),
            "adaptive_learning": _checked(form, "strategy_router_learning"),
            "record_cases": _checked(form, "strategy_router_record_cases"),
            "mixture_min_prompt_chars": _int_range_field(
                form, "strategy_router_mixture_min_prompt_chars", minimum=0, maximum=100000,
            ),
            "mixture_min_budget_usd": _float_range_field(
                form, "strategy_router_mixture_min_budget_usd", minimum=0.0, maximum=1000.0,
            ),
            "escalation_min_confidence": _float_range_field(
                form, "strategy_router_escalation_min_confidence", minimum=0.0, maximum=1.0,
            ),
            "learning_min_cases": _int_range_field(
                form, "strategy_router_learning_min_cases", minimum=1, maximum=10000,
            ),
        }
    payload["providers"]["ollama"] = _parse_ollama_provider(current, form)
    payload["providers"]["deepseek"] = _parse_deepseek_provider(current, form)
    payload["providers"]["custom"] = _parse_custom_providers(current, form)
    return BrokerConfig.model_validate(payload)


def _parse_ollama_provider(current: BrokerConfig, form: dict[str, str]) -> dict[str, Any]:
    cur = current.providers.ollama.model_dump(mode="json")
    # Formularios que no incluyen la tarjeta (parciales o antiguos) no tocan
    # la config: sin este guard, un checkbox ausente desactivaría Ollama.
    if form.get("ollama_base_url") is None:
        return cur
    return {
        **cur,
        "enabled": _checked(form, "ollama_enabled"),
        "base_url": form.get("ollama_base_url", "").strip().rstrip("/") or cur["base_url"],
        "timeout_seconds": _float_field(form, "ollama_timeout_seconds", cur["timeout_seconds"]),
        "unload_timeout_seconds": _float_field(form, "ollama_unload_timeout_seconds", cur["unload_timeout_seconds"]),
        "catalog_cache_seconds": _float_field(form, "ollama_catalog_cache_seconds", cur["catalog_cache_seconds"]),
    }


def _parse_deepseek_provider(current: BrokerConfig, form: dict[str, str]) -> dict[str, Any]:
    cur = current.providers.deepseek.model_dump(mode="json")
    if form.get("deepseek_base_url") is None:
        return cur
    return {
        **cur,
        "enabled": _checked(form, "deepseek_enabled"),
        "base_url": form.get("deepseek_base_url", "").strip().rstrip("/") or cur["base_url"],
        "timeout_seconds": _float_field(form, "deepseek_timeout_seconds", cur["timeout_seconds"]),
        "api_key_env": form.get("deepseek_api_key_env", "").strip() or cur["api_key_env"],
        "default_model": form.get("deepseek_default_model", "").strip() or cur["default_model"],
        "context_window": _int_field(form, "deepseek_context_window", cur["context_window"]),
        "input_cost_per_million": _float_field(form, "deepseek_input_cost_per_million", cur["input_cost_per_million"]),
        "output_cost_per_million": _float_field(form, "deepseek_output_cost_per_million", cur["output_cost_per_million"]),
    }


def _apply_config_update(target: BrokerConfig, updated: BrokerConfig) -> None:
    target.processing = updated.processing
    target.prompt_compression = updated.prompt_compression
    target.resources = updated.resources
    target.strategy_router = updated.strategy_router
    target.providers = updated.providers



_CUSTOM_PROVIDER_FIELD_PATTERN = re.compile(r"^custom_provider_(\d+)_")


def _custom_provider_form_indexes(form: dict[str, str]) -> list[int]:
    """Índices de proveedor presentes en el formulario: el número de
    proveedores es dinámico (el combo permite dar de alta los que hagan falta)."""
    indexes = {
        int(match.group(1))
        for key in form
        if (match := _CUSTOM_PROVIDER_FIELD_PATTERN.match(key))
    }
    return sorted(indexes)


def _parse_custom_providers(current: BrokerConfig, form: dict[str, str]) -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for index in _custom_provider_form_indexes(form):
        # Baja explícita desde el botón "Eliminar proveedor": el proveedor se
        # omite del YAML resultante, modelos incluidos.
        if _checked(form, f"custom_provider_{index}_delete"):
            continue
        provider_id = form.get(f"custom_provider_{index}_id", "").strip()
        base_url = form.get(f"custom_provider_{index}_base_url", "").strip()
        models_field = form.get(f"custom_provider_{index}_models")
        models_text = (models_field or "").strip()
        enabled = _checked(form, f"custom_provider_{index}_enabled")
        if not provider_id and not base_url and not models_text:
            continue
        if not provider_id:
            raise PromptTesterError(f"Proveedor custom {index}: indica un id.")
        if not base_url:
            raise PromptTesterError(f"Proveedor custom {provider_id}: indica base_url.")
        previous = _find_custom_provider(current, provider_id)
        previous_models = {item.name: item for item in previous.models} if previous is not None else {}
        if models_field is None:
            # El formulario ya no lista los modelos: se conservan los del YAML
            # (los mantienen el analizador y la sincronización de catálogo).
            models = list(previous.models) if previous is not None else []
        else:
            models = _parse_custom_provider_models(provider_id, models_text, previous_models)
        sync_models = _checked(form, f"custom_provider_{index}_sync_models")
        if enabled and not sync_models and not models:
            raise PromptTesterError(
                f"Proveedor custom {provider_id}: anade al menos un modelo o activa sincronizar catalogo."
            )
        providers.append({
            "id": provider_id,
            "enabled": enabled,
            "adapter": "openai_compatible",
            "display_name": form.get(f"custom_provider_{index}_display_name", "").strip() or None,
            "base_url": base_url.rstrip("/"),
            "timeout_seconds": _float_field(form, f"custom_provider_{index}_timeout_seconds", 300.0),
            "api_key_env": form.get(f"custom_provider_{index}_api_key_env", "").strip() or None,
            "keyring_service": "ai-broker",
            "keyring_username": form.get(f"custom_provider_{index}_keyring_username", "").strip() or None,
            "deployment": form.get(f"custom_provider_{index}_deployment", "cloud") or "cloud",
            "auto_start": _checked(form, f"custom_provider_{index}_auto_start"),
            "sync_models": sync_models,
            "default_context_window": _int_field(form, f"custom_provider_{index}_default_context_window", 128000),
            "probe_max_output_tokens": _int_field(form, f"custom_provider_{index}_probe_max_output_tokens", 1),
            "probe_delay_seconds": _float_field(form, f"custom_provider_{index}_probe_delay_seconds", 0.25),
            "probe_max_models": _int_field(form, f"custom_provider_{index}_probe_max_models", 50),
            "probe_skip_compatible": _checked(form, f"custom_provider_{index}_probe_skip_compatible"),
            "probe_skip_checked": _checked(form, f"custom_provider_{index}_probe_skip_checked"),
            "probe_features": _checked(form, f"custom_provider_{index}_probe_features"),
            "input_cost_per_million": _float_field(form, f"custom_provider_{index}_input_cost_per_million", 0.0),
            "output_cost_per_million": _float_field(form, f"custom_provider_{index}_output_cost_per_million", 0.0),
            "models": [item.model_dump(mode="json") for item in models],
        })
    return providers


def _parse_custom_provider_models(
    provider_id: str,
    models_text: str,
    previous_models: dict[str, OpenAICompatibleModelConfig] | None = None,
) -> list[OpenAICompatibleModelConfig]:
    if not models_text:
        return []
    previous_models = previous_models or {}
    models: list[OpenAICompatibleModelConfig] = []
    for line_number, raw_line in enumerate(models_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        try:
            previous = previous_models.get(parts[0])
            models.append(OpenAICompatibleModelConfig(
                name=parts[0],
                context_window=int(parts[1]) if len(parts) > 1 and parts[1] else 128000,
                input_cost_per_million=float(parts[2]) if len(parts) > 2 and parts[2] else 0.0,
                output_cost_per_million=float(parts[3]) if len(parts) > 3 and parts[3] else 0.0,
                compatibility=previous.compatibility if previous is not None else "unknown",
                compatibility_checked_at=previous.compatibility_checked_at if previous is not None else None,
                compatibility_error=previous.compatibility_error if previous is not None else None,
                features=dict(previous.features) if previous is not None else {},
                features_checked_at=previous.features_checked_at if previous is not None else None,
            ))
        except (ValueError, ValidationError) as error:
            raise PromptTesterError(
                f"Proveedor custom {provider_id}: modelo invalido en linea {line_number}. "
                "Usa nombre|contexto|coste_input_millon|coste_output_millon."
            ) from error
    return models


def _find_custom_provider(
    config: BrokerConfig,
    provider_id: str,
) -> OpenAICompatibleProviderConfig | None:
    return next(
        (item for item in config.providers.custom if item.id.lower() == provider_id.lower()),
        None,
    )


def _apply_probe_results(
    config: BrokerConfig,
    provider_id: str,
    results: list[dict[str, Any]],
    catalog: list[dict[str, Any]] | None = None,
) -> None:
    provider_config = _find_custom_provider(config, provider_id)
    if provider_config is None:
        raise PromptTesterError(f"Proveedor custom no encontrado: {provider_id}")
    existing = {item.name: item for item in provider_config.models}
    updated_by_name: dict[str, OpenAICompatibleModelConfig] = dict(existing)
    for entry in catalog or []:
        name = str(entry.get("name") or entry.get("id") or "")
        if not name:
            continue
        previous = updated_by_name.get(name)
        inferred_capabilities = list(entry.get("capabilities") or [])
        if previous is not None:
            updated_by_name[name] = OpenAICompatibleModelConfig(
                name=name,
                context_window=previous.context_window,
                input_cost_per_million=previous.input_cost_per_million,
                output_cost_per_million=previous.output_cost_per_million,
                capabilities=inferred_capabilities or list(previous.capabilities),
                compatibility=previous.compatibility,
                compatibility_checked_at=previous.compatibility_checked_at,
                compatibility_error=previous.compatibility_error,
                features=dict(previous.features),
                features_checked_at=previous.features_checked_at,
            )
            continue
        updated_by_name[name] = OpenAICompatibleModelConfig(
            name=name,
            context_window=int(entry.get("context_window") or provider_config.default_context_window),
            input_cost_per_million=provider_config.input_cost_per_million,
            output_cost_per_million=provider_config.output_cost_per_million,
            capabilities=inferred_capabilities or ["completion"],
            compatibility=str(entry.get("compatibility") or "unknown"),
            compatibility_checked_at=entry.get("compatibility_checked_at"),
            compatibility_error=entry.get("compatibility_error"),
        )
    for result in results:
        name = str(result["name"])
        previous = updated_by_name.get(name)
        result_features = result.get("features")
        updated_by_name[name] = OpenAICompatibleModelConfig(
            name=name,
            context_window=previous.context_window if previous is not None else provider_config.default_context_window,
            input_cost_per_million=(
                previous.input_cost_per_million if previous is not None else provider_config.input_cost_per_million
            ),
            output_cost_per_million=(
                previous.output_cost_per_million if previous is not None else provider_config.output_cost_per_million
            ),
            capabilities=list(previous.capabilities) if previous is not None else ["completion"],
            compatibility=str(result.get("compatibility") or "unknown"),
            compatibility_checked_at=result.get("compatibility_checked_at"),
            compatibility_error=result.get("compatibility_error"),
            # El sondeo de capacidades puede no haberse ejecutado (modelo no
            # operativo, rate limit): en ese caso se conserva lo ya verificado.
            features=(
                dict(result_features)
                if isinstance(result_features, dict)
                else (dict(previous.features) if previous is not None else {})
            ),
            features_checked_at=(
                result.get("features_checked_at")
                if isinstance(result_features, dict)
                else (previous.features_checked_at if previous is not None else None)
            ),
        )
    provider_config.models = list(updated_by_name.values())


def _build_prompt_tester_request(form: dict[str, str]) -> TaskCreateRequest:
    prompt = form.get("prompt", "")
    if not prompt.strip():
        raise PromptTesterError("El prompt no puede estar vacio.")
    input_mode = form.get("input_mode", "prompt")
    if input_mode == "json":
        try:
            json.loads(prompt)
        except json.JSONDecodeError as error:
            raise PromptTesterError(
                f"JSON de entrada invalido: linea {error.lineno}, columna {error.colno}."
            ) from error
    elif input_mode != "prompt":
        raise PromptTesterError("Modo de entrada no soportado.")

    prompt_compression = form.get("prompt_compression", "").strip()
    if prompt_compression not in {"", "off", "light", "medium", "aggressive"}:
        raise PromptTesterError(
            "Compresión de prompt no soportada: usa la global, off, light, medium o aggressive."
        )

    output_format = form.get("output_format", "markdown")
    output: dict[str, Any] = {"format": output_format, "language": "es"}
    json_schema_text = form.get("json_schema", "").strip()
    if output_format == "json":
        if not json_schema_text:
            raise PromptTesterError("El formato de salida JSON requiere JSON Schema.")
        try:
            output["json_schema"] = json.loads(json_schema_text)
        except json.JSONDecodeError as error:
            raise PromptTesterError(
                f"JSON Schema invalido: linea {error.lineno}, columna {error.colno}."
            ) from error

    strategy = form.get("strategy", "single")
    cloud_allowed = _checked(form, "cloud_allowed")
    fallback_allowed = _checked(form, "fallback_allowed")
    if strategy == "single":
        target = _parse_model_reference(form.get("single_model", ""))
        _ensure_cloud_allowed([target], cloud_allowed)
        execution = {
            "strategy": "single",
            "preset": "fast",
            "scheduling": "sequential",
            "timeout_seconds": _int_field(form, "timeout_seconds", 600),
        }
        model_requirements = {
            "preferred_model": target.model,
            "target_model": target.model_dump(mode="json"),
            "fallback_allowed": fallback_allowed,
            "cloud_allowed": cloud_allowed,
            "allowed_providers": [target.provider],
            "max_cost_usd": _optional_float(form, "max_cost_usd"),
        }
    elif strategy == "mixture_of_agents":
        preset = form.get("preset", "fast")
        if preset not in {"fast", "slow"}:
            raise PromptTesterError("El probador solo admite mixture_of_agents/fast o slow.")
        proposers = _parse_proposers(form)
        arbiter = _parse_model_reference(form.get("arbiter_model", ""))
        selected_models = proposers + [arbiter]
        _ensure_cloud_allowed(selected_models, cloud_allowed)
        proposer_skills: list[str] = []
        if _checked(form, "proposer_skills_enabled"):
            proposer_skills = [
                name for name, field in (
                    ("web_search", "proposer_skill_web_search"),
                    ("fetch_url", "proposer_skill_fetch_url"),
                    ("calculator", "proposer_skill_calculator"),
                    ("current_datetime", "proposer_skill_current_datetime"),
                )
                if _checked(form, field)
            ]
            if not proposer_skills:
                raise PromptTesterError("Activa al menos una skill para los proponentes, o desmarca la opción.")
        execution = {
            "strategy": "mixture_of_agents",
            "preset": preset,
            "scheduling": "sequential" if preset == "fast" else form.get("scheduling", "adaptive"),
            "max_proposers": len(proposers),
            "max_judges": 1,
            "max_rounds": 1,
            "timeout_seconds": _int_field(form, "timeout_seconds", 600),
            "proposer_skills": proposer_skills,
            "selection": {
                "mode": "manual",
                "allow_substitution": False,
                "proposer_count": len(proposers),
                "proposers": [item.model_dump(mode="json") for item in proposers],
                "arbiter": arbiter.model_dump(mode="json"),
            },
        }
        model_requirements = {
            "fallback_allowed": fallback_allowed,
            "cloud_allowed": cloud_allowed,
            "allowed_providers": sorted({item.provider for item in selected_models}),
            "max_cost_usd": _optional_float(form, "max_cost_usd"),
        }
    elif strategy == "agent":
        target = _parse_model_reference(form.get("agent_model", ""))
        _ensure_cloud_allowed([target], cloud_allowed)
        skills = [
            name for name, field in (
                ("web_search", "agent_skill_web_search"),
                ("fetch_url", "agent_skill_fetch_url"),
                ("calculator", "agent_skill_calculator"),
                ("current_datetime", "agent_skill_current_datetime"),
            )
            if _checked(form, field)
        ]
        if not skills:
            raise PromptTesterError("El agente necesita al menos una skill activa.")
        execution = {
            "strategy": "agent",
            "preset": "fast",
            "scheduling": "sequential",
            "timeout_seconds": _int_field(form, "timeout_seconds", 600),
            "agent": {
                "skills": skills,
                "max_iterations": _int_range_field(form, "agent_max_iterations", minimum=1, maximum=20),
            },
        }
        model_requirements = {
            "preferred_model": target.model,
            "target_model": target.model_dump(mode="json"),
            "fallback_allowed": fallback_allowed,
            "cloud_allowed": cloud_allowed,
            "allowed_providers": [target.provider],
            "max_cost_usd": _optional_float(form, "max_cost_usd"),
        }
    else:
        raise PromptTesterError("Estrategia no soportada.")

    return TaskCreateRequest.model_validate({
        "idempotency_key": f"prompt-tester:{uuid4().hex}",
        "request_id": f"prompt-tester-{uuid4().hex[:12]}",
        "prompt_compression": prompt_compression or None,
        "content": {
            "prompt": prompt,
            "metadata": {
                "origin": "prompt_tester",
                "input_mode": input_mode,
            },
        },
        "output": output,
        "generation": {
            "temperature": _float_field(form, "temperature", 0.3),
            "max_output_tokens": _int_field(form, "max_output_tokens", 4000),
        },
        "model_requirements": model_requirements,
        "execution": execution,
        "risk": {
            "data_classification": form.get("data_classification", "internal"),
            "human_review_required": False,
        },
        "priority": _int_field(form, "priority", 100),
    })


def _prompt_tester_impact(payload: TaskCreateRequest) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    execution = data.get("execution") or {}
    model_requirements = data.get("model_requirements") or {}
    generation = data.get("generation") or {}
    strategy = execution.get("strategy", "single")
    preset = execution.get("preset", "fast")
    if strategy == "mixture_of_agents":
        selection = execution.get("selection") if isinstance(execution.get("selection"), dict) else {}
        proposers = selection.get("proposers") if isinstance(selection.get("proposers"), list) else []
        expected_invocations = len(proposers) + int(execution.get("max_judges") or 1)
        selected_models = proposers + ([selection.get("arbiter")] if selection.get("arbiter") else [])
    elif strategy == "agent":
        agent = execution.get("agent") if isinstance(execution.get("agent"), dict) else {}
        expected_invocations = int(agent.get("max_iterations") or 1)
        selected_models = [model_requirements.get("target_model")]
    else:
        expected_invocations = 1
        selected_models = [model_requirements.get("target_model")]
    selected_models = [item for item in selected_models if isinstance(item, dict)]
    cloud_models = [
        f"{item.get('provider')}/{item.get('deployment')}/{item.get('model')}"
        for item in selected_models
        if str(item.get("deployment") or "").lower() == "cloud"
    ]
    return {
        "strategy": f"{strategy}/{preset}",
        "expected_invocations": expected_invocations,
        "scheduling": execution.get("scheduling", "sequential"),
        "timeout_seconds": execution.get("timeout_seconds"),
        "max_output_tokens": generation.get("max_output_tokens"),
        "cloud_allowed": bool(model_requirements.get("cloud_allowed")),
        "fallback_allowed": bool(model_requirements.get("fallback_allowed")),
        "cloud_models": cloud_models,
    }


def _prompt_tester_selected_models(payload: TaskCreateRequest) -> list[dict[str, Any]]:
    """Referencias de modelo (dicts provider/deployment/model) que ejecutará la tarea."""
    data = payload.model_dump(mode="json")
    execution = data.get("execution") or {}
    if execution.get("strategy") == "mixture_of_agents":
        selection = execution.get("selection") if isinstance(execution.get("selection"), dict) else {}
        proposers = selection.get("proposers") if isinstance(selection.get("proposers"), list) else []
        selected = list(proposers) + ([selection.get("arbiter")] if selection.get("arbiter") else [])
    else:
        model_requirements = data.get("model_requirements") or {}
        selected = [model_requirements.get("target_model")]
    return [item for item in selected if isinstance(item, dict)]


def _model_tools_unsupported(reference: ModelReference, catalog: list[dict[str, Any]]) -> bool:
    """True solo si el sondeo/runtime/catálogo verificó que NO soporta tools.
    'Sin verificar' devuelve False: no hay evidencia de fallo, se deja intentar."""
    entry = next(
        (
            item for item in catalog
            if str(item.get("name")) == reference.model
            and str(item.get("provider") or "").lower() == reference.provider.lower()
        ),
        None,
    )
    if entry is None:
        return False
    features = entry.get("features") or {}
    catalog_info = entry.get("catalog") or {}
    tools_support = features.get("tools")
    if tools_support is None and isinstance(catalog_info.get("tools"), bool):
        tools_support = catalog_info.get("tools")
    return tools_support is False


def _prompt_tester_agent_precheck(payload: TaskCreateRequest, catalog: list[dict[str, Any]]) -> None:
    """Rechaza antes de encolar tareas cuyo modelo con tools requeridas no las
    soporte (estrategia agent, o proponentes de mixture con skills)."""
    strategy = payload.execution.strategy.value
    if strategy == "agent":
        target = payload.model_requirements.target_model
        if target is not None and _model_tools_unsupported(target, catalog):
            raise PromptTesterError(
                f"El modelo {target.provider}/{target.model} no soporta tools (function calling); "
                "elige otro modelo con tools o usa la estrategia Modelo único."
            )
    elif strategy == "mixture_of_agents" and payload.execution.proposer_skills:
        selection = payload.execution.selection
        for proposer in selection.proposers:
            if _model_tools_unsupported(proposer, catalog):
                raise PromptTesterError(
                    f"El proponente {proposer.provider}/{proposer.model} no soporta tools; "
                    "elige proponentes con tools o desactiva las herramientas de los proponentes."
                )


def _prompt_tester_feature_warnings(
    payload: TaskCreateRequest,
    catalog: list[dict[str, Any]],
) -> list[str]:
    """Avisos no bloqueantes: la petición exige una capacidad que el sondeo
    verificó como no soportada en algún modelo seleccionado. Sin sondeo
    (clave ausente en features) no se avisa: solo cuenta el negativo probado."""
    if payload.output.format != OutputFormat.json:
        return []
    entries = {
        (str(item.get("provider") or "").lower(), str(item.get("name") or "")): item
        for item in catalog
    }
    warnings: list[str] = []
    seen: set[str] = set()
    for item in _prompt_tester_selected_models(payload):
        label = f"{item.get('provider')}/{item.get('model')}"
        if label in seen:
            continue
        entry = entries.get((str(item.get("provider") or "").lower(), str(item.get("model") or "")))
        features = (entry or {}).get("features") or {}
        if features.get("json_mode") is False:
            seen.add(label)
            warnings.append(
                f"El modelo {label} no superó el sondeo de JSON estructurado: "
                "la salida JSON puede llegar como texto plano o fallar."
            )
    return warnings


def _parse_proposers(form: dict[str, str]) -> list[ModelReference]:
    proposers: list[ModelReference] = []
    for index in range(1, 6):
        raw = form.get(f"proposer_model_{index}", "")
        if not raw:
            continue
        role = form.get(f"proposer_role_{index}", "").strip() or f"proposer_{index}"
        proposers.append(_parse_model_reference(raw).model_copy(update={"role": role}))
    if not proposers:
        raise PromptTesterError("Selecciona al menos un proponente.")
    return proposers


def _parse_model_reference(raw: str) -> ModelReference:
    if not raw:
        raise PromptTesterError("Selecciona un modelo del catalogo.")
    try:
        payload = json.loads(raw)
        return ModelReference(
            provider=payload["provider"],
            deployment=payload["deployment"],
            model=payload["model"],
        )
    except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
        raise PromptTesterError("Referencia de modelo invalida.") from error


def _ensure_cloud_allowed(models: list[ModelReference], cloud_allowed: bool) -> None:
    if cloud_allowed:
        return
    blocked = [
        f"{item.provider}/{item.deployment}/{item.model}"
        for item in models
        if not is_local_deployment(item.deployment)
    ]
    if blocked:
        raise PromptTesterError(
            "Marca Permitir cloud o selecciona solo modelos locales: " + ", ".join(blocked)
        )


def _checked(form: dict[str, str], key: str) -> bool:
    return form.get(key) in {"1", "true", "on", "yes"}


def _int_field(form: dict[str, str], key: str, default: int) -> int:
    raw = form.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise PromptTesterError(f"{key} debe ser un numero entero.") from error


def _int_range_field(form: dict[str, str], key: str, *, minimum: int, maximum: int) -> int:
    value = _int_field(form, key, minimum)
    if value < minimum or value > maximum:
        raise PromptTesterError(f"{key} debe estar entre {minimum} y {maximum}.")
    return value


def _float_field(form: dict[str, str], key: str, default: float) -> float:
    raw = form.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise PromptTesterError(f"{key} debe ser numerico.") from error


def _float_range_field(form: dict[str, str], key: str, *, minimum: float, maximum: float) -> float:
    value = _float_field(form, key, minimum)
    if value < minimum or value > maximum:
        raise PromptTesterError(f"{key} debe estar entre {minimum:g} y {maximum:g}.")
    return value


def _auto_or_int_field(form: dict[str, str], key: str, *, minimum: int, maximum: int) -> int | str:
    raw = form.get(key, "").strip().lower()
    if not raw or raw == "auto":
        return "auto"
    try:
        value = int(raw)
    except ValueError as error:
        raise PromptTesterError(f"{key} debe ser 'auto' o un numero entero.") from error
    if value < minimum or value > maximum:
        raise PromptTesterError(f"{key} debe ser 'auto' o estar entre {minimum} y {maximum}.")
    return value


def _optional_float(form: dict[str, str], key: str) -> float | None:
    raw = form.get(key, "").strip()
    if not raw:
        return None
    return _float_field(form, key, 0.0)


def _validation_messages(error: ValidationError) -> list[str]:
    messages = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        messages.append(f"{location}: {item.get('msg')}")
    return messages


