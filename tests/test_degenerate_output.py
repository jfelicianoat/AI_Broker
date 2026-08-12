"""Regresiones del bucle degenerativo: task_456ea023, el fallo de origen.

El árbitro escribió dos frases correctas y siguió con "fallo en " 1.297 veces
hasta agotar `max_output_tokens`. Nada falló de forma visible —la respuesta
llegó entera y con `done_reason` normal—, así que se guardó como `completed`,
se entregó al usuario, y volvió al broker en el historial del turno siguiente,
donde sí reventó (`PROMPT_ECHOED`, task_c8da5dd6). El guardarraíl atrapaba el
reflejo, no el disparo: aquella repetición era nueva, no copia del prompt.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import UNUSABLE_OUTPUT_CODES, BrokerConfig, load_config
from app.providers import OllamaProvider, ProviderError, RoutedModelProvider
from app.providers.base import degenerate_loop_period, looks_like_degenerate_loop
from app.schemas import ModelReference, TaskCreateRequest

# La forma exacta de aquella salida: respuesta que empieza bien y se atasca.
SALIDA_REAL = (
    "### En una frase:\nEste texto explica por qué los modelos que \"piensan\" "
    "fallan cuando no reciben una consigna clara.\n\n¿Quieres que te ayude a "
    "formular una buena pregunta para evitar este fallo en el fallo de "
) + "fallo en " * 400


def _request(prompt: str = "Resume el documento") -> TaskCreateRequest:
    return TaskCreateRequest.model_validate({
        "idempotency_key": "degenerate:test", "content": {"prompt": prompt},
    })


class TestTheDetector:
    def test_the_real_output_is_caught(self) -> None:
        assert looks_like_degenerate_loop(SALIDA_REAL)
        assert degenerate_loop_period(SALIDA_REAL) == len("fallo en ")

    @pytest.mark.parametrize("texto", [
        # Prosa larga y normal: lo que responde un modelo sano.
        "El conjunto MNIST contiene 70.000 imágenes de dígitos manuscritos. "
        "Se usó durante años como referencia de clasificación, hasta que las "
        "redes convolucionales bajaron el error por debajo del 0,5%, momento "
        "en que dejó de discriminar entre arquitecturas. Hoy se considera "
        "demasiado fácil para la investigación, aunque sigue siendo útil como "
        "primera prueba de que una implementación funciona. Su sucesor "
        "habitual en los artículos es ImageNet, mucho más grande y difícil, "
        "cuyas etiquetas provienen de los synsets de WordNet." * 2,
        # Una tabla markdown: filas parecidas, ninguna repetida.
        "| Método | Error |\n" + "".join(
            f"| Modelo con {n} capas ocultas y regularización | {n / 100:.2f} |\n"
            for n in range(1, 40)
        ),
        # Una lista larga: mismo prefijo en cada punto, contenido distinto.
        "".join(f"- Punto número {n} de la enumeración solicitada\n" for n in range(1, 40)),
        # Código con estructura muy repetitiva.
        "".join(f"    self.campo_{n} = datos.get('campo_{n}', None)\n" for n in range(1, 30)),
    ])
    def test_repetitive_but_legitimate_text_is_not_flagged(self, texto: str) -> None:
        """El riesgo real no es no pillar el bucle: es rechazar trabajo bueno.
        Una tabla, una lista y un bloque de código son repetitivos a propósito."""
        assert len(texto) > 400, "el caso debe ser lo bastante largo para poder marcarse"
        assert not looks_like_degenerate_loop(texto)

    def test_a_short_repetition_is_not_enough(self) -> None:
        """Repetir una coletilla al cerrar no es haberse quedado atascado."""
        assert not looks_like_degenerate_loop("Sí, sí, sí, sí, sí, sí.")

    def test_a_loop_the_model_escapes_from_is_left_alone(self) -> None:
        """Solo se mira la cola: si el modelo salió del bucle y terminó de
        responder, la respuesta sirve y no se tira."""
        recuperada = SALIDA_REAL + (
            "\n\nDisculpa la repetición. En resumen: el documento sostiene que "
            "un modelo razonador necesita un objetivo explícito para activar su "
            "cadena de pensamiento, y que sin él la salida se degrada."
        )
        assert not looks_like_degenerate_loop(recuperada)


class TestTheLoopBecomesAFailure:
    @staticmethod
    def _provider(respuesta: str) -> RoutedModelProvider:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{
                    "name": "loro", "size": 1, "capabilities": ["completion"],
                    "details": {"context_length": 128000},
                }]})
            if request.url.path == "/api/chat":
                return httpx.Response(200, json={
                    "message": {"content": respuesta}, "done_reason": "length",
                    "prompt_eval_count": 6858, "eval_count": 4000,
                })
            return httpx.Response(200, json={"models": []})

        config = BrokerConfig()
        return RoutedModelProvider(
            config, ollama=OllamaProvider(config, transport=httpx.MockTransport(handler))
        )

    def test_the_stuck_answer_no_longer_passes_as_good(self) -> None:
        provider = self._provider(SALIDA_REAL)
        model = ModelReference(provider="ollama", deployment="local", model="loro")
        with pytest.raises(ProviderError) as raised:
            asyncio.run(provider.propose(_request(), model, 1))
        asyncio.run(provider.close())
        assert raised.value.code == "DEGENERATE_OUTPUT"
        assert "fallo en" in str(raised.value)
        # Se pagó: 4.000 tokens de salida y la carga del modelo. La evidencia
        # se guarda en vez de perderse (ver fail_invocation).
        assert raised.value.output is not None
        assert raised.value.output.tokens_output == 4000

    def test_a_good_answer_still_gets_through(self) -> None:
        provider = self._provider(
            "El documento sostiene que un modelo razonador necesita un objetivo "
            "explícito para activar su cadena de pensamiento. " * 6
        )
        model = ModelReference(provider="ollama", deployment="local", model="loro")
        output = asyncio.run(provider.propose(_request(), model, 1))
        asyncio.run(provider.close())
        assert output.content is not None and "objetivo explícito" in output.content


def test_the_shipped_config_counts_unusable_output() -> None:
    """La configuración viva pinaba `definitive_codes` sin `PROMPT_ECHOED`: el
    código lo tenía por defecto y el YAML lo pisaba entero, así que en la
    máquina real un eco no apartaba a nadie. Una lista que se queda atrás
    convierte un guardarraíl en un adorno, y en silencio."""
    codigos = load_config("broker_config.yaml").model_quarantine.definitive_codes
    assert [code for code in UNUSABLE_OUTPUT_CODES if code not in codigos] == []
