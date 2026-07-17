"""Cobertura del parseo de formularios del dashboard (dashboard_forms).

Los formularios HTML llegan como str y cada error debe convertirse en un
PromptTesterError legible, nunca en un 500: estos tests fijan ese contrato.
"""
import pytest

from app.config import BrokerConfig, OpenAICompatibleModelConfig, OpenAICompatibleProviderConfig, ProvidersConfig
from app.dashboard_forms import (
    PromptTesterError,
    _apply_probe_results,
    _auto_or_int_field,
    _checked,
    _custom_provider_form_indexes,
    _ensure_cloud_allowed,
    _find_custom_provider,
    _float_field,
    _float_range_field,
    _int_field,
    _int_range_field,
    _optional_float,
    _parse_custom_provider_models,
    _parse_custom_providers,
    _parse_deepseek_provider,
    _parse_model_reference,
    _parse_ollama_provider,
    _parse_proposers,
    _validation_messages,
)
from app.schemas import ModelReference


def test_checked_accepts_common_truthy_form_values() -> None:
    assert _checked({"x": "on"}, "x") is True
    assert _checked({"x": "1"}, "x") is True
    assert _checked({"x": "off"}, "x") is False
    assert _checked({}, "x") is False


def test_int_and_float_fields_reject_non_numeric_input() -> None:
    assert _int_field({"n": " 7 "}, "n", 1) == 7
    assert _int_field({}, "n", 42) == 42
    with pytest.raises(PromptTesterError, match="numero entero"):
        _int_field({"n": "siete"}, "n", 1)

    assert _float_field({"t": "0.5"}, "t", 1.0) == 0.5
    assert _float_field({}, "t", 1.5) == 1.5
    with pytest.raises(PromptTesterError, match="numerico"):
        _float_field({"t": "medio"}, "t", 1.0)


def test_range_fields_enforce_bounds() -> None:
    assert _int_range_field({"n": "3"}, "n", minimum=1, maximum=5) == 3
    with pytest.raises(PromptTesterError, match="entre 1 y 5"):
        _int_range_field({"n": "9"}, "n", minimum=1, maximum=5)

    assert _float_range_field({"t": "0.7"}, "t", minimum=0.0, maximum=2.0) == 0.7
    with pytest.raises(PromptTesterError, match="entre 0 y 2"):
        _float_range_field({"t": "3.5"}, "t", minimum=0.0, maximum=2.0)


def test_auto_or_int_field_accepts_auto_and_validates_numbers() -> None:
    assert _auto_or_int_field({}, "p", minimum=1, maximum=8) == "auto"
    assert _auto_or_int_field({"p": "AUTO"}, "p", minimum=1, maximum=8) == "auto"
    assert _auto_or_int_field({"p": "4"}, "p", minimum=1, maximum=8) == 4
    with pytest.raises(PromptTesterError, match="'auto' o un numero"):
        _auto_or_int_field({"p": "muchos"}, "p", minimum=1, maximum=8)
    with pytest.raises(PromptTesterError, match="'auto' o estar entre"):
        _auto_or_int_field({"p": "99"}, "p", minimum=1, maximum=8)


def test_optional_float_returns_none_for_empty_value() -> None:
    assert _optional_float({}, "coste") is None
    assert _optional_float({"coste": "0.25"}, "coste") == 0.25


def test_parse_model_reference_requires_complete_json() -> None:
    valid = _parse_model_reference('{"provider": "ollama", "deployment": "local", "model": "llama3"}')
    assert valid.model == "llama3"
    for raw in ("", "no-json", '{"provider": "ollama"}'):
        with pytest.raises(PromptTesterError):
            _parse_model_reference(raw)


def test_parse_proposers_requires_at_least_one_and_assigns_roles() -> None:
    form = {
        "proposer_model_1": '{"provider": "ollama", "deployment": "local", "model": "a"}',
        "proposer_role_1": "generalista",
        "proposer_model_3": '{"provider": "ollama", "deployment": "local", "model": "b"}',
    }
    proposers = _parse_proposers(form)
    assert [(p.model, p.role) for p in proposers] == [("a", "generalista"), ("b", "proposer_3")]
    with pytest.raises(PromptTesterError, match="al menos un proponente"):
        _parse_proposers({})


