"""Cobertura de la presentación del catálogo: disponibilidad y matriz de features."""
from app.model_catalog import model_availability_item, model_feature_profile


def _entry(**overrides):
    base = {
        "name": "modelo-x",
        "provider": "ollama",
        "deployment": "local",
        "status": "available",
        "capabilities": ["completion"],
        "compatibility": "compatible",
        "context_window": 8192,
        "compatibility_error": None,
    }
    base.update(overrides)
    return base


def test_availability_offline_when_provider_unavailable() -> None:
    item = model_availability_item(_entry(), {"ollama": {"status": "unavailable"}})
    assert item.availability == "offline"
    assert item.dispatchable is False


def test_availability_incompatible_uses_compatibility_error() -> None:
    item = model_availability_item(
        _entry(compatibility="incompatible", compatibility_error="404 en /chat/completions"),
        {"ollama": {"status": "healthy"}},
    )
    assert item.availability == "incompatible"
    assert item.reason == "404 en /chat/completions"


def test_availability_online_but_not_dispatchable_without_runnable_capability() -> None:
    item = model_availability_item(
        _entry(capabilities=["rerank"]),
        {"ollama": {"status": "healthy"}},
    )
    assert item.availability == "online"
    assert item.dispatchable is False


def test_availability_unknown_when_compatibility_unchecked() -> None:
    unchecked = model_availability_item(
        _entry(compatibility="unknown"),
        {"ollama": {"status": "healthy"}},
    )
    assert unchecked.availability == "unknown"
    assert "compatibilidad del modelo no comprobada" in unchecked.reason

    no_info = model_availability_item(
        _entry(compatibility="unknown", status="unknown"),
        {},
    )
    assert no_info.availability == "unknown"
    assert no_info.provider_status == "unknown"


def test_availability_tolerates_malformed_context_window() -> None:
    item = model_availability_item(
        _entry(context_window="no-numerico"),
        {"ollama": {"status": "healthy"}},
    )
    assert item.context_window is None


def test_feature_profile_infers_capabilities_from_model_name() -> None:
    profile = model_feature_profile(
        _entry(name="qwen2.5-vl-embedding-whisper-video-r1-coder-math-aya-guard-reranker")
    )
    features = profile["features"]
    assert features["modalities"]["image_input"] == "supported"
    assert features["modalities"]["audio_input"] == "supported"
    assert features["modalities"]["video_input"] == "supported"
    assert features["modalities"]["embedding_output"] == "supported"
    assert features["reasoning"]["reasoning_optimized"] == "supported"
    assert features["understanding"]["coding"] == "supported"
    assert features["understanding"]["math"] == "supported"
    assert features["understanding"]["multilingual"] == "supported"
    assert features["safety"]["safety_tuned"] == "supported"
    assert features["generation"]["reranking"] == "supported"
    assert any("inferido por el nombre" in note for note in profile["feature_notes"])


def test_feature_profile_reflects_deployment_and_compatibility() -> None:
    local = model_feature_profile(_entry())
    assert local["features"]["deployment"]["local_execution"] == "supported"
    assert local["features"]["deployment"]["cloud_execution"] == "unsupported"
    assert local["features"]["broker_support"]["mixture_proposer"] == "supported"

    remote = model_feature_profile(_entry(deployment="api", compatibility="incompatible"))
    assert remote["features"]["deployment"]["cloud_execution"] == "supported"
    assert remote["features"]["broker_support"]["mixture_proposer"] == "unsupported"
    assert remote["features"]["generation"]["chat_completions"] == "unsupported"
