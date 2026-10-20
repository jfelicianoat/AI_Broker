"""HTTPS del broker: qué escuchas se abren y qué cookies salen por cada una.

Con certificado, la red habla HTTPS y, con tls_loopback_http, loopback sigue en
HTTP para clientes locales sin HTTPS (el runner de Agora anterior a 0.2.16).
Sin él, todo es HTTPS. Los dos errores que estos
tests vigilan son los que no avisan: un comodín que solapa ambas escuchas en
loopback, y una cookie Secure que el navegador tira en la escucha HTTP.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.admin_auth as admin_auth
from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig, ServerConfig
from app.listeners import Listener, ListenerConfigError, plan_listeners, uvicorn_config
from app.main import create_app


@pytest.fixture
def cert_files(tmp_path: Path) -> tuple[str, str]:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    return str(cert), str(key)


def tls_server(cert_files: tuple[str, str], host: str = "192.168.1.52", **extra) -> ServerConfig:
    cert, key = cert_files
    return ServerConfig(host=host, tls_certfile=cert, tls_keyfile=key, **extra)


def test_without_certificate_there_is_one_plain_listener() -> None:
    listeners = plan_listeners(ServerConfig(host="0.0.0.0"), "0.0.0.0", 8765)

    assert listeners == [Listener("0.0.0.0", 8765, tls=False, lifespan=True)]


def test_tls_on_lan_ip_keeps_plain_http_on_loopback(cert_files) -> None:
    listeners = plan_listeners(tls_server(cert_files), "192.168.1.52", 8765)

    assert [item.url for item in listeners] == [
        "https://192.168.1.52:8765",
        "http://127.0.0.1:8765",
    ]
    # El lifespan arranca el dispatcher: dos lifespans serían dos dispatchers
    # sobre la misma SQLite.
    assert [item.lifespan for item in listeners] == [True, False]


def test_loopback_http_can_be_switched_off(cert_files) -> None:
    server = tls_server(cert_files, tls_loopback_http=False)

    assert [item.url for item in plan_listeners(server, "192.168.1.52", 8765)] == [
        "https://192.168.1.52:8765",
    ]


def test_https_only_on_wildcard_is_one_tls_listener(cert_files) -> None:
    # El despliegue del PC IA: el runner 0.2.16 ya habla HTTPS en loopback, así
    # que sin la escucha HTTP no hay solape y el comodín vale.
    server = tls_server(cert_files, host="0.0.0.0", tls_loopback_http=False)

    assert plan_listeners(server, "0.0.0.0", 8765) == [
        Listener("0.0.0.0", 8765, tls=True, lifespan=True),
    ]


def test_tls_on_loopback_host_needs_no_second_listener(cert_files) -> None:
    server = tls_server(cert_files, host="127.0.0.1")

    assert [item.url for item in plan_listeners(server, "127.0.0.1", 8765)] == [
        "https://127.0.0.1:8765",
    ]


def test_wildcard_host_with_tls_and_loopback_http_is_rejected_by_config(cert_files) -> None:
    with pytest.raises(ValueError, match="IP de red"):
        tls_server(cert_files, host="0.0.0.0")


def test_wildcard_from_command_line_is_rejected_too(cert_files) -> None:
    # --host salta la validación del YAML: plan_listeners la repite con el
    # host efectivo.
    with pytest.raises(ListenerConfigError, match="comodín"):
        plan_listeners(tls_server(cert_files), "0.0.0.0", 8765)


def test_certificate_and_key_go_together(cert_files) -> None:
    with pytest.raises(ValueError, match="van juntos"):
        ServerConfig(host="192.168.1.52", tls_certfile=cert_files[0])


def test_missing_certificate_file_fails_before_listening(tmp_path: Path) -> None:
    server = ServerConfig(
        host="192.168.1.52",
        tls_certfile=str(tmp_path / "no-existe.crt"),
        tls_keyfile=str(tmp_path / "no-existe.key"),
    )

    with pytest.raises(ListenerConfigError, match="tls_certfile"):
        plan_listeners(server, "192.168.1.52", 8765)


def test_only_the_tls_listener_gets_the_certificate(cert_files) -> None:
    server = tls_server(cert_files)
    https, http = plan_listeners(server, "192.168.1.52", 8765)

    secure = uvicorn_config(object(), https, server, "warning")
    plain = uvicorn_config(object(), http, server, "warning")

    assert (secure.ssl_certfile, secure.ssl_keyfile) == cert_files
    assert secure.lifespan == "on"
    assert plain.ssl_certfile is None
    assert plain.lifespan == "off"


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *args, **kwargs: None)
    admin_auth._keyring_cache.clear()
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    yield create_app(config)
    admin_auth._keyring_cache.clear()


@pytest.mark.parametrize(
    ("base_url", "secure"),
    [("https://192.168.1.52:8765", True), ("http://127.0.0.1:8765", False)],
)
def test_dashboard_cookie_is_secure_only_over_https(client, base_url: str, secure: bool) -> None:
    with TestClient(client, base_url=base_url) as http:
        response = http.get("/dashboard/login")

    cookies = response.headers.get_list("set-cookie")
    assert cookies
    assert all(("; secure" in item.lower()) is secure for item in cookies)
