"""Utilidades de módulo que comparten el coordinador y sus mixins por estrategia.

Salieron de app/coordinator.py al dividirlo (AB-08). Viven aquí y no en un
mixin para que ningún mixin tenga que importar de otro ni del coordinador.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.providers import ModelOutput
from app.providers.base import estimate_tokens_upper_bound

# Margen sobre la ventana al medir una conversación agéntica: la estimación es
# una cota por bytes, no un tokenizer real, y la plantilla de chat del modelo
# añade lo suyo por cada mensaje.
from app.resource_scheduler import ResourcePlan
from app.schemas import ModelReference

_AGENT_CONTEXT_HEADROOM_TOKENS = 512

# Lo que sobrevive de un resultado de tool compactado: bastante para que el
# modelo recuerde qué consultó y con qué pinta volvió, no para releerlo entero.
_COMPACTED_TOOL_RESULT_CHARS = 400

# Los últimos resultados de tool no se compactan salvo como último recurso: son
# los que el modelo está usando para razonar en la vuelta actual.
_PROTECTED_TOOL_RESULTS = 2

def _conversation_tokens(messages: list[dict[str, Any]]) -> int:
    """Cota superior de los tokens que ocupa una conversación agéntica.

    Misma regla que el preflight de contexto (bytes UTF-8), aplicada al
    contenido de todos los mensajes y a los argumentos de las tool_calls, que
    también viajan y en una búsqueda larga no son despreciables.
    """
    total = 0
    for message in messages:
        total += estimate_tokens_upper_bound(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            total += estimate_tokens_upper_bound(
                f"{function.get('name') or ''}{function.get('arguments') or ''}"
            )
    return total

def _last_assistant_content(messages: list[dict[str, Any]]) -> str | None:
    """Lo último que dijo el modelo en una conversación agéntica, si dijo algo.

    Un turno con tool_calls puede traer texto además de las llamadas (un
    preámbulo, un avance parcial). Cuando el bucle se corta sin respuesta final
    eso es lo único aprovechable que hay sin pagar otra invocación.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = (message.get("content") or "").strip()
        if content:
            return content
    return None

# Campos de ModelOutput que sobreviven a un punto de control del consenso. Las
# imágenes y el embedding no: un mixture_of_agents produce texto, y las
# imágenes que sí importan —las de la síntesis— se guardan como artefactos al
# cerrar, no a mitad de camino.
_CHECKPOINT_OUTPUT_FIELDS = (
    "content", "tokens_input", "tokens_output", "cost_usd", "latency_ms", "generation",
    "fingerprint_observed", "fingerprint_extensions", "content_source", "raw_content",
)

def _output_to_checkpoint(output: ModelOutput) -> dict[str, Any]:
    return {name: getattr(output, name) for name in _CHECKPOINT_OUTPUT_FIELDS}

