"""Diagnóstico de fallos de proveedor.

Todas las firmas de este fichero salen de errores que ocurrieron **de verdad**
en esta máquina y quedaron en el histórico. No hay ninguna inventada: una tabla
de diagnósticos escrita imaginando qué podría fallar acaba reconociendo lo que
nunca pasa y callando ante lo que pasa a diario.

Lo que se protege: que el diagnóstico ayude sin mentir. Una firma reconocida da
un código específico y una salida; una desconocida se deja como estaba, con el
texto del proveedor intacto.
"""
from __future__ import annotations

import httpx
import pytest

from app.providers.base import ProviderError, provider_error_from_http
from app.providers.diagnostics import diagnose, reasoning_only_error

# --- Mensajes literales del histórico (state/broker.db), abreviados. ---

OLLAMA_OOM = (
    'HTTP 500 en http://127.0.0.1:11434/api/chat: {"error":"llama-server process has '
    'terminated: exit status 1: cudaMalloc failed: out of memory\\nalloc_tensor_range: '
    'failed to allocate ROCm0 buffer of size 92469490688"}'
)
OLLAMA_OOM_STARTUP = (
    'HTTP 500: {"error":"llama-server reported out-of-memory during startup: '
    'cudaMalloc failed: out of memory"}'
)
LMSTUDIO_ENGINE = (
    'HTTP 400 en http://127.0.0.1:1234/v1/chat/completions: '
    '{"error":"Engine protocol predict request failed: fetch failed"}'
)
LMSTUDIO_UNLOADED = 'HTTP 400: {"error":"Model is unloaded."}'
LMSTUDIO_GUARDRAIL = (
    'HTTP 400: {"error":{"message":"Failed to load model \\"google/gemma-4-31b-qat\\". '
    "Error: Model loading was stopped due to insufficient system resources. Under the "
    'current settings, this model requires approximately 90.17 GB of memory"}}'
)
NVIDIA_DEGRADED = (
    'HTTP 400 en https://integrate.api.nvidia.com/v1/chat/completions: '
    '{"status":400,"title":"Bad Request","detail":"DEGRADED function"}'
)


class TestKnownSignatures:
    def test_the_most_frequent_failure_becomes_a_memory_diagnosis(self) -> None:
        """31 de los 40 MODEL_ERROR del histórico son este mismo texto: un
        volcado JSON con una IP, un código de salida y un número de bytes, con
        el diagnóstico enterrado dentro."""
        diagnosis = diagnose(OLLAMA_OOM)

        assert diagnosis is not None
        assert diagnosis.code == "MODEL_OUT_OF_MEMORY"
        # No se reintenta: un modelo que no cabe no cabe más por insistir.
        assert diagnosis.retryable is False
        assert "memoria" in diagnosis.summary

    def test_both_wordings_of_running_out_of_memory_are_the_same_problem(self) -> None:
        """El runtime lo dice distinto al cargar y al arrancar; para quien lo
        sufre es lo mismo."""
        assert diagnose(OLLAMA_OOM_STARTUP).code == "MODEL_OUT_OF_MEMORY"

    def test_a_providers_own_memory_guardrail_is_also_a_memory_problem(self) -> None:
        """LM Studio lo detecta antes de romper nada y dice cuánto necesitaría.
        Es el mismo diagnóstico por otra vía."""
        diagnosis = diagnose(LMSTUDIO_GUARDRAIL)

        assert diagnosis is not None
        assert diagnosis.code == "MODEL_OUT_OF_MEMORY"

    def test_an_engine_crash_is_not_blamed_on_the_prompt(self) -> None:
        diagnosis = diagnose(LMSTUDIO_ENGINE)

        assert diagnosis is not None
        assert diagnosis.code == "PROVIDER_ENGINE_CRASHED"
        # Sí se reintenta: el motor puede volver, a diferencia de la memoria.
        assert diagnosis.retryable is True

    def test_an_unloaded_model_says_so(self) -> None:
        diagnosis = diagnose(LMSTUDIO_UNLOADED)

        assert diagnosis is not None
        assert diagnosis.code == "MODEL_NOT_LOADED"
        assert diagnosis.retryable is True

    def test_every_diagnosis_says_what_to_do(self) -> None:
        """Un error que no sugiere salida obliga a abrir el código para saber
        por dónde seguir. La acción es la mitad que faltaba."""
        for text in (OLLAMA_OOM, LMSTUDIO_ENGINE, LMSTUDIO_UNLOADED, LMSTUDIO_GUARDRAIL):
            diagnosis = diagnose(text)
            assert diagnosis is not None
            assert len(diagnosis.action) > 40, text[:60]


