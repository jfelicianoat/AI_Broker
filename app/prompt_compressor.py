"""Compresión de prompts antes de la inferencia.

Adapta a español las técnicas de caveman/caveman-micro/ponytail: eliminar
cortesías, muletillas y relleno sin tocar el contenido técnico. El código,
las URLs y los correos se protegen byte a byte; el prompt original se
conserva en la persistencia y solo se comprime lo que viaja al proveedor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("ai_broker.prompt_compressor")

# Si la compresión deja menos de este porcentaje del original, algo raro ha
# pasado (prompt patológico o léxico demasiado agresivo): se envía el original.
_MIN_SURVIVING_RATIO = 0.2

# Segmentos que nunca se comprimen: código, URLs y correos.
_PROTECTED_PATTERN = re.compile(
    r"```.*?```"          # bloques de código con fence
    r"|~~~.*?~~~"
    r"|`[^`\n]+`"         # código inline
    r"|https?://\S+"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+",
    re.DOTALL,
)

# Frases de cortesía y aperturas sociales: se eliminan enteras (light+).
_COURTESY_PHRASES = [
    r"por\s+favor,?",
    # Las variantes largas van antes que las cortas: en la alternancia gana la
    # primera que casa, y "muchas gracias" dejaría colgando "de antemano".
    r"(?:muchas\s+)?gracias\s+de\s+antemano\.?",
    r"(?:muchas\s+)?gracias\s+por\s+adelantado\.?",
    r"muchas\s+gracias\.?",
    r"te\s+lo\s+agradecer[ií]a( mucho)?\.?",
    r"si\s+eres\s+tan\s+amable,?",
    r"si\s+no\s+es\s+mucha\s+molestia,?",
    r"cuando\s+puedas,?",
    r"un\s+saludo\.?",
    r"saludos\s+cordiales\.?",
    r"hola,?",
    r"buenos\s+d[ií]as,?",
    r"buenas\s+tardes,?",
    r"buenas\s+noches,?",
    r"buenas,",
    r"thanks\s+in\s+advance\.?",
    r"please,?",
    r"kindly,?",
]

# Envoltorios de petición: convierten "¿Podrías resumir X?" en "resumir X".
# Solo formas que dejan detrás un verbo o sintagma autónomo (medium+).
_REQUEST_WRAPPERS = [
    r"¿?\s*podr[ií]as(?:\s+por\s+favor)?\s+",
    r"¿?\s*puedes(?:\s+por\s+favor)?\s+",
    r"¿?\s*ser[ií]as\s+capaz\s+de\s+",
    r"me\s+gustar[ií]a\s+que\s+",
    r"me\s+gustar[ií]a\s+",
    r"quisiera\s+que\s+",
    r"quisiera\s+",
    r"necesito\s+que\s+",
    r"quiero\s+que\s+",
    r"te\s+pido\s+que\s+",
    r"lo\s+que\s+quiero\s+es\s+que\s+",
    r"could\s+you(?:\s+please)?\s+",
    r"can\s+you(?:\s+please)?\s+",
    r"i\s+would\s+like\s+you\s+to\s+",
    r"i\s+need\s+you\s+to\s+",
]

# Muletillas y relleno seguros de eliminar en cualquier posición (medium+).
_FILLER_PHRASES = [
    r"b[aá]sicamente,?",
    r"realmente,?",
    r"simplemente,?",
    r"literalmente,?",
    r"obviamente,?",
    r"evidentemente,?",
    r"claramente,?",
    r"sinceramente,?",
    r"honestamente,?",
    r"de\s+hecho,?",
    r"en\s+realidad,?",
    r"la\s+verdad\s+es\s+que,?",
    r"cabe\s+destacar\s+que,?",
    r"cabe\s+mencionar\s+que,?",
    r"es\s+importante\s+mencionar\s+que,?",
    r"es\s+importante\s+destacar\s+que,?",
    r"como\s+ya\s+sabes,?",
    r"como\s+sabes,?",
    r"como\s+te\s+coment[eé],?",
    r"pues\s+bien,?",
    r"a\s+ver,?",
    r"en\s+definitiva,?",
    r"en\s+cualquier\s+caso,?",
    r"dicho\s+esto,?",
    r"sin\s+m[aá]s\s+dilaci[oó]n,?",
    r"actually,?",
    r"basically,?",
    r"really,?",
    r"just\s",
]

# Artículos y determinantes que el nivel aggressive elimina (estilo caveman).
_AGGRESSIVE_STOPWORDS = [
    "el", "la", "los", "las", "un", "una", "unos", "unas", "lo",
    "the", "a", "an",
]


def _compile_phrase_pattern(phrases: list[str]) -> re.Pattern[str]:
    joined = "|".join(f"(?:{phrase})" for phrase in phrases)
    return re.compile(rf"(?<![\w\d]){joined}(?![\w\d])", re.IGNORECASE)


_COURTESY_RE = _compile_phrase_pattern(_COURTESY_PHRASES)
_REQUEST_RE = _compile_phrase_pattern(_REQUEST_WRAPPERS)
_FILLER_RE = _compile_phrase_pattern(_FILLER_PHRASES)
_STOPWORD_RE = re.compile(
    r"\b(?:" + "|".join(_AGGRESSIVE_STOPWORDS) + r")\b\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompressionResult:
    text: str
    original_chars: int
    compressed_chars: int
    applied: bool

    @property
    def ratio(self) -> float:
        if self.original_chars == 0:
            return 1.0
        return self.compressed_chars / self.original_chars


class PromptCompressor:
    """Compresión determinista por reglas; sin llamadas a modelos."""

    LEVELS = ("light", "medium", "aggressive")

    def __init__(self, enabled: bool = True, level: str = "medium", min_chars: int = 40) -> None:
        if level not in self.LEVELS:
            raise ValueError(f"prompt_compression.level must be one of {self.LEVELS}")
        self.enabled = enabled
        self.level = level
        self.min_chars = max(0, min_chars)

    def compress(self, prompt: str) -> CompressionResult:
        original_chars = len(prompt)
        if not self.enabled or original_chars < self.min_chars:
            return CompressionResult(prompt, original_chars, original_chars, applied=False)

        protected: list[str] = []

        def _shelter(match: re.Match[str]) -> str:
            protected.append(match.group(0))
            return f"\x00{len(protected) - 1}\x00"

        working = _PROTECTED_PATTERN.sub(_shelter, prompt)

        working = _COURTESY_RE.sub(" ", working)
        if self.level in ("medium", "aggressive"):
            working = _REQUEST_RE.sub(" ", working)
            working = _FILLER_RE.sub(" ", working)
        if self.level == "aggressive":
            working = _STOPWORD_RE.sub("", working)

        working = self._normalize(working)

        for index, fragment in enumerate(protected):
            working = working.replace(f"\x00{index}\x00", fragment)

        if not working or len(working) < original_chars * _MIN_SURVIVING_RATIO:
            return CompressionResult(prompt, original_chars, original_chars, applied=False)
        return CompressionResult(working, original_chars, len(working), applied=True)

    def compress_text(self, prompt: str) -> str:
        """Atajo: devuelve el texto comprimido (o el original si no aplica)."""
        result = self.compress(prompt)
        if result.applied and result.compressed_chars < result.original_chars:
            logger.info(
                "prompt.compressed",
                extra={
                    "event": "prompt.compressed",
                    "level": self.level,
                    "chars_before": result.original_chars,
                    "chars_after": result.compressed_chars,
                    "ratio": round(result.ratio, 3),
                },
            )
        return result.text

    @staticmethod
    def _normalize(text: str) -> str:
        # Huérfanos de puntuación que dejan las frases eliminadas: "¿ ?" , " ,".
        text = re.sub(r"¿\s*\?", "", text)
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"([,;:])\1+", r"\1", text)
        text = re.sub(r"(^|\n)\s*[,.;:]+\s*", r"\1", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return "\n".join(line.rstrip() for line in text.split("\n")).strip()