def _chunks_fingerprint(chunks: list[str]) -> str:
    """Huella del troceado de un map-reduce. Los fragmentos no se guardan en el
    punto de control —son el documento entero otra vez—: al retomar se
    recalculan y esta huella dice si salieron iguales."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()

def _output_from_checkpoint(data: dict[str, Any]) -> ModelOutput:
    return ModelOutput(
        data.get("content"),
        int(data.get("tokens_input") or 0),
        int(data.get("tokens_output") or 0),
        float(data.get("cost_usd") or 0.0),
        float(data.get("latency_ms") or 0.0),
        generation=data.get("generation"),
        fingerprint_observed=data.get("fingerprint_observed"),
        fingerprint_extensions=data.get("fingerprint_extensions"),
        content_source=data.get("content_source"),
        raw_content=data.get("raw_content"),
    )

@dataclass
class AgentLoopResult:
    """Resultado de un bucle de tool-calling: la respuesta final del modelo,
    las invocaciones LLM realizadas (para coste/artefactos) y por qué paró."""
    content: str | None
    outputs: list[ModelOutput] = field(default_factory=list)
    # completed | max_iterations | budget_exhausted | context_exhausted |
    # cancelled | waiting_for_tools | yielded
    stop_reason: str = "completed"
    tool_calls: int = 0
    last_invocation_id: str | None = None
    iteration: int = 0
    # Hubo turno de cierre sin tools (ver Coordinator._final_answer_turn). Es
    # una invocación más dentro de `outputs`, pero NO una ronda del bucle: quien
    # informe de iteraciones tiene que descontarla para no contradecir el tope.
    final_turn: bool = False
    # Solo en waiting_for_tools: llamadas del cliente pendientes y la
    # conversación completa a congelar para reanudar tras resolverlas.
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Solo en yielded: la tarea más urgente a la que se cedió el turno.
    yielded_to: str | None = None

@dataclass
class _MapReduceCheckpoint:
    """Lo que un map-reduce dejó hecho al ceder el turno.

    `phase` es "map" si cedió entre fragmentos y "reduce" si cedió entre
    reducciones; en ese caso `reduce_input` es la entrada de la ronda en curso
    y `reduce_merged` los grupos de esa ronda ya reducidos.
    """
    model: ModelReference
    window: int
    chunks: list[str]
    phase: str
    map_partials: list[str]
    outputs: list[ModelOutput]
    reduce_rounds: int = 0
    reduce_input: list[str] = field(default_factory=list)
    reduce_merged: list[str] = field(default_factory=list)


# Errores que hablan de la TAREA, no del modelo que los encontró: cambiar de
# proposer no arregla ninguno, así que seguir con los demás solo gastaría
# máquina para llegar al mismo sitio. Todo lo demás es una baja del proposer y
# lo resuelve el quorum.
_TASK_FATAL_ERRORS = frozenset({
    "TASK_CANCELLED",
    "BUDGET_EXCEEDED",
    "CONTEXT_LIMIT_EXCEEDED",
})


# En la síntesis el contexto ya no habla de la petición sino del árbitro: la
# petición cupo en los proponentes, y lo que no cabe es ella más sus
# propuestas en la ventana de ESE modelo. El sustituto lo elige el router con
# la petición original, así que podía tocarle uno de 8k (qwen3.6-35b-a3b en LM
# Studio) y su rechazo tumbaba la tarea con tres propuestas ya hechas: 16 de
# los 22 CONTEXT_LIMIT_EXCEEDED de la semana del 2026-10-18.
_ARBITER_FATAL_ERRORS = _TASK_FATAL_ERRORS - {"CONTEXT_LIMIT_EXCEEDED"}


# Candidatos que se piden al router al buscar repuesto: los que no tengan
# ventana para la síntesis se saltan, y hace falta margen para encontrar uno.
_ARBITER_SUBSTITUTE_POOL = 8


# Modelos que prueba una tarea single antes de rendirse: el elegido y dos
# relevos. Cancelación y presupuesto no se arreglan cambiando de modelo.
_SINGLE_FAILOVER_MODELS = 3


# Tampoco la falta de memoria momentánea: esa tarea espera a su modelo
# (waiting_for_memory) en vez de irse a otro.
_SINGLE_FATAL_ERRORS = frozenset({
    "TASK_CANCELLED", "BUDGET_EXCEEDED", "VRAM_INSUFFICIENT", "LOCAL_MODEL_SLOTS_BUSY",
})


@dataclass
class _RoundOutcome:
    """Lo que deja una ronda de propuestas del consenso."""
    proposals: list[tuple[ModelReference, ModelOutput]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    max_parallel_launched: int = 0
    # La cancelación sube por aquí en vez de por una excepción: el llamante ya
    # tiene que decidir qué escribe en la tarea, y son dos sitios distintos
    # según la ronda en la que se cancele.
    cancelled: bool = False
    # Ceder el turno sube igual: la tarea más urgente que espera y cuántas
    # oleadas quedaron hechas, que es desde donde se retoma.
    yielded_to: str | None = None
    waves_done: int = 0


@dataclass
class _SecondRound:
    """Lo que deja el intento de segunda ronda.

    `synthesis is None` significa que la tarea se queda con la ronda 1: puede
    ser porque el juez la dio por buena, porque no quedaba presupuesto o porque
    la ronda 2 falló. Los tres casos se ven igual desde fuera a propósito — la
    ronda 2 no puede empeorar el resultado— y se distinguen por los eventos.
    """
    confidence: float | None = None
    # Invocaciones ya pagadas que dejan de estar representadas en el resultado
    # (el juez, y las de la ronda 1 cuando la 2 la sustituye). Sin ellas el
    # coste que se informa sería menor que el real.
    extra_outputs: list[ModelOutput] = field(default_factory=list)
    proposals: list[tuple[ModelReference, ModelOutput]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    max_parallel_launched: int = 0
    synthesis: _Synthesis | None = None


@dataclass(frozen=True)
class _Synthesis:
    """Lo que cierra un consenso: la síntesis del árbitro o, si no la hubo, la
    propuesta que responde en su lugar."""
    model: ModelReference
    output: ModelOutput
    # None cuando degraded: la salida es una propuesta con su invocación ya
    # cerrada, no una inferencia nueva.
    invocation_id: str | None
    degraded: bool
    failures: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _ConsensusCheckpoint:
    """Lo que un mixture_of_agents dejó hecho al ceder el turno.

    `synthesis` es None si cedió durante la primera ronda de propuestas; si no,
    cedió tras la primera síntesis y lo que queda es la segunda ronda.
    """
    proposers: list[ModelReference]
    arbiter: ModelReference
    resource_plan: ResourcePlan
    round_one: _RoundOutcome
    synthesis: _Synthesis | None
