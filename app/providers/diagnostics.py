"""Traducción de fallos de proveedor a diagnósticos accionables.

El problema que resuelve, medido sobre el histórico real de esta máquina: los
errores que el broker diagnostica por sí mismo son claros («X necesita 89.4 GB y
el presupuesto es 62.0 GB: no cabe ni con la máquina vacía»), y los que vienen
de un proveedor se entregaban en crudo. El más repetido, 31 veces:

    HTTP 500 en http://127.0.0.1:11434/api/chat: {"error":"llama-server process
    has terminated: exit status 1: cudaMalloc failed: out of memory\\n
    alloc_tensor_range: failed to allocate ROCm0 buffer of size 92469490688"}

Ahí dentro está el diagnóstico —se quedó sin memoria— enterrado en un volcado
JSON con una dirección IP, un código de salida y un número de bytes. Quien lo
lee tiene que ser detective, y el código genérico (`MODEL_ERROR`) no permite
distinguir «este modelo no cabe» de «el proveedor está mal configurado» ni en
las métricas ni en la cuarentena.

Aquí se reconocen las firmas conocidas y se convierten en un código específico
y una frase que dice **qué pasó y qué hacer**. Reglas de la casa:

- **El texto original nunca se pierde**: viaja en `details["provider_error"]`.
  Un diagnóstico es una ayuda, no una censura, y la firma de mañana no estará
  en esta tabla.
- **Solo se afirma lo que la firma demuestra.** Un fallo que no se reconoce se
  queda como estaba: es preferible un mensaje feo a uno seguro de sí mismo y
  equivocado.
- Cada entrada de la tabla sale de un fallo que ocurrió de verdad, no de
  imaginar qué podría fallar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnosis:
    code: str
    # Qué pasó, en una frase, sin jerga del runtime.
    summary: str
    # Qué hacer al respecto. Es la mitad que faltaba: un error que no sugiere
    # salida obliga a abrir el código para saber por dónde seguir.
    action: str
    # ¿Tiene sentido reintentar tal cual? Un modelo que no cabe no cambia por
    # insistir; un motor que se cayó puede volver.
    retryable: bool = False

    def message(self, provider: str, model: str | None = None) -> str:
        subject = f"{provider}/{model}" if model else provider
        return f"{subject}: {self.summary} {self.action}"


# Firmas observadas en fallos reales. El orden importa: la primera que casa
# gana, así que lo específico va antes que lo general.
_SIGNATURES: tuple[tuple[re.Pattern[str], Diagnosis], ...] = (
    (
        # Ollama/llama.cpp agotando memoria del acelerador. La firma aparece en
        # dos variantes ("process has terminated" al cargar, "reported
        # out-of-memory during startup") y ambas significan lo mismo.
        re.compile(r"cudaMalloc failed|out of memory|failed to allocate .*buffer", re.I),
        Diagnosis(
            "MODEL_OUT_OF_MEMORY",
            "el runtime se quedó sin memoria al cargar o ejecutar el modelo.",
            "Es memoria del acelerador, no del broker: el modelo entró en la admisión pero "
            "no cupo de verdad. Revisa resources.local_vram_budget_gb (debe describir la "
            "memoria que existe), y si el modelo es mayor que el presupuesto, "
            "resources.gpu_layer_offload lo reparte entre GPU y CPU.",
        ),
    ),
    (
        # LM Studio con su guardarraíl de memoria: se niega a cargar y dice
        # cuánto necesitaría. Es el mismo problema que el anterior, detectado
        # antes de romper nada.
        re.compile(r"insufficient system resources|requires approximately", re.I),
        Diagnosis(
            "MODEL_OUT_OF_MEMORY",
            "el proveedor se negó a cargar el modelo porque no cabe en la memoria de esta máquina.",
            "La cifra que da suele estar dominada por el contexto, no por los pesos: un modelo "
            "que declara 262.144 tokens reserva caché para todos. Cárgalo con un contexto menor "
            "desde la interfaz del proveedor.",
        ),
    ),
    (
        # LM Studio cuando su motor muere a mitad de petición.
        re.compile(r"Engine protocol .*failed|fetch failed", re.I),
        Diagnosis(
            "PROVIDER_ENGINE_CRASHED",
            "el motor del proveedor se cayó mientras atendía la petición.",
            "No es un fallo del prompt: la petición no llegó a completarse. Suele ir "
            "acompañado de falta de memoria con ese modelo. Comprueba en el proveedor que el "
            "modelo sigue cargado y con qué contexto.",
            retryable=True,
        ),
    ),
    (
        re.compile(r"Model is unloaded|model not loaded", re.I),
        Diagnosis(
            "MODEL_NOT_LOADED",
            "el modelo no está cargado en el proveedor.",
            "Puede haberse descargado solo tras un fallo anterior. Cárgalo desde el proveedor, "
            "o revisa si se está cayendo al cargar.",
            retryable=True,
        ),
    ),
    (
        re.compile(r"Failed to load model", re.I),
        Diagnosis(
            "MODEL_LOAD_FAILED",
            "el proveedor no pudo cargar el modelo.",
            "El fichero puede faltar, estar corrupto, o no caber. El texto original de abajo "
            "trae el motivo exacto del proveedor.",
        ),
    ),
    (
        re.compile(r"context length|too many tokens|maximum context", re.I),
        Diagnosis(
            "CONTEXT_LIMIT_EXCEEDED",
            "la petición supera el contexto que el proveedor admite para ese modelo.",
            "El broker estima el contexto antes de invocar; si aquí falla, la ventana declarada "
            "en el catálogo es mayor que la real. Ajusta context_window de ese modelo en la "
            "configuración del proveedor.",
        ),
    ),
    (
        re.compile(r"\bAPI key\b|unauthorized|invalid.*token|authentication", re.I),
        Diagnosis(
            "CREDENTIALS_REJECTED",
            "el proveedor rechazó las credenciales.",
            "La clave existe pero no vale: caducada, revocada o de otra cuenta. Revísala en la "
            "variable de entorno o el llavero que declara ese proveedor.",
        ),
    ),
    (
        re.compile(r"rate.?limit|too many requests|quota", re.I),
        Diagnosis(
            "RATE_LIMITED",
            "el proveedor está limitando la frecuencia de peticiones.",
            "Espera antes de reintentar, o reparte la carga entre varios modelos.",
            retryable=True,
        ),
    ),
)


def diagnose(text: str | None) -> Diagnosis | None:
    """Diagnóstico de un fallo de proveedor, o None si no se reconoce.

    Devolver None es un resultado legítimo y frecuente: significa que el texto
    original describe el problema mejor que cualquier cosa que se pudiera
    inventar aquí."""
    if not text:
        return None
    for pattern, diagnosis in _SIGNATURES:
        if pattern.search(text):
            return diagnosis
    return None


def reasoning_only_error(reasoning_chars: int, reasoning_tokens: int | None) -> Diagnosis:
    """El modelo respondió, pero en el campo de razonamiento.

    Pasa de verdad y es de los fallos más desconcertantes que hay: el proveedor
    devuelve 200, cobra los tokens, y el broker informa de que «no devolvió
    contenido». Es cierto —`content` viene vacío— y a la vez engañoso, porque la
    respuesta está ahí, en `reasoning_content`, que es donde la interfaz del
    propio proveedor la enseña. Sin decirlo, la única salida es ponerse a
    comparar respuestas HTTP a mano."""
    medido = f"{reasoning_chars} caracteres"
    if reasoning_tokens:
        medido += f" ({reasoning_tokens} tokens)"
    return Diagnosis(
        "MODEL_RETURNED_ONLY_REASONING",
        f"el modelo devolvió su respuesta como razonamiento ({medido}) y dejó el contenido vacío.",
        "La respuesta existe y se ha pagado, pero llega por un campo que el contrato no trata "
        "como contenido. Suele indicar que el modelo está mal empaquetado (su plantilla no "
        "cierra el bloque de razonamiento) o que el proveedor cambió cómo lo separa.",
    )
