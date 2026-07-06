import pytest

from app.config import BrokerConfig, PromptCompressionConfig
from app.prompt_compressor import PromptCompressor


def test_disabled_returns_original():
    compressor = PromptCompressor(enabled=False)
    prompt = "Por favor, ¿podrías resumir este documento? Muchas gracias."
    result = compressor.compress(prompt)
    assert result.text == prompt
    assert not result.applied


def test_short_prompt_below_min_chars_untouched():
    compressor = PromptCompressor(enabled=True, min_chars=200)
    prompt = "Por favor resume esto."
    assert compressor.compress(prompt).text == prompt


def test_removes_spanish_courtesy_and_fillers():
    compressor = PromptCompressor(enabled=True, level="medium", min_chars=0)
    prompt = (
        "Hola, por favor, básicamente necesito que resumas el informe de ventas "
        "del último trimestre. Gracias de antemano."
    )
    result = compressor.compress(prompt)
    assert result.applied
    lowered = result.text.lower()
    assert "por favor" not in lowered
    assert "hola" not in lowered
    assert "básicamente" not in lowered
    assert "necesito que" not in lowered
    assert "gracias" not in lowered
    assert "resumas el informe de ventas" in result.text
    assert result.compressed_chars < result.original_chars


def test_light_level_keeps_request_wrappers():
    compressor = PromptCompressor(enabled=True, level="light", min_chars=0)
    result = compressor.compress("Por favor, necesito que traduzcas este texto al inglés.")
    assert "necesito que" in result.text
    assert "Por favor" not in result.text


def test_aggressive_drops_articles():
    compressor = PromptCompressor(enabled=True, level="aggressive", min_chars=0)
    result = compressor.compress(
        "Analiza los resultados de la campaña y describe las conclusiones principales del estudio."
    )
    lowered = f" {result.text.lower()} "
    assert " los " not in lowered
    assert " la " not in lowered
    assert " las " not in lowered
    assert "analiza" in lowered
    assert "resultados" in lowered


def test_code_blocks_and_urls_preserved():
    compressor = PromptCompressor(enabled=True, level="aggressive", min_chars=0)
    prompt = (
        "Por favor revisa el código:\n"
        "```python\n"
        "los = [la for la in unas]\n"
        "```\n"
        "y también `el = la + los` según https://ejemplo.com/los/la-guia "
        "avisando a soporte@ejemplo.com"
    )
    result = compressor.compress(prompt)
    assert "los = [la for la in unas]" in result.text
    assert "`el = la + los`" in result.text
    assert "https://ejemplo.com/los/la-guia" in result.text
    assert "soporte@ejemplo.com" in result.text


def test_word_boundaries_do_not_break_words():
    compressor = PromptCompressor(enabled=True, level="aggressive", min_chars=0)
    # "el" dentro de "modelo"/"Manuela" no debe eliminarse.
    result = compressor.compress("Manuela entrena un modelo local con datos reales.")
    assert "Manuela" in result.text
    assert "modelo" in result.text


def test_overcompression_falls_back_to_original():
    compressor = PromptCompressor(enabled=True, level="aggressive", min_chars=0)
    prompt = "el la los las un una unos unas lo"
    assert compressor.compress(prompt).text == prompt


def test_invalid_level_rejected():
    with pytest.raises(ValueError):
        PromptCompressor(level="extreme")


def test_broker_config_defaults_and_toggle():
    config = BrokerConfig()
    assert config.prompt_compression.enabled is True
    assert config.prompt_compression.level == "medium"
    disabled = PromptCompressionConfig(enabled=False)
    assert disabled.enabled is False


def test_routing_provider_respects_reload(monkeypatch):
    from app.config import BrokerConfig
    from app.providers.routing import RoutedModelProvider

    config = BrokerConfig()
    config.prompt_compression.enabled = True
    config.prompt_compression.min_chars = 0
    provider = RoutedModelProvider(config)
    assert provider.prompt_compressor.enabled is True

    updated = config.model_copy(deep=True)
    updated.prompt_compression.enabled = False
    provider.reload_config(updated)
    assert provider.prompt_compressor.enabled is False
