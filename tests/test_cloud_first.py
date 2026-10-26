"""Cloud primero cuando la petición lo permite (decisión del usuario, 2026-10-25).

Antes ninguna tarea iba al cloud: sin medidas recientes, los remotos quedaban
detrás de todos los locales medidos y la exploración remataba antes a los
locales «a medias». Con la BD real, 2.000 de 2.000 elecciones eran locales.
"""
from __future__ import annotations

from uuid import uuid4

from app.config import BrokerConfig, OpenAICompatibleProviderConfig, RoutingConfig
from app.model_stats import ModelStats
from app.providers import RoutedModelProvider
from app.schemas import TaskCreateRequest


def _entry(name: str, provider: str, deployment: str) -> dict:
    return {"name": name, "provider": provider, "deployment": deployment,
            "context_window": 100000, "capabilities": ["completion"], "compatibility": "compatible"}


CATALOG = [
    _entry("local-rapido", "ollama", "local"),
    _entry("local-lento", "ollama", "local"),
    _entry("nim-a", "nvidia", "api"),
    _entry("nim-b", "nvidia", "api"),
    # Declarado cloud pero corre en este PC: no cuenta como cloud de verdad.
    _entry("studio", "unsloth", "cloud"),
    _entry("gemma-cloud", "ollama", "cloud"),
]
STATS = {
    ("ollama", "local", "local-rapido", "prose"): ModelStats(10, 10, 500.0, 0.0),
    ("ollama", "local", "local-lento", "prose"): ModelStats(10, 10, 9000.0, 0.0),
}


def _router(**routing) -> RoutedModelProvider:
    config = BrokerConfig()
    config.routing = RoutingConfig(**{"exploration_rate": 0.0, "cloud_exploration_rate": 0.0, **routing})
    config.providers.custom = [
        OpenAICompatibleProviderConfig(id="nvidia", base_url="https://integrate.api.nvidia.com/v1",
                                       deployment="api"),
        OpenAICompatibleProviderConfig(id="unsloth", base_url="http://127.0.0.1:8888/v1",
                                       deployment="cloud"),
    ]
    return RoutedModelProvider(config, custom={}, stats_loader=lambda: STATS)


def _request(classification: str) -> TaskCreateRequest:
    return TaskCreateRequest.model_validate({
        "idempotency_key": f"cloud:{uuid4().hex}",
        "content": {"prompt": "Explica la fotosíntesis."},
        "risk": {"data_classification": classification},
    })


def _names(ranked: list[dict]) -> list[str]:
    return [item["name"] for item in ranked]


def test_cloud_goes_first_when_the_request_allows_it() -> None:
    request = _request("internal")
    assert request.model_requirements.cloud_allowed is True
    ranked = _names(_router()._rank_candidates(CATALOG, request))
    assert set(ranked[:3]) == {"nim-a", "nim-b", "gemma-cloud"}
    # Lo local queda de respaldo, en su orden de tiempo esperado.
    assert ranked.index("local-rapido") < ranked.index("local-lento")


def test_a_loopback_provider_labelled_cloud_is_not_cloud() -> None:
    ranked = _names(_router()._rank_candidates(CATALOG, _request("internal")))
    assert ranked.index("studio") > ranked.index("gemma-cloud")
    assert ranked.index("studio") > 2


def test_switch_off_restores_the_time_ranking() -> None:
    ranked = _names(_router(prefer_cloud_when_allowed=False)._rank_candidates(CATALOG, _request("internal")))
    assert ranked[0] == "local-rapido"


def test_cloud_exploration_spreads_the_work() -> None:
    router = _router(cloud_exploration_rate=1.0)
    firsts = {router._rank_candidates(CATALOG, _request("internal"))[0]["name"] for _ in range(200)}
    assert firsts <= {"nim-a", "nim-b", "gemma-cloud"}
    assert len(firsts) >= 2


def test_a_request_that_forbids_cloud_keeps_the_local_ranking() -> None:
    """Sin cloud_allowed los remotos ni llegan aquí (frontera de datos); y si
    llegaran, el bloque cloud no se activa."""
    request = _request("internal").model_copy(deep=True)
    request.model_requirements.cloud_allowed = False
    ranked = _names(_router()._rank_candidates(CATALOG, request))
    assert ranked[0] == "local-rapido"
