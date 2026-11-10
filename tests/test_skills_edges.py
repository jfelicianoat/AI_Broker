"""Skills del agente: lectura de URLs, guardia SSRF y bordes de la calculadora.

`fetch_url` resuelve el host antes de pedir nada; aquí la resolución se
sustituye para no depender de la red ni del DNS de la máquina que ejecuta la
suite (el CI no tiene por qué resolver nombres públicos).
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest

from app import skills
from app.sandbox import SandboxError
from app.skills import run_skill


def _resolve_to(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    def fake_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(skills.socket, "getaddrinfo", fake_getaddrinfo)


def _skill(name: str, arguments: dict[str, Any], **kwargs: Any) -> str:
    return asyncio.run(run_skill(name, arguments, **kwargs))


def test_fetch_url_reads_html_as_text_and_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(skills, "_MAX_RESULT_CHARS", 40)
    page = "<html><style>x{}</style><script>alert(1)</script><p>Hola &amp; adiós</p>" + "<p>relleno</p>" * 20
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
    )
    text = _skill("fetch_url", {"url": "https://example.com/"}, transport=transport)
    assert text.startswith("Hola & adiós")
    assert "alert" not in text
    assert text.endswith("[...contenido truncado...]")


def test_fetch_url_plain_text_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    plain = httpx.MockTransport(lambda request: httpx.Response(200, text="  dato  ", headers={"content-type": "text/plain"}))
    assert _skill("fetch_url", {"url": "https://example.com/a.txt"}, transport=plain) == "dato"
    empty = httpx.MockTransport(lambda request: httpx.Response(200, text="", headers={"content-type": "text/plain"}))
    assert _skill("fetch_url", {"url": "https://example.com/vacio"}, transport=empty) == "(respuesta vacía)"


def test_fetch_url_stops_reading_at_the_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(skills, "_MAX_FETCH_BYTES", 10)
    async def chunks():
        for _ in range(100):
            yield b"x" * 10

    # El tope corta entre trozos: el cuerpo tiene que llegar troceado.
    big = httpx.MockTransport(lambda request: httpx.Response(200, content=chunks(), headers={"content-type": "text/plain"}))
    assert _skill("fetch_url", {"url": "https://example.com/big"}, transport=big) == "x" * 10


@pytest.mark.parametrize(
    ("url", "address", "fragment"),
    [
        ("ftp://example.com/x", "93.184.216.34", "Esquema no permitido"),
        ("http://192.168.1.10/panel", "192.168.1.10", "Host no público"),
        ("http://localhost:8765/", "127.0.0.1", "Host no público"),
    ],
)
def test_fetch_url_rejects_unsafe_targets(
    monkeypatch: pytest.MonkeyPatch, url: str, address: str, fragment: str
) -> None:
    _resolve_to(monkeypatch, address)
    assert fragment in _skill("fetch_url", {"url": url})


def test_fetch_url_argument_and_resolution_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "requiere el argumento url" in _skill("fetch_url", {})

    def no_dns(*args: Any, **kwargs: Any) -> list[tuple]:
        raise OSError("sin DNS")

    monkeypatch.setattr(skills.socket, "getaddrinfo", no_dns)
    assert "No se pudo resolver" in _skill("fetch_url", {"url": "https://no-existe.example/"})


def test_fetch_url_http_error_is_reported_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "93.184.216.34")
    failing = httpx.MockTransport(lambda request: httpx.Response(404))
    assert "fallo de red" in _skill("fetch_url", {"url": "https://example.com/404"}, transport=failing)


def test_web_search_requires_query_and_reports_no_results() -> None:
    assert "requiere el argumento query" in _skill("web_search", {"query": "  "})
    empty = httpx.MockTransport(lambda request: httpx.Response(200, text="<html></html>"))
    assert _skill("web_search", {"query": "nada"}, transport=empty) == "Sin resultados para: nada"


@pytest.mark.parametrize(
    ("expression", "fragment"),
    [
        ("", "requiere el argumento expression"),
        ("2 +", "expresión inválida"),
        ("1 / 0", "división por cero"),
        ("2 ** 100000", "exponente demasiado grande"),
        ("'a' * 3", "solo se permiten números"),
        ("True + 1", "solo se permiten números"),
        ("abs(-1)", "expresión no permitida"),
    ],
)
def test_calculator_rejections(expression: str, fragment: str) -> None:
    assert fragment in _skill("calculator", {"expression": expression})


def test_calculator_values() -> None:
    assert _skill("calculator", {"expression": "-(2 ** 3) + 0.5"}) == "-7.5"
    assert _skill("calculator", {"expression": "6 / 3"}) == "2"


def test_run_code_reports_sandbox_errors_and_missing_code() -> None:
    class BrokenSandbox:
        async def run_python(self, code: str, files: Any = None) -> str:
            raise SandboxError("Docker no responde")

    assert "requiere el argumento code" in _skill("run_code", {"code": " "}, sandbox=BrokenSandbox())
    assert "Docker no responde" in _skill("run_code", {"code": "print(1)"}, sandbox=BrokenSandbox())


def test_unexpected_skill_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(arguments: dict[str, Any]) -> str:
        raise KeyError("inesperado")

    monkeypatch.setattr(skills, "_run_calculator", explode)
    assert _skill("calculator", {"expression": "1"}).startswith("ERROR de calculator: KeyError")