class TestUnknownFailures:
    def test_an_unrecognised_failure_is_left_exactly_as_it_was(self) -> None:
        """La firma de mañana no está en la tabla. Inventar un diagnóstico para
        lo que no se reconoce sería peor que el mensaje feo de siempre."""
        assert diagnose(NVIDIA_DEGRADED) is None
        assert diagnose("") is None
        assert diagnose(None) is None


class TestErrorConstruction:
    def _http_error(self, status: int, body: str) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
        response = httpx.Response(status, text=body, request=request)
        return httpx.HTTPStatusError("boom", request=request, response=response)

    def test_a_diagnosed_error_keeps_the_original_text_as_evidence(self) -> None:
        """El diagnóstico ayuda a leer, no sustituye a la prueba: sin el texto
        original no se puede comprobar que el diagnóstico acertó."""
        error = provider_error_from_http(
            self._http_error(500, '{"error":"cudaMalloc failed: out of memory"}'),
            provider="ollama", model="laguna-s:q4",
        )

        assert error.code == "MODEL_OUT_OF_MEMORY"
        assert "cudaMalloc" in error.details["provider_error"]
        assert error.details["http_status"] == 500
        # Y nombra al modelo: con varios en juego, saber cuál falló es la mitad.
        assert "laguna-s:q4" in str(error)

    def test_an_undiagnosed_error_keeps_the_previous_behaviour(self) -> None:
        error = provider_error_from_http(
            self._http_error(400, '{"detail":"algo raro y nuevo"}'),
            provider="nvidia",
        )

        assert error.code == "MODEL_ERROR"
        assert "algo raro y nuevo" in str(error)
        assert error.retryable is False

    def test_retryability_comes_from_the_diagnosis_not_the_status_code(self) -> None:
        """Un 500 por falta de memoria no mejora reintentando; un 400 de motor
        caído sí puede. El código HTTP por sí solo se equivoca en los dos."""
        sin_memoria = provider_error_from_http(
            self._http_error(500, "cudaMalloc failed: out of memory"), provider="ollama",
        )
        motor_caido = provider_error_from_http(
            self._http_error(400, "Engine protocol predict request failed: fetch failed"),
            provider="lmstudio",
        )

        assert sin_memoria.retryable is False
        assert motor_caido.retryable is True


class TestReasoningOnly:
    def test_an_answer_that_arrived_as_reasoning_is_named_as_such(self) -> None:
        """El caso más desconcertante del histórico: 200, tokens cobrados, la
        interfaz del proveedor enseña el texto, y el broker decía «no devolvió
        contenido»."""
        diagnosis = reasoning_only_error(572, 193)

        assert diagnosis.code == "MODEL_RETURNED_ONLY_REASONING"
        assert "572 caracteres" in diagnosis.summary
        assert "193 tokens" in diagnosis.summary

    def test_it_works_without_the_token_count(self) -> None:
        """No todos los proveedores desglosan `reasoning_tokens`."""
        diagnosis = reasoning_only_error(300, None)

        assert "300 caracteres" in diagnosis.summary
        assert "tokens" not in diagnosis.summary.split("caracteres")[0]


