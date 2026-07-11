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
    _ensure_cloud_allowed,
    _find_custom_provider,
    _float_field,
    _float_range_field,
    _int_field,
    _int_range_field,
    _optional_float,
    _parse_custom_provider_models,
    _parse_huggingface_local_models,
    _parse_model_reference,
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


def test_parse_huggingface_models_lines_and_errors() -> None:
    models = _parse_huggingface_local_models(
        "# comentario\n"
        "qwen|/modelos/qwen|4096|cpu|fp16\n"
        "\n"
        "phi|phi-3-mini\n"
    )
    assert [(m.name, m.context_window, m.device, m.dtype) for m in models] == [
        ("qwen", 4096, "cpu", "fp16"),
        ("phi", 32768, None, None),
    ]
    assert _parse_huggingface_local_models("") == []
    with pytest.raises(PromptTesterError, match="linea 1"):
        _parse_huggingface_local_models("solo-nombre-sin-ruta")
    with pytest.raises(PromptTesterError, match="linea 1"):
        _parse_huggingface_local_models("qwen|/ruta|contexto-no-numerico")


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
