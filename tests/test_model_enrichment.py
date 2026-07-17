"""Enriquecimiento del catálogo con models.dev: casado de nombres, jerarquía
de evidencia (el catálogo nunca pisa datos verificados) y caché sin red."""
import asyncio
import json

import httpx

from app.config import ModelEnrichmentConfig
from app.model_enrichment import ModelEnrichment, normalize_model_name


def test_normalize_model_name_converges_local_and_canonical_names() -> None:
    assert normalize_model_name("qwen/qwen3-30b-a3b") == "qwen3-30b-a3b"
    assert normalize_model_name("qwen3-30b-a3b:latest") == "qwen3-30b-a3b"
    assert normalize_model_name("Qwen3-30B-A3B-Q4_K_M.gguf") == "qwen3-30b-a3b"
    assert normalize_model_name("gpt-oss:20b") == "gpt-oss-20b"
    assert normalize_model_name("meta/llama-3.1-70b-instruct") == "llama-3-1-70b-instruct"
    assert normalize_model_name("") == ""
    assert normalize_model_name(None) == ""


_FAKE_CATALOG = {
    "lmstudio": {
        "models": {
            "qwen/qwen3-30b-a3b": {
                "tool_call": True,
                "structured_output": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 262144, "output": 32768},
                "cost": {"input": 0, "output": 0},
                "knowledge": "2025-04",
                "release_date": "2025-07-01",
            },
        },
    },
    "deepseek": {
        "models": {
            "deepseek-chat": {
                "tool_call": True,
                "structured_output": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 128000},
                "cost": {"input": 0.27, "output": 1.1},
            },
        },
    },
    "openrouter": {
        "models": {
            "mistralai/mistral-small-3.2": {
                "tool_call": False,
                "structured_output": True,
                "modalities": {"input": ["text"]},
                "limit": {"context": 32768},
                "cost": {"input": 0.05, "output": 0.1},
            },
        },
    },
}


def _enrichment(tmp_path, *, payload=_FAKE_CATALOG, fail_network=False, enabled=True) -> ModelEnrichment:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail_network:
            raise httpx.ConnectError("sin red")
        return httpx.Response(200, json=payload)

    return ModelEnrichment(
        ModelEnrichmentConfig(enabled=enabled),
        cache_path=tmp_path / "models_dev_catalog.json",
        transport=httpx.MockTransport(handler),
    )


def test_lookup_prefers_mapped_provider_and_flags_cost_comparability(tmp_path) -> None:
    enrichment = _enrichment(tmp_path)
    asyncio.run(enrichment.ensure_loaded())

    # Casado por proveedor mapeado: el precio es comparable.
    info, cost_ok = enrichment.lookup("lmstudio", "qwen/qwen3-30b-a3b")
    assert info is not None and cost_ok is True
    assert info["vision"] is True and info["tools"] is True

    # Casado global (otro proveedor del catálogo): capacidades sí, precio no.
    info, cost_ok = enrichment.lookup("ollama", "mistral-small-3.2:latest")
    assert info is not None and cost_ok is False
    assert info["json_mode"] is True and info["tools"] is False

    assert enrichment.lookup("lmstudio", "modelo-casero-inexistente") == (None, False)


def test_enrich_entry_fills_gaps_without_overriding_verified_data(tmp_path) -> None:
    enrichment = _enrichment(tmp_path)
    asyncio.run(enrichment.ensure_loaded())

    # Contexto default sin verificar: el catálogo lo sustituye y se anota la fuente.
    default_ctx = enrichment.enrich_entry({
        "name": "qwen/qwen3-30b-a3b", "provider": "lmstudio",
        "context_window": 128000, "context_window_source": "default",
    })
    assert default_ctx["context_window"] == 262144
    assert default_ctx["context_window_source"] == "catalog"
    assert default_ctx["catalog"]["json_mode"] is True
    assert default_ctx["catalog"]["cost_input_per_million"] == 0

    # Contexto configurado por el operador: intocable.
    configured = enrichment.enrich_entry({
        "name": "qwen/qwen3-30b-a3b", "provider": "lmstudio",
        "context_window": 8192, "context_window_source": "configured",
    })
    assert configured["context_window"] == 8192
    assert configured["context_window_source"] == "configured"

    # Casado global: sin claves de coste en el resultado.
    global_match = enrichment.enrich_entry({"name": "deepseek-chat", "provider": "nvidia"})
    assert "cost_input_per_million" not in global_match["catalog"]

    # Sin casado: la entrada vuelve intacta, sin clave catalog.
    missed = enrichment.enrich_entry({"name": "finetune-propio", "provider": "lmstudio"})
    assert "catalog" not in missed


def test_disk_cache_serves_catalog_when_network_is_down(tmp_path) -> None:
    online = _enrichment(tmp_path)
    asyncio.run(online.ensure_loaded())
    assert (tmp_path / "models_dev_catalog.json").exists()

    offline = _enrichment(tmp_path, fail_network=True)
    asyncio.run(offline.ensure_loaded())
    info, _ = offline.lookup("deepseek", "deepseek-chat")
    assert info is not None
    assert info["cost_input_per_million"] == 0.27

    # Sin red y sin caché: no hay enriquecimiento, pero tampoco excepción.
    empty = ModelEnrichment(
        ModelEnrichmentConfig(enabled=True),
        cache_path=tmp_path / "vacia" / "cache.json",
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("sin red"))),
    )
    asyncio.run(empty.ensure_loaded())
    assert empty.enrich_entry({"name": "deepseek-chat", "provider": "deepseek"}) == {
        "name": "deepseek-chat", "provider": "deepseek",
    }


def test_disabled_enrichment_never_touches_network_or_entries(tmp_path) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=_FAKE_CATALOG)

    enrichment = ModelEnrichment(
        ModelEnrichmentConfig(enabled=False),
        cache_path=tmp_path / "cache.json",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(enrichment.ensure_loaded())
    entry = {"name": "deepseek-chat", "provider": "deepseek"}
    assert enrichment.enrich_entry(entry) is entry
    assert calls["count"] == 0


def test_corrupt_disk_cache_is_ignored(tmp_path) -> None:
    cache = tmp_path / "models_dev_catalog.json"
    cache.write_text("{json roto", encoding="utf-8")
    enrichment = _enrichment(tmp_path, fail_network=True)
    asyncio.run(enrichment.ensure_loaded())
    assert enrichment.lookup("deepseek", "deepseek-chat") == (None, False)
    assert json.loads('"sanity"') == "sanity"
