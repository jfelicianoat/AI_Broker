"""Regresiones de la tarea task_fffcffb6…, donde la respuesta que llegó al
usuario era el prompt del árbitro reescrito en portuñol.

Tres eslabones fallaron a la vez: el clasificador mandó una explicación en
español a un modelo de código, ese modelo devolvió su entrada en vez de
sintetizar, y nadie lo comprobó.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import BrokerConfig, PromptCompressionConfig
from app.providers import OllamaProvider, ProviderError, RoutedModelProvider
from app.providers.base import looks_like_prompt_echo, prompt_echo_ratio
from app.schemas import ModelReference, TaskCreateRequest
from app.task_classifier import classify_task_type

# La petición real, ya comprimida, que ChatyGPT envió aquel día. El término
# "implementa" aparece dentro de "implementaciones" y dentro del HISTORIAL,
# no de la pregunta: las dos razones por las que se clasificó como código.
REAL_PROMPT = (
    "following JSON list is complete set of files active for current request.\n"
    "<active_attachment_scope_json>[]</active_attachment_scope_json>\n\n"
    "Continue conversation. Treat JSON history as previous dialogue data.\n"
    '<conversation_history_json>[{"role":"assistant","text":"Tokenización y API: '
    'muchas implementaciones rechazan prompts vacíos o el bucle de generación falla."}]'
    "</conversation_history_json>\n\n"
    "Current user request:\n"
    "Explícame porque llm thinking si se le envía petición con longitud 0, "
    "falla en su respuesta"
)


def _request(prompt: str, **overrides) -> TaskCreateRequest:
    payload = {
        "idempotency_key": "echo:test",
        "content": {"prompt": prompt},
        **overrides,
    }
    return TaskCreateRequest.model_validate(payload)


class TestTaskClassification:
    def test_the_real_petition_is_no_longer_routed_to_a_code_model(self) -> None:
        assert classify_task_type(_request(REAL_PROMPT)) != "code"

    @pytest.mark.parametrize("prompt", [
        "Escribe una función en Python que ordene una lista",
        "Implementa un endpoint REST para crear usuarios",
        "Corrige este bug en el script de despliegue",
        "Optimiza esta función recursiva",
        "Programa una función que calcule la media",
        "Revisa este traceback y dime qué falla",
        "Analiza el fichero config.yaml",
        "```python\nprint(1)\n```\n¿Qué hace?",
    ])
    def test_real_code_work_is_still_code(self, prompt: str) -> None:
        assert classify_task_type(_request(prompt)) == "code"

    @pytest.mark.parametrize("prompt", [
        # Vocabulario técnico sin encargo: hablar de programación no es programar.
        "Muchas implementaciones rechazan prompts vacíos, explícame por qué",
        "Explica qué es una variable aleatoria en estadística",
        "Habla sobre algoritmos de recomendación y su impacto social",
        # Palabras que solo son técnicas por accidente.
        "¿Cuál es el código postal de Valencia?",
        "¿Qué código civil se aplica a este contrato?",
        "Resume el programa de televisión de anoche",
        # Verbo de encargo sin nada de código.
        "Escribe un poema sobre el mar",
        "Revisa la ortografía de este texto",
    ])
    def test_talking_about_technology_is_prose(self, prompt: str) -> None:
        assert classify_task_type(_request(prompt)) == "prose"

    def test_context_blocks_do_not_decide_the_routing(self) -> None:
        """Una palabra en el historial no puede clasificar la petición de hoy."""
        con_historial = _request(
            "<conversation_history_json>[{\"text\":\"escribe una función en python\"}]"
            "</conversation_history_json>\n\nCurrent user request:\n¿Quién pintó Las Meninas?"
        )
        assert classify_task_type(con_historial) == "prose"


class TestCodeModelsStayOnCode:
    """El objetivo final: que un modelo de código no acabe redactando prosa.

    La política ya existía en task_affinity.exclude_by_task_type; lo que
    fallaba era la clasificación, así que nunca llegaba a aplicarse.
    """

    CATALOG = [
        {"name": "qwen3-coder:30b", "provider": "ollama", "deployment": "local"},
        {"name": "qwen3-coder-next:latest", "provider": "ollama", "deployment": "local"},
        {"name": "north-mini-code-1.0:q8_0", "provider": "ollama", "deployment": "local"},
        {"name": "gemma4:12b", "provider": "ollama", "deployment": "local"},
    ]

    def _excluded(self, prompt: str) -> set[str]:
        from app.providers.routing import task_affinity_excluded

        config = BrokerConfig()
        task_type = classify_task_type(_request(prompt))
        return {
            entry["name"]
            for entry in task_affinity_excluded(self.CATALOG, config.task_affinity, task_type)
        }

    def test_the_real_petition_now_keeps_coders_out(self) -> None:
        apartados = self._excluded(REAL_PROMPT)
        assert "qwen3-coder:30b" in apartados
        assert "qwen3-coder-next:latest" in apartados
        assert "north-mini-code-1.0:q8_0" in apartados
        assert "gemma4:12b" not in apartados

    def test_real_code_work_still_gets_code_models(self) -> None:
        assert self._excluded("Escribe una función en Python que ordene una lista") == set()


class TestPromptEchoDetection:
    def test_an_answer_that_is_the_prompt_is_caught(self) -> None:
        prompt = "<original_request>\n" + ("Explica por qué falla el modelo. " * 40) + "\n</original_request>"
        assert looks_like_prompt_echo(prompt, prompt.replace("<original_request>", "<forward>"))

    def test_a_real_answer_is_not_flagged(self) -> None:
        prompt = "<original_request>\n" + ("Explica por qué falla el modelo. " * 40) + "\n</original_request>"
        answer = (
            "Cuando se envía una petición de longitud cero, el modelo carece de contexto "
            "inicial y no puede activar su proceso de razonamiento interno. Los mecanismos "
            "de atención requieren al menos un token para producir representaciones útiles, "
            "de modo que la inferencia se detiene o es rechazada en la capa de validación."
        )
        assert not looks_like_prompt_echo(prompt, answer)
        assert prompt_echo_ratio(prompt, answer) < 0.1

    def test_a_short_answer_quoting_the_prompt_is_allowed(self) -> None:
        """Citar es legítimo; el eco que importa es reproducirlo entero."""
        prompt = "Resume en una frase: " + ("dato relevante " * 100)
        assert not looks_like_prompt_echo(prompt, "dato relevante " * 5)

    def test_the_echo_becomes_a_provider_error(self) -> None:
        prompt_largo = "Sintetiza estas propuestas sobre modelos de lenguaje. " * 30

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{
                    "name": "loro", "size": 1, "capabilities": ["completion"],
                    "details": {"context_length": 128000},
                }]})
            if request.url.path == "/api/chat":
                # El modelo devuelve su propio prompt, como qwen3-coder:30b.
                body = request.content.decode()
                import json as _json
                echoed = _json.loads(body)["messages"][-1]["content"]
                return httpx.Response(200, json={
                    "message": {"content": echoed}, "done_reason": "stop",
                    "prompt_eval_count": 10, "eval_count": 400,
                })
            return httpx.Response(200, json={"models": []})

        config = BrokerConfig()
        provider = RoutedModelProvider(
            config, ollama=OllamaProvider(config, transport=httpx.MockTransport(handler))
        )
        model = ModelReference(provider="ollama", deployment="local", model="loro")
        with pytest.raises(ProviderError) as raised:
            asyncio.run(provider.propose(_request(prompt_largo), model, 1))
        asyncio.run(provider.close())
        assert raised.value.code == "PROMPT_ECHOED"
        assert "copia literal" in str(raised.value)


class TestCompressionKeepsTheLanguageAnchor:
    @staticmethod
    def _provider() -> RoutedModelProvider:
        config = BrokerConfig(
            prompt_compression=PromptCompressionConfig(
                enabled=True, level="aggressive", min_chars=10
            )
        )
        return RoutedModelProvider(config)

    def test_articles_survive_when_a_language_is_required(self) -> None:
        provider = self._provider()
        texto = "Explica el motivo por el que la petición de longitud cero falla en un modelo"
        con_idioma = provider.user_prompt(
            _request(texto, output={"format": "markdown", "language": "es"})
        )
        asyncio.run(provider.close())
        # medium sigue quitando cortesías y muletillas, pero no determinantes:
        # un español sin artículos suelta el anclaje del idioma.
        assert " el " in con_idioma and " la " in con_idioma

    def test_a_task_can_still_ask_for_caveman_mode_explicitly(self) -> None:
        """`output.language` tiene default "es", así que la política global ya
        nunca borra artículos. Pedirlo desde la petición sigue funcionando: es
        la vía explícita, y la petición manda sobre la política del broker."""
        provider = self._provider()
        texto = "Explica el motivo por el que la petición de longitud cero falla en un modelo"
        explicito = provider.user_prompt(
            _request(texto, prompt_compression="aggressive")
        )
        asyncio.run(provider.close())
        assert " el " not in explicito
