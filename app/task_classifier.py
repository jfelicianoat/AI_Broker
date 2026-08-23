"""Clasificación heurística del tipo de tarea, para segmentar las métricas de
enrutamiento (app.model_stats) por naturaleza de la petición.

Un mismo modelo puede rendir muy distinto en código, prosa y contexto largo;
agregar todas las invocaciones en un único score (como hacía routing.py hasta
ahora) esconde esa diferencia y deja que un modelo bueno en resúmenes cortos
"gane" también las tareas de código solo por tener más volumen. La
clasificación es barata y determinista (regex sobre el prompt), en la misma
línea que app.strategy_router: no llama a ningún modelo ni añade latencia.

"code" significa que la tarea PRODUCE o MANIPULA código fuente, no que hable de
programación. La diferencia importa porque de ella depende que la respuesta la
escriba un modelo especializado en código: uno de esos redactando prosa técnica
en español es la receta de una mala respuesta, y hablar de LLMs, de APIs o de
algoritmos es prosa por mucho vocabulario técnico que lleve.
"""
from __future__ import annotations

import re
import unicodedata

from app.schemas import TaskCreateRequest


def _fold(text: str) -> str:
    """Sin tildes y en minúsculas, para casar una sola forma de cada término.

    "depúralo" y "depura", "función" y "funcion" son la misma señal escrita de
    dos maneras; sin plegar habría que listar cada variante y siempre faltaría
    alguna.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))

_CODE_FENCE_RE = re.compile(r"```")
_CODE_EXTENSION_RE = re.compile(
    r"\.(py|js|ts|tsx|jsx|java|go|rs|cpp|cc|c|h|hpp|rb|php|cs|kt|swift|sql|sh|yaml|yml|json)\b",
    re.IGNORECASE,
)

# Señales que por sí solas significan código: o son sintaxis, o son artefactos
# que solo existen alrededor de código fuente. Ninguna aparece por casualidad
# en una explicación.
_STRONG_CODE_TERMS = (
    "traceback", "stack trace", "docstring", "pytest", "unit test",
    "pull request", "refactoriza", "refactor", "npm ", "git ",
)
_STRONG_CODE_PATTERNS = (
    re.compile(r"\bdef\s+\w+\s*\("),
    re.compile(r"\bclass\s+\w+\s*[(:]"),
    re.compile(r"^\s*(import|from)\s+\w+", re.MULTILINE),
    re.compile(r"\w+\s*\([^)]*\)\s*[{;]"),
)

# Vocabulario técnico que aparece tanto escribiendo código como hablando de
# programación: "código postal", "programa de televisión", "variable
# aleatoria", "muchas implementaciones rechazan...". Por sí solo no clasifica
# nada; necesita un verbo que pida trabajo sobre código.
_WEAK_CODE_TERMS = (
    "codigo", "funcion", "function", "clase", "class", "script", "programa",
    "implementacion", "algoritmo", "endpoint", "regex", "repositorio",
    "commit", "api rest", "variable", "bug", "compilacion", "code",
    # Nombrar un lenguaje no basta por sí solo ("un libro sobre Python"), pero
    # acompañado de un encargo no deja lugar a dudas.
    "python", "javascript", "typescript", "java", "kotlin", "swift", "rust",
    "golang", "sql", "bash", "powershell", "html", "css", "php", "ruby",
)
# Verbos de encargo: lo que convierte "una función" en "escribe una función".
_CODE_ACTIONS = (
    "escribe", "crea", "genera", "implementa", "programa", "codifica",
    "corrige", "arregla", "depura", "debuggea", "debug", "optimiza",
    "refactoriza", "revisa", "migra", "compila", "ejecuta", "haz", "hazme",
    "dame", "monta", "prepara", "necesito", "anade", "elimina", "modifica",
    "actualiza", "write", "create", "generate", "implement", "fix",
    "refactor", "optimize", "review", "add", "remove", "update", "port",
)
# Pronombres enclíticos: "depúralo", "arréglalo", "escríbeme" son el mismo
# encargo que "depura", "arregla", "escribe".
_ENCLITICS = "(?:se|me|te|nos|le|les|lo|la|los|las)?" * 2


def _word_pattern(terms: tuple[str, ...], suffix: str = "") -> re.Pattern[str]:
    """Términos anclados a límite de palabra, sobre el texto ya plegado.

    Sin el anclaje, "implementa" casa dentro de "implementaciones" y una
    explicación sobre LLMs acaba enrutada a un modelo de código: el caso real
    que motivó esto.
    """
    alternatives = "|".join(re.escape(_fold(term.strip())) for term in terms)
    return re.compile(rf"(?<!\w)(?:{alternatives}){suffix}(?!\w)")


_WEAK_CODE_RE = _word_pattern(_WEAK_CODE_TERMS)
_CODE_ACTIONS_RE = _word_pattern(_CODE_ACTIONS, _ENCLITICS)
_STRONG_CODE_RE = _word_pattern(_STRONG_CODE_TERMS)

# Bloques que el cliente adjunta como CONTEXTO: historial de conversación,
# memoria aprobada, ámbito de ficheros. Describen lo que se hizo antes, no lo
# que se pide ahora, y clasificar con ellos deja que un mensaje viejo decida el
# enrutamiento de la petición de hoy.
_CONTEXT_BLOCK_RE = re.compile(
    r"<(\w*(?:history|memory|scope|attachment|context)\w*)_json>.*?</\1_json>",
    re.IGNORECASE | re.DOTALL,
)

# Umbral conservador (tokens estimados, cota superior): por encima, la tarea
# depende de que el modelo maneje bien ventanas grandes, aunque no sea código.
LONG_CONTEXT_TOKEN_THRESHOLD = 6000

TASK_TYPES = ("code", "long_context", "prose")

# --------------------------------------------------- peticiones sobre imágenes
#
# Producir una imagen es una capacidad, no un estilo de respuesta: un modelo de
# texto al que se le pide un logotipo no falla, entrega un párrafo describiendo
# el logotipo que no ha hecho. Detectar aquí la intención permite exigir esa
# capacidad al enrutar y, cuando no hay ningún modelo que la tenga, decirlo en
# vez de entregar el sucedáneo.
#
# El criterio es deliberadamente estrecho: un falso positivo tumba una tarea
# que el broker sabía atender ("resume la imagen adjunta" no pide una imagen
# nueva), y ese daño es peor que el de un falso negativo, que como mucho
# devuelve texto donde se quería un dibujo.

_IMAGE_NOUNS = (
    "imagen", "imagenes", "foto", "fotos", "fotografia", "fotografias",
    "ilustracion", "ilustraciones", "dibujo", "dibujos", "retrato", "boceto",
    "wallpaper", "avatar",
    "image", "images", "picture", "pictures", "photo", "photos",
    "illustration", "drawing", "artwork", "sketch",
)
# Fuera quedan a propósito "logo", "icono", "cartel" y "diagrama": se piden
# tanto como imagen como en formatos que un modelo de texto sí sabe escribir
# (SVG, Mermaid), y exigir por ellos un modelo de imagen convertiría en
# irresoluble una tarea que hoy se atiende bien.

# Verbos que solo se usan para hacer una imagen, sin necesidad de que el objeto
# esté nombrado ("dibújame un gato" no dice "imagen" por ninguna parte). La
# lista es corta porque casi todos los candidatos tienen otro uso: "ilustra con
# un ejemplo", "renderiza el HTML", "draw a conclusion".
_IMAGE_ONLY_VERBS = ("dibuja", "dibujar", "pinta", "pintar")

# Verbos de encargo genéricos: solo cuentan si el objeto es una imagen y está
# cerca ("genera una imagen de…", no "haz un resumen de la imagen").
_IMAGE_CREATE_VERBS = (
    "genera", "generar", "crea", "crear", "haz", "disena", "disenar",
    "elabora", "produce", "generate", "create", "make", "design", "draw",
    "paint", "illustrate",
)

# Operaciones sobre una imagen que ya existe. Igual de estrechas: fuera quedan
# "cambia", "convierte" y "transforma", que aparecen mucho más en peticiones
# que NO quieren una imagen de vuelta ("convierte la imagen a texto").
_IMAGE_EDIT_VERBS = (
    "edita", "editar", "modifica", "modificar", "retoca", "retocar",
    "recorta", "recortar", "redimensiona", "escala", "amplia", "rota",
    "gira", "voltea", "difumina", "desenfoca", "colorea", "restaura",
    "mejora", "edit", "modify", "crop", "resize", "rotate", "blur",
    "colorize", "upscale", "enhance", "restore",
)

# Encargos de edición que no nombran la imagen porque no hace falta: solo se
# le hacen a una.
_IMAGE_EDIT_PHRASES = (
    "quita el fondo", "quitale el fondo", "elimina el fondo", "borra el fondo",
    "remove the background", "quita la marca de agua",
)

_DETERMINERS = r"(?:el|la|los|las|un|una|unos|unas|este|esta|estos|estas|ese|esa|mi|mis|the|this|these|a|an|my)"

# Verbo de creación + hasta tres palabras + sustantivo de imagen. El hueco es
# corto a propósito: "haz un resumen de la imagen" mete cuatro palabras entre
# el verbo y el sustantivo y se queda fuera, que es justo lo que se quiere.
_IMAGE_CREATE_RE = re.compile(
    rf"(?<!\w)(?:{'|'.join(_IMAGE_CREATE_VERBS)}){_ENCLITICS}(?!\w)"
    rf"(?:\W+\w+){{0,3}}\W+(?:{'|'.join(_IMAGE_NOUNS)})(?!\w)"
)
_IMAGE_ONLY_VERB_RE = _word_pattern(_IMAGE_ONLY_VERBS, _ENCLITICS)
# Verbo de edición + determinante opcional + la imagen, sin hueco libre: el
# objeto tiene que ser ella ("recorta la foto"), no algo que la acompaña
# ("modifica el código según la imagen adjunta").
_IMAGE_EDIT_RE = re.compile(
    rf"(?<!\w)(?:{'|'.join(_IMAGE_EDIT_VERBS)}){_ENCLITICS}(?!\w)"
    rf"(?:\W+{_DETERMINERS})?\W+(?:{'|'.join(_IMAGE_NOUNS)})(?!\w)"
)
_IMAGE_EDIT_PHRASE_RE = re.compile(
    "|".join(re.escape(_fold(phrase)) for phrase in _IMAGE_EDIT_PHRASES)
)


# Peticiones de "dime qué pone aquí". Cuentan por separado de todo lo anterior
# porque tienen una salida que el broker sabe producir SIN modelo de visión: el
# OCR local. Es el único caso en el que una imagen puede degradarse a texto sin
# traicionar la petición — si la pregunta fuera "¿de qué color es el gato?", el
# texto reconocido no contestaría nada.
_OCR_TERMS = (
    "ocr", "transcribe", "transcribir", "transcripcion", "transcribeme",
    "reconocimiento optico", "optical character",
)
_OCR_PHRASES = (
    "extrae el texto", "extraer el texto", "extraeme el texto", "saca el texto",
    "sacar el texto", "lee el texto", "leer el texto", "leeme el texto",
    "dime que pone", "que pone en", "que dice el texto", "que texto tiene",
    "texto de la imagen", "texto de la foto", "texto que aparece",
    "extract the text", "read the text", "text in the image", "what does it say",
    "copia el texto", "pasame el texto", "pasa a texto",
)
_OCR_TERMS_RE = _word_pattern(_OCR_TERMS)
_OCR_PHRASES_RE = re.compile("|".join(re.escape(_fold(item)) for item in _OCR_PHRASES))


def classify_ocr_intent(request: TaskCreateRequest) -> bool:
    """True si lo que se pide es el texto que hay dentro de la imagen.

    Generoso a propósito, al revés que `classify_image_intent`: aquí el falso
    positivo solo hace que se intente un OCR de más, mientras que el falso
    negativo deja la tarea sin respuesta cuando no hay modelo con visión.
    """
    text = _fold(request_text(request.content.prompt))
    return bool(_OCR_TERMS_RE.search(text) or _OCR_PHRASES_RE.search(text))


def classify_image_intent(request: TaskCreateRequest) -> str | None:
    """"generate" | "edit" | None: qué quiere la petición que SALGA una imagen.

    "edit" solo cuando además hay una imagen adjunta sobre la que operar. Sin
    ella no se asciende a "generate": "recorta la foto" sin foto no es un
    encargo de dibujar nada, es una petición a la que le falta el adjunto, y
    tratarla como generación la tumbaría con un error sobre modelos de imagen
    en vez de dejar que el modelo pida lo que falta.
    """
    text = _fold(request_text(request.content.prompt))
    has_image = bool(getattr(request, "inline_images", None))
    if has_image and (_IMAGE_EDIT_RE.search(text) or _IMAGE_EDIT_PHRASE_RE.search(text)):
        return "edit"
    if _IMAGE_ONLY_VERB_RE.search(text) or _IMAGE_CREATE_RE.search(text):
        return "generate"
    return None


def request_text(prompt: str) -> str:
    """Lo que el usuario pide de verdad, sin los bloques de contexto adjunto."""
    return _CONTEXT_BLOCK_RE.sub(" ", prompt)


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

    petition = request_text(request.content.prompt)
    if _has_code_signal(petition):
        return "code"
    if estimate_required_context(request) >= LONG_CONTEXT_TOKEN_THRESHOLD:
        return "long_context"
    return "prose"


def _has_code_signal(text: str) -> bool:
    """Código de verdad: o hay sintaxis/artefactos, o se pide trabajo sobre él."""
    if _CODE_FENCE_RE.search(text) or _CODE_EXTENSION_RE.search(text):
        return True
    if any(pattern.search(text) for pattern in _STRONG_CODE_PATTERNS):
        return True
    text = _fold(text)
    if _STRONG_CODE_RE.search(text):
        return True
    # El vocabulario débil solo cuenta acompañado de un encargo: "implementa
    # una función" es código, "muchas implementaciones rechazan prompts
    # vacíos" es una explicación.
    #
    # Tienen que ser dos palabras distintas. "programa" está en las dos listas
    # —es sustantivo y es verbo—, así que sin comparar posiciones se validaría
    # a sí misma y "resume el programa de televisión" acabaría en un modelo de
    # código.
    weak_spans = {match.span() for match in _WEAK_CODE_RE.finditer(text)}
    action_spans = {match.span() for match in _CODE_ACTIONS_RE.finditer(text)}
    return any(action != weak for action in action_spans for weak in weak_spans)