class TestLiveProviderPaths:
    """Que los adapters usen de verdad el diagnóstico, no solo que exista."""

    @staticmethod
    def _provider(handler):
        from app.config import OpenAICompatibleProviderConfig
        from app.providers import OpenAICompatibleProvider

        config = OpenAICompatibleProviderConfig(
            id="lmstudio", enabled=True, base_url="http://127.0.0.1:1234/v1",
            api_key_env=None, deployment="local",
            # Como el LM Studio real: el catálogo se lee del proveedor.
            sync_models=True, catalog_cache_seconds=0,
        )
        return OpenAICompatibleProvider(config, transport=httpx.MockTransport(handler))

    @staticmethod
    def _request():
        from app.schemas import TaskCreateRequest

        return TaskCreateRequest(
            idempotency_key="diag:test", content={"prompt": "hola"},
            model_requirements={"allowed_providers": ["lmstudio"]},
        )

    @staticmethod
    def _reasoning_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(200, json={
            "choices": [{"message": {
                "role": "assistant", "content": "",
                "reasoning_content": "¡Hola! Puedo ayudarte con muchas cosas.",
            }, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 53, "completion_tokens": 194,
                      "completion_tokens_details": {"reasoning_tokens": 193}},
        })

    def test_an_answer_that_arrived_as_reasoning_is_rescued_and_marked(self) -> None:
        """La respuesta existe y ya se ha pagado: darla por perdida sería tirar
        trabajo hecho. Se entrega, pero marcada — sin la marca, un modelo que
        nunca produce contenido pasaría por sano al evaluarlo."""
        import asyncio

        provider = self._provider(self._reasoning_handler)

        async def scenario():
            output = await provider.generate(self._request(), "m", "hola")
            await provider.close()
            return output

        output = asyncio.run(scenario())
        assert output.content is not None and "Puedo ayudarte" in output.content
        assert output.content_source == "reasoning_content"
        # Y la marca sobrevive a la persistencia, que es donde se consulta luego.
        assert output.technical_output()["content_source"] == "reasoning_content"

    def test_a_normal_answer_is_not_marked_as_rescued(self) -> None:
        import asyncio

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "m"}]})
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "Hola",
                                         "reasoning_content": "pensando..."}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            })

        provider = self._provider(handler)

        async def scenario():
            output = await provider.generate(self._request(), "m", "hola")
            await provider.close()
            return output

        output = asyncio.run(scenario())
        # El rescate NUNCA pisa un contenido válido.
        assert output.content == "Hola"
        assert output.content_source == "content"
        assert "content_source" not in output.technical_output()

    def test_with_the_rescue_off_the_typed_error_comes_back(self) -> None:
        """Se apaga por proveedor para poder exigir contenido limpio en una
        tanda de medición sin cambiar el resto."""
        import asyncio

        from app.config import OpenAICompatibleProviderConfig
        from app.providers import OpenAICompatibleProvider

        config = OpenAICompatibleProviderConfig(
            id="lmstudio", enabled=True, base_url="http://127.0.0.1:1234/v1",
            api_key_env=None, deployment="local", sync_models=True,
            catalog_cache_seconds=0, rescue_reasoning_content=False,
        )
        provider = OpenAICompatibleProvider(
            config, transport=httpx.MockTransport(self._reasoning_handler),
        )

        async def scenario():
            with pytest.raises(ProviderError) as error:
                await provider.generate(self._request(), "m", "hola")
            await provider.close()
            return error.value

        error = asyncio.run(scenario())
        assert error.code == "MODEL_RETURNED_ONLY_REASONING"
        assert "Puedo ayudarte" in error.details["reasoning_excerpt"]

    def test_an_empty_answer_without_reasoning_explains_why_it_is_empty(self) -> None:
        import asyncio

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "m"}]})
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": ""},
                             "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4000},
            })

        provider = self._provider(handler)

        async def scenario():
            with pytest.raises(ProviderError) as error:
                await provider.generate(self._request(), "m", "hola")
            await provider.close()
            return error.value

        error = asyncio.run(scenario())
        assert error.code == "INVALID_PROVIDER_RESPONSE"
        # Dice por qué paró y que se facturó: sin eso, "sin contenido" no
        # distingue un modelo mudo de uno que agotó el presupuesto.
        assert "finish_reason=length" in str(error)
        assert "4000" in str(error)


