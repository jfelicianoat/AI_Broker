"""`arbiter_policy` dejó de ser un campo decorativo.

Estaba declarado en el esquema desde el principio, con valor "strongest_available"
en las 4.307 peticiones recibidas, y no lo leía nadie: el árbitro salía del
mismo ranking por tiempo esperado que los proponentes. En task_14341781 eso
puso a juzgar al modelo que antes contestaba, que se quedó con la negativa de
dos proponentes y tiró el único resumen correcto del documento.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import BrokerConfig, ResourceConfig
from app.providers import OllamaProvider, RoutedModelProvider
from app.providers.base import declared_parameter_count
from app.schemas import TaskCreateRequest

# Los modelos locales reales de la máquina, con el tamaño que declara Ollama.
CATALOGO = [
    ("nemotron-3-nano:latest", "31.6B", 24_271_934_866),
    ("muse-glimmer:30b", "27.9B", 18_157_010_252),
    ("nemotron:latest", "70.6B", 42_500_000_000),
    ("gemma4:12b", "11.9B", 7_600_000_000),
    # No cabe en el presupuesto: 96 GB de pesos contra 62 GB de techo.
    ("laguna-s-2.1:q4_K_M", "117.6B", 96_000_000_000),
]


def _request(**overrides) -> TaskCreateRequest:
    payload = {
        "idempotency_key": "arbiter:policy",
        "content": {"prompt": "Compara estas propuestas y quédate con la cierta"},
        "execution": {"strategy": "mixture_of_agents", "preset": "fast", "max_proposers": 3},
        **overrides,
    }
    return TaskCreateRequest.model_validate(payload)


def _provider() -> RoutedModelProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": name, "size": size, "capabilities": ["completion"],
                 "details": {"context_length": 131072, "parameter_size": params}}
                for name, params, size in CATALOGO
            ]})
        return httpx.Response(200, json={"models": []})

    config = BrokerConfig(resources=ResourceConfig(local_vram_budget_gb=64.0))
    return RoutedModelProvider(
        config, ollama=OllamaProvider(config, transport=httpx.MockTransport(handler))
    )


def _elegido(request: TaskCreateRequest, roles: list[str]) -> str:
    provider = _provider()
    chosen = asyncio.run(provider.select(request, len(roles), roles))
    asyncio.run(provider.close())
    return chosen[0].model


class TestParameterCount:
    @pytest.mark.parametrize("declarado,esperado", [
        ("31.6B", 31.6), ("70.6B", 70.6), ("1.1B", 1.1), ("800M", 0.8), (" 27,9 b ", 27.9),
    ])
    def test_the_runtime_declaration_wins(self, declarado: str, esperado: float) -> None:
        assert declared_parameter_count({"parameter_size": declarado}) == esperado

    @pytest.mark.parametrize("nombre,esperado", [
        ("gemma4:31b-cloud", 31.0),
        ("deepseek-coder-33b-base", 33.0),
        ("allenai/olmo-3-32b-think", 32.0),
        # La versión no es un tamaño: 3.1 no puede ganarle a 8b.
        ("llama-3.1-8b-instruct", 8.0),
    ])
    def test_the_name_is_the_last_resort(self, nombre: str, esperado: float) -> None:
        assert declared_parameter_count({"name": nombre}) == esperado

    @pytest.mark.parametrize("entrada", [
        {"name": "laguna-xs-2.1"}, {"name": "north-mini-code-1.0:q8_0"},
        {"name": "text-embedding-nomic-embed-text-v1.5"}, {},
    ])
    def test_an_unknown_size_is_not_invented(self, entrada: dict) -> None:
        assert declared_parameter_count(entrada) is None

    def test_disk_size_is_not_used_as_a_proxy(self) -> None:
        """Un 30B en Q8 pesa más que un 70B en Q4 y no es más capaz: el peso
        mide la cuantización tanto como el modelo."""
        assert declared_parameter_count({"size_bytes": 96_000_000_000}) is None


class TestTheArbiterIsPickedForCapacity:
    def test_the_strongest_model_arbitrates(self) -> None:
        assert _elegido(_request(), ["arbiter"]) == "nemotron:latest"

    def test_a_model_that_does_not_fit_is_not_the_strongest_available(self) -> None:
        """laguna es el mayor del catálogo y no cabe en la máquina: elegirlo
        sería elegir un árbitro que falla seguro con VRAM_MODEL_TOO_LARGE."""
        provider = _provider()
        orden = asyncio.run(provider.select(_request(), 5, ["arbiter"] * 5))
        asyncio.run(provider.close())
        assert orden[0].model == "nemotron:latest"
        assert orden[-1].model == "laguna-s-2.1:q4_K_M"

    def test_the_proposers_are_still_picked_by_time(self) -> None:
        """La política es del árbitro. Quien responde se sigue eligiendo por lo
        que tarda, que es lo que nota el usuario."""
        elegido = _elegido(_request(), ["generalist", "specialist", "skeptic"])
        assert elegido != "nemotron:latest"

    def test_fastest_available_keeps_the_old_behaviour(self) -> None:
        request = _request(execution={
            "strategy": "mixture_of_agents", "preset": "fast", "max_proposers": 3,
            "selection": {"mode": "auto", "arbiter_policy": "fastest_available"},
        })
        assert _elegido(request, ["arbiter"]) != "nemotron:latest"

    def test_an_explicit_model_still_wins_over_the_policy(self) -> None:
        """`preferred_model` es una decisión del cliente; la política es una
        preferencia del broker y no la pisa."""
        request = _request(model_requirements={"preferred_model": "gemma4:12b"})
        assert _elegido(request, ["arbiter"]) == "gemma4:12b"

    def test_an_unknown_policy_is_rejected_at_the_contract(self) -> None:
        """El campo era un str libre y por eso pudo quedarse sin implementar sin
        que nadie lo notara. Ahora un valor que el broker no aplica se rechaza
        en la validación, no se ignora en silencio."""
        with pytest.raises(ValueError):
            _request(execution={
                "strategy": "mixture_of_agents",
                "selection": {"mode": "auto", "arbiter_policy": "el_mas_barato"},
            })
