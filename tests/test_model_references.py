"""Qué se avisa antes de borrar un modelo del disco.

El borrado es irreversible, así que la confirmación tiene que poder decir qué
deja de funcionar: una configuración que nombra el modelo a mano queda
apuntando a algo que ya no existe, y borrar el último modelo con cierta
capacidad deja al broker sin poder atender esa clase de peticiones.
"""
from app.config import (
    BrokerConfig,
    DeepSeekConfig,
    IngestionConfig,
    IngestionImagesConfig,
    OpenAICompatibleModelConfig,
    OpenAICompatibleProviderConfig,
    ProvidersConfig,
)
from app.model_references import deletion_warnings

_LOCAL_CHAT = {
    "name": "chat:8b", "provider": "ollama", "deployment": "local",
    "capabilities": ["completion"],
}
_LOCAL_EMBED = {
    "name": "embed:0.5b", "provider": "ollama", "deployment": "local",
    "capabilities": ["embedding"],
}
_CLOUD_EMBED = {
    "name": "embed-cloud", "provider": "deepseek", "deployment": "cloud",
    "capabilities": ["embedding"],
}


def test_nothing_to_warn_about_an_ordinary_model() -> None:
    catalog = [_LOCAL_CHAT, dict(_LOCAL_CHAT, name="otro:8b")]
    assert deletion_warnings(BrokerConfig(), catalog, "ollama", "chat:8b") == []


def test_warns_when_the_vision_model_of_the_ingestion_is_deleted() -> None:
    config = BrokerConfig(
        ingestion=IngestionConfig(images=IngestionImagesConfig(enabled=True, model="visor:7b")),
    )
    catalog = [dict(_LOCAL_CHAT, name="visor:7b", capabilities=["completion", "vision"])]
    warnings = deletion_warnings(config, catalog, "ollama", "visor:7b")
    assert any("ingestion.images.model" in warning for warning in warnings)
    assert any("figuras" in warning for warning in warnings)


def test_warns_when_a_custom_provider_declares_the_model() -> None:
    config = BrokerConfig(providers=ProvidersConfig(custom=[
        OpenAICompatibleProviderConfig(
            id="lmstudio", enabled=True, display_name="LM Studio",
            base_url="http://127.0.0.1:1234/v1", api_key_env=None, deployment="local",
            models=[OpenAICompatibleModelConfig(name="chat:8b", context_window=8192)],
        ),
    ]))
    warnings = deletion_warnings(config, [_LOCAL_CHAT], "ollama", "chat:8b")
    assert any("providers.custom[lmstudio]" in warning for warning in warnings)


def test_warns_when_it_is_the_default_deepseek_model() -> None:
    config = BrokerConfig(providers=ProvidersConfig(
        deepseek=DeepSeekConfig(default_model="chat:8b"),
    ))
    warnings = deletion_warnings(config, [_LOCAL_CHAT], "ollama", "chat:8b")
    assert any("deepseek.default_model" in warning for warning in warnings)


def test_warns_when_it_is_the_last_local_model_of_a_capability() -> None:
    """Borrar el único embedding local deja esas tareas sin dónde ejecutarse."""
    catalog = [_LOCAL_CHAT, _LOCAL_EMBED]
    warnings = deletion_warnings(BrokerConfig(), catalog, "ollama", "embed:0.5b")
    assert any("ÚNICO modelo local" in warning and "embedding" in warning for warning in warnings)


def test_a_cloud_survivor_does_not_count_as_a_replacement() -> None:
    """Decir «queda uno en la nube» sería contestar a otra pregunta, y una que
    la clasificación de datos de sus tareas puede tener prohibida."""
    catalog = [_LOCAL_CHAT, _LOCAL_EMBED, _CLOUD_EMBED]
    warnings = deletion_warnings(BrokerConfig(), catalog, "ollama", "embed:0.5b")
    assert any("ÚNICO modelo local" in warning for warning in warnings)


def test_no_capability_warning_when_another_local_model_survives() -> None:
    catalog = [_LOCAL_EMBED, dict(_LOCAL_EMBED, name="embed2:0.5b")]
    assert deletion_warnings(BrokerConfig(), catalog, "ollama", "embed:0.5b") == []


def test_losing_the_last_chat_model_is_not_reported() -> None:
    """Quedarse con un solo modelo de chat es normal; avisar de ello en cada
    borrado sería ruido que enseña a ignorar los avisos."""
    assert deletion_warnings(BrokerConfig(), [_LOCAL_CHAT], "ollama", "chat:8b") == []