class TestTemplateMarkerLeak:
    """Fuga del formato de conversación en la respuesta.

    Calibrado contra las 517 salidas de texto del histórico: dispara en 33,
    todas del mismo modelo mal empaquetado (plantilla passthrough, cero
    secuencias de parada), y en ninguna respuesta legítima.

    Lo que se protege sobre todo: **no perder respuestas buenas**. De las 33, la
    parte anterior a la marca es la contestación correcta en todas — incluidas
    las de un solo carácter.
    """

    def _leak(self, text: str):
        from app.providers.base import template_marker_leak

        return template_marker_leak(text)

    def test_the_answer_before_the_leak_is_what_gets_recovered(self) -> None:
        """Caso literal del histórico: el modelo contestó bien en 107
        caracteres respetando el «20 palabras o menos», y luego repitió lo mismo
        124 veces hasta llenar 14.374."""
        bueno = "El IVA repercutido es el impuesto que el vendedor cobra al cliente."
        salida = bueno + "</assistant>" + (bueno + "</assistant>") * 60

        resultado = self._leak(salida)

        assert resultado is not None
        limpio, marca = resultado
        assert limpio == bueno
        assert marca == "</assistant>"

    def test_a_one_character_answer_is_not_thrown_away(self) -> None:
        """El caso que descartó cualquier umbral de longitud: a «responde solo
        con la letra», la respuesta correcta ocupa un carácter."""
        for respuesta in ("b", "210", "7"):
            resultado = self._leak(f"{respuesta}</assistant>{respuesta}</assistant>")
            assert resultado is not None, respuesta
            assert resultado[0] == respuesta

    def test_a_marker_inside_code_is_legitimate_content(self) -> None:
        """La única respuesta legítima del histórico que contenía una marca: un
        modelo explicando qué hace ese token. Va entre comillas de código."""
        legitima = (
            "Cuando la probabilidad de generar el primer token es errática, el modelo "
            "puede detenerse inmediatamente (`<|endoftext|>`)."
        )

        assert self._leak(legitima) is None

    def test_a_marker_in_a_fenced_block_is_also_left_alone(self) -> None:
        respuesta = (
            "Para plantillas ChatML el formato es:\n\n"
            "```\n<|im_start|>user\nhola<|im_end|>\n```\n\n"
            "Y así se cierra cada turno."
        )

        assert self._leak(respuesta) is None

    def test_a_single_passing_mention_does_not_fire(self) -> None:
        """La cautela que no cambia el resultado en el corpus pero separa una
        fuga de un modelo que nombró una etiqueta de pasada."""
        texto = (
            "El token </s> marca el final de una secuencia en algunos modelos. "
            + "Explicado de otra manera, sirve como delimitador. " * 12
        )

        assert self._leak(texto) is None

    def test_a_marker_at_the_very_end_fires_even_if_it_appears_once(self) -> None:
        """En la cola es donde el modelo cierra su turno: ahí una marca suelta
        ya es inequívoca."""
        resultado = self._leak("La respuesta es 42.</assistant>")

        assert resultado is not None
        assert resultado[0] == "La respuesta es 42."

    def test_nothing_left_after_trimming_is_not_a_rescue(self) -> None:
        """Sin nada delante de la marca no hay respuesta que recuperar: lo
        diagnostica quien llama, no se devuelve una cadena vacía."""
        assert self._leak("</assistant></assistant></assistant>") is None

    def test_a_clean_answer_is_untouched(self) -> None:
        assert self._leak("Una respuesta normal y corriente, sin marcas.") is None
        assert self._leak("") is None

    def test_the_repair_marks_and_keeps_the_original(self) -> None:
        """La reparación no puede borrar la prueba de que el modelo está mal
        empaquetado: es justo lo que hay que ver al evaluarlo."""
        import asyncio

        from app.config import BrokerConfig
        from app.providers import RoutedModelProvider
        from app.providers.base import ModelOutput
        from app.schemas import ModelReference, TaskCreateRequest

        sucia = "210" + "</assistant>210" * 40

        class _Stub(RoutedModelProvider):
            async def _generate_raw(self, *args, **kwargs):
                return ModelOutput(sucia, 10, 20, 0.0, 5.0)

        provider = _Stub(BrokerConfig())
        request = TaskCreateRequest(
            idempotency_key="leak:repair", content={"prompt": "¿cuánto es el 21% de 1000?"},
        )
        model = ModelReference(provider="ollama", deployment="local", model="roto")

        output = asyncio.run(provider._generate(request, model, "¿cuánto es el 21% de 1000?"))

        assert output.content == "210"
        assert output.content_source == "content_truncated_at_template_marker"
        # El original completo sobrevive en lo que se persiste.
        assert output.raw_content == sucia
        assert output.technical_output()["raw_content"] == sucia
