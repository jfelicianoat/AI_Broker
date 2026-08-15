"""Cotejo de enlaces citados contra los que el agente llegó a consultar."""
from app.evidence import (
    collect_consulted_urls,
    extract_urls,
    normalize_url,
    unsupported_citations,
)


def test_normalize_url_ignores_cosmetic_differences() -> None:
    canonical = normalize_url("https://ejemplo.com/a")
    for variant in (
        "https://WWW.Ejemplo.com/a/",
        "https://ejemplo.com/a#seccion",
        "https://ejemplo.com/a.",
    ):
        assert normalize_url(variant) == canonical
    # La query sí distingue: en muchos sitios es la que identifica el documento.
    assert normalize_url("https://ejemplo.com/a?id=2") != canonical
    # Y el host, obviamente.
    assert normalize_url("https://otro.com/a") != canonical


def test_extract_urls_leaves_out_trailing_punctuation_and_markdown() -> None:
    text = (
        "Según [la fuente](https://ejemplo.com/uno), y también en "
        "https://ejemplo.com/dos. Repetida: https://WWW.ejemplo.com/uno/"
    )
    urls = extract_urls(text)
    assert urls == ["https://ejemplo.com/uno", "https://ejemplo.com/dos"]


def test_collect_consulted_counts_fetches_and_search_results() -> None:
    """Cuenta lo que el agente pidió abrir (aunque fallara: vio el error) y lo
    que apareció en el texto que devolvió cualquier skill."""
    messages = [
        {"role": "user", "content": "investiga"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "fetch_url", "arguments": '{"url": "https://abierta.com/x"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ERROR: 404"},
        {"role": "assistant", "content": None, "tool_calls": [
            # Ollama devuelve los argumentos ya como objeto, no como cadena.
            {"id": "c2", "type": "function", "function": {
                "name": "web_search", "arguments": {"query": "algo"}}},
        ]},
        {"role": "tool", "tool_call_id": "c2", "content": "1. Título — https://buscada.com/y"},
    ]
    consulted = collect_consulted_urls(messages)
    assert normalize_url("https://abierta.com/x") in consulted
    assert normalize_url("https://buscada.com/y") in consulted


def test_unsupported_citations_flags_links_the_agent_never_saw() -> None:
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "fetch_url", "arguments": '{"url": "https://real.com/dato"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "el dato es 42"},
    ]
    answer = (
        "El dato es 42, según https://real.com/dato y también "
        "https://inventada.com/informe-2026."
    )
    assert unsupported_citations(answer, messages) == ["https://inventada.com/informe-2026"]

    # Sin enlaces inventados no hay nada que señalar.
    assert unsupported_citations("El dato es 42 (https://real.com/dato).", messages) == []
    # Y una respuesta sin enlaces tampoco.
    assert unsupported_citations("El dato es 42.", messages) == []