def test_ensure_cloud_allowed_blocks_external_models_only() -> None:
    local = ModelReference(provider="ollama", deployment="local", model="a")
    remote = ModelReference(provider="nvidia", deployment="api", model="b")
    _ensure_cloud_allowed([local, remote], cloud_allowed=True)
    _ensure_cloud_allowed([local], cloud_allowed=False)
    with pytest.raises(PromptTesterError, match="nvidia/api/b"):
        _ensure_cloud_allowed([local, remote], cloud_allowed=False)


def test_parse_custom_provider_models_preserves_previous_compatibility() -> None:
    previous = {
        "conocido": OpenAICompatibleModelConfig(name="conocido", compatibility="compatible"),
    }
    models = _parse_custom_provider_models(
        "lmstudio",
        "conocido|8192|0.1|0.2\nnuevo\n",
        previous_models=previous,
    )
    assert models[0].compatibility == "compatible"
    assert models[0].input_cost_per_million == 0.1
    assert models[1].compatibility == "unknown"
    assert models[1].context_window == 128000
    assert _parse_custom_provider_models("lmstudio", "") == []
    with pytest.raises(PromptTesterError, match="modelo invalido en linea 1"):
        _parse_custom_provider_models("lmstudio", "modelo|no-numerico")


def _config_with_custom() -> BrokerConfig:
    return BrokerConfig(
        providers=ProvidersConfig(
            custom=[
                OpenAICompatibleProviderConfig(
                    id="lmstudio",
                    enabled=True,
                    base_url="http://127.0.0.1:1234/v1",
                    deployment="local",
                    models=[OpenAICompatibleModelConfig(name="previo", context_window=4096)],
                )
            ]
        )
    )


def test_find_custom_provider_is_case_insensitive() -> None:
    config = _config_with_custom()
    assert _find_custom_provider(config, "LMSTUDIO") is not None
    assert _find_custom_provider(config, "inexistente") is None


def test_parse_ollama_provider_updates_only_when_card_is_posted() -> None:
    config = BrokerConfig()
    assert config.providers.ollama.enabled is True

    # Sin la tarjeta en el formulario (formularios parciales) no se toca nada:
    # un checkbox ausente no debe desactivar Ollama.
    untouched = _parse_ollama_provider(config, {"task_timeout_seconds": "300"})
    assert untouched["enabled"] is True
    assert untouched["base_url"] == "http://127.0.0.1:11434"

    updated = _parse_ollama_provider(config, {
        "ollama_base_url": "http://127.0.0.1:11434/",
        "ollama_timeout_seconds": "120",
        "ollama_unload_timeout_seconds": "5",
        "ollama_catalog_cache_seconds": "10",
    })
    assert updated["enabled"] is False
    assert updated["base_url"] == "http://127.0.0.1:11434"
    assert updated["timeout_seconds"] == 120.0


def test_parse_deepseek_provider_updates_only_when_card_is_posted() -> None:
    config = BrokerConfig()
    untouched = _parse_deepseek_provider(config, {})
    assert untouched["enabled"] is False
    assert untouched["default_model"] == "deepseek-chat"

    updated = _parse_deepseek_provider(config, {
        "deepseek_enabled": "on",
        "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_api_key_env": "MI_DEEPSEEK_KEY",
        "deepseek_default_model": "deepseek-reasoner",
        "deepseek_context_window": "128000",
        "deepseek_timeout_seconds": "300",
        "deepseek_input_cost_per_million": "0.27",
        "deepseek_output_cost_per_million": "1.1",
    })
    assert updated["enabled"] is True
    assert updated["api_key_env"] == "MI_DEEPSEEK_KEY"
    assert updated["default_model"] == "deepseek-reasoner"
    assert updated["input_cost_per_million"] == 0.27
    # Los campos que no viajan en el formulario conservan su valor.
    assert updated["keyring_username"] == "deepseek_api_key"


def test_custom_provider_form_indexes_are_dynamic() -> None:
    form = {
        "custom_provider_1_id": "lmstudio",
        "custom_provider_4_id": "nuevo",
        "custom_provider_12_base_url": "http://x",
        "otro_campo": "x",
    }
    assert _custom_provider_form_indexes(form) == [1, 4, 12]
    assert _custom_provider_form_indexes({}) == []


