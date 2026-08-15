"""Cotejo entre lo que un agente afirma haber consultado y lo que consultó.

Un agente puede buscar en la web, no leer nada útil y citar igualmente enlaces
que nunca abrió —o que se inventó entero—. El broker tiene delante toda la
evidencia (los argumentos de cada `fetch_url` y el texto que devolvió cada
skill) y hasta ahora no la cotejaba con la respuesta que entregaba.

La comprobación es DETERMINISTA a propósito: comparar URLs no necesita otro
modelo opinando, no cuesta una invocación y no puede equivocarse a favor de
nadie. No juzga si la respuesta es correcta —eso no lo sabe nadie aquí— sino si
sus enlaces salieron de algún sitio.
"""
from __future__ import annotations

import re
from typing import Any

# Las URLs en Markdown vienen con paréntesis, comas y puntos finales pegados;
# la clase de cierre los deja fuera para no inventar rutas que no existen.
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`\]\)}]+", re.IGNORECASE)
# Cola de puntuación que sobrevive al patrón cuando la URL cierra una frase.
_TRAILING_PUNCTUATION = ".,;:!?"


def normalize_url(url: str) -> str:
    """Forma canónica para comparar dos URLs escritas de manera distinta.

    Deliberadamente conservadora: esquema y host en minúsculas, sin `www.`, sin
    barra final ni fragmento. La query se conserva, porque en muchos sitios es
    la que identifica el documento.
    """
    candidate = str(url or "").strip().rstrip(_TRAILING_PUNCTUATION)
    if not candidate:
        return ""
    candidate = candidate.split("#", 1)[0]
    match = re.match(r"^(https?)://([^/?]+)(.*)$", candidate, re.IGNORECASE)
    if match is None:
        return candidate.lower()
    scheme, host, rest = match.groups()
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    rest = rest.rstrip("/")
    return f"{scheme.lower()}://{host}{rest}"


def extract_urls(text: str) -> list[str]:
    """URLs presentes en un texto, en orden de aparición y sin repetir."""
    seen: dict[str, str] = {}
    for raw in _URL_PATTERN.findall(str(text or "")):
        trimmed = raw.rstrip(_TRAILING_PUNCTUATION)
        key = normalize_url(trimmed)
        if key and key not in seen:
            seen[key] = trimmed
    return list(seen.values())


def collect_consulted_urls(messages: list[dict[str, Any]]) -> set[str]:
    """URLs que el agente llegó a tener delante, en forma normalizada.

    Cuentan dos cosas: las que pidió abrir (argumento `url` de un `fetch_url`,
    aunque la descarga fallara: el modelo vio el error) y las que aparecieron en
    el texto que devolvió cualquier skill, que es de donde salen los resultados
    de una búsqueda.
    """
    consulted: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "tool":
            consulted.update(
                normalize_url(url) for url in extract_urls(str(message.get("content") or ""))
            )
            continue
        if role != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            if function.get("name") != "fetch_url":
                continue
            arguments = function.get("arguments")
            # Ollama devuelve el objeto ya parseado; OpenAI, una cadena JSON.
            text = arguments if isinstance(arguments, str) else str(arguments or "")
            consulted.update(normalize_url(url) for url in extract_urls(text))
    consulted.discard("")
    return consulted


def unsupported_citations(answer: str, messages: list[dict[str, Any]]) -> list[str]:
    """Enlaces de la respuesta que no salen de ninguna consulta del agente.

    Devuelve las URLs tal como aparecen en la respuesta (no normalizadas): son
    las que el operador va a buscar en el texto.
    """
    consulted = collect_consulted_urls(messages)
    return [url for url in extract_urls(answer) if normalize_url(url) not in consulted]
