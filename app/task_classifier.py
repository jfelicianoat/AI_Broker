"""Clasificación heurística del tipo de tarea, para segmentar las métricas de
enrutamiento (app.model_stats) por naturaleza de la petición.

Un mismo modelo puede rendir muy distinto en código, prosa y contexto largo;
agregar todas las invocaciones en un único score (como hacía routing.py hasta
ahora) esconde esa diferencia y deja que un modelo bueno en resúmenes cortos
"gane" también las tareas de código solo por tener más volumen. La
clasificación es barata y determinista (regex sobre el prompt), en la misma
línea que app.strategy_router: no llama a ningún modelo ni añade latencia.
"""
from __future__ import annotations

import re

from app.schemas import TaskCreateRequest

_CODE_FENCE_RE = re.compile(r"```")
_CODE_EXTENSION_RE = re.compile(
    r"\.(py|js|ts|tsx|jsx|java|go|rs|cpp|cc|c|h|hpp|rb|php|cs|kt|swift|sql|sh|yaml|yml|json)\b",
    re.IGNORECASE,
)
# Señales de que la tarea es sobre código (generarlo, explicarlo, depurarlo).
_CODE_TERMS = (
    "código", "codigo", "función", "funcion", "clase ", "script", "programa",
    "implementa", "implementación", "refactoriza", "refactor", "depura",
    "debug", "bug", "traceback", "stack trace", "compila", "compilación",
    "endpoint", "regex", "algoritmo", "pytest", "unit test", "docstring",
    "repositorio", "pull request", "commit", "api rest",
    "code", "function", "class ", "import ", "def ", "variable",
)

# Umbral conservador (tokens estimados, cota superior): por encima, la tarea
# depende de que el modelo maneje bien ventanas grandes, aunque no sea código.
LONG_CONTEXT_TOKEN_THRESHOLD = 6000

TASK_TYPES = ("code", "long_context", "prose")


def classify_task_type(request: TaskCreateRequest) -> str:
    """"code" | "long_context" | "prose".

    Orden de prioridad: el código manda incluso si el prompt es largo (para
    enrutar bien importa más acertar el modelo de código que el de contexto
    largo); si no hay señal de código, decide el tamaño; si no, prosa por
    defecto.
    """
    # Import diferido: app.providers.base tira de app.providers, que importa
    # el router, que importa este módulo. Con el import arriba, el ciclo solo
    # se resolvía si algo cargaba antes app.providers (lo que hace el arranque
    # del broker, pero no un script que empiece por el clasificador).
    from app.providers.base import estimate_required_context

    prompt = request.content.prompt
    lowered = prompt.lower()
    has_code_signal = bool(
        _CODE_FENCE_RE.search(prompt)
        or _CODE_EXTENSION_RE.search(prompt)
        or any(term in lowered for term in _CODE_TERMS)
    )
    if has_code_signal:
        return "code"
    if estimate_required_context(request) >= LONG_CONTEXT_TOKEN_THRESHOLD:
        return "long_context"
    return "prose"