def test_parse_custom_providers_preserves_models_when_form_omits_them() -> None:
    """El formulario de config ya no lista los modelos: guardar la conexión de
    un proveedor no debe borrar los modelos que mantiene el analizador."""
    config = _config_with_custom()
    form = {
        "custom_provider_1_enabled": "on",
        "custom_provider_1_id": "lmstudio",
        "custom_provider_1_base_url": "http://127.0.0.1:1234/v1",
        "custom_provider_1_deployment": "local",
    }
    providers = _parse_custom_providers(config, form)
    assert len(providers) == 1
    assert [model["name"] for model in providers[0]["models"]] == ["previo"]
    assert providers[0]["models"][0]["context_window"] == 4096


def test_parse_custom_providers_accepts_index_beyond_three() -> None:
    config = BrokerConfig()
    form = {
        "custom_provider_4_enabled": "on",
        "custom_provider_4_id": "openrouter",
        "custom_provider_4_base_url": "https://openrouter.ai/api/v1",
        "custom_provider_4_sync_models": "on",
    }
    providers = _parse_custom_providers(config, form)
    assert [item["id"] for item in providers] == ["openrouter"]


def test_parse_custom_providers_drops_provider_marked_for_deletion() -> None:
    config = _config_with_custom()
    form = {
        "custom_provider_1_delete": "on",
        "custom_provider_1_enabled": "on",
        "custom_provider_1_id": "lmstudio",
        "custom_provider_1_base_url": "http://127.0.0.1:1234/v1",
        "custom_provider_2_enabled": "on",
        "custom_provider_2_id": "openrouter",
        "custom_provider_2_base_url": "https://openrouter.ai/api/v1",
        "custom_provider_2_sync_models": "on",
    }
    providers = _parse_custom_providers(config, form)
    assert [item["id"] for item in providers] == ["openrouter"]


def test_parse_custom_providers_still_parses_models_textarea_when_present() -> None:
    config = _config_with_custom()
    form = {
        "custom_provider_1_enabled": "on",
        "custom_provider_1_id": "lmstudio",
        "custom_provider_1_base_url": "http://127.0.0.1:1234/v1",
        "custom_provider_1_models": "previo|8192\nextra|4096",
    }
    providers = _parse_custom_providers(config, form)
    assert [model["name"] for model in providers[0]["models"]] == ["previo", "extra"]
    assert providers[0]["models"][0]["context_window"] == 8192


def test_apply_probe_results_merges_catalog_and_probe_outcomes() -> None:
    config = _config_with_custom()
    catalog = [
        {"name": "previo", "capabilities": ["completion"]},
        {"name": "descubierto", "context_window": 16384, "capabilities": ["embedding"]},
        {"id": ""},
    ]
    results = [
        {
            "name": "previo",
            "compatibility": "compatible",
            "compatibility_checked_at": "2026-07-12T00:00:00+00:00",
            "compatibility_error": None,
        },
    ]
    _apply_probe_results(config, "lmstudio", results, catalog)
    by_name = {item.name: item for item in config.providers.custom[0].models}
    # El modelo previo conserva su contexto y gana la compatibilidad sondeada.
    assert by_name["previo"].context_window == 4096
    assert by_name["previo"].compatibility == "compatible"
    # El descubierto en catálogo entra con sus capacidades y contexto.
    assert by_name["descubierto"].context_window == 16384
    assert by_name["descubierto"].capabilities == ["embedding"]

    with pytest.raises(PromptTesterError, match="no encontrado"):
        _apply_probe_results(config, "desconocido", results, catalog)


def test_validation_messages_flatten_pydantic_errors() -> None:
    from pydantic import ValidationError

    try:
        OpenAICompatibleProviderConfig(id="x y z", base_url="http://ok")
    except ValidationError as error:
        messages = _validation_messages(error)
    assert messages
    assert all(":" in message for message in messages)


def test_model_features_text_and_effective_caps_merge_catalog_claims() -> None:
    from app.dashboard_filters import model_effective_caps, model_features_text

    verified_and_catalog = {
        "features": {"vision": True, "json_mode": False},
        "catalog": {"vision": False, "json_mode": True, "tools": True},
    }
    # json_mode verificado en falso: el claim del catálogo no lo resucita.
    assert model_features_text(verified_and_catalog) == "visión · catálogo: tools"
    assert model_effective_caps(verified_and_catalog) == ["vision", "tools"]

    catalog_only = {"catalog": {"vision": True, "json_mode": False, "tools": False}}
    assert model_features_text(catalog_only) == "catálogo: visión"
    assert model_effective_caps(catalog_only) == ["vision"]

    assert model_features_text({}) == "sin sondear"
    assert model_effective_caps({}) == []
