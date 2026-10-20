"""Escuchas HTTP/HTTPS del broker.

Sin certificado hay una sola escucha HTTP, como siempre. Con certificado,
host:port sirve HTTPS y, si server.tls_loopback_http sigue activo, una segunda
escucha HTTP en 127.0.0.1 con el mismo puerto atiende a los clientes de esta
máquina que no sepan HTTPS (el runner de Agora anterior a 0.2.16). Con
tls_loopback_http: false hay una sola escucha, HTTPS, también en loopback.

Las dos escuchas sirven la MISMA instancia de la app, pero el lifespan (que
arranca el dispatcher, recupera tareas y publica el token) corre solo en la
principal: correrlo dos veces duplicaría el dispatcher sobre la misma SQLite.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from app.admin_auth import LOOPBACK_HOSTS
from app.config import WILDCARD_HOSTS, ServerConfig

LOOPBACK_HTTP_HOST = "127.0.0.1"


class ListenerConfigError(ValueError):
    """La combinación de host y TLS no se puede servir."""


@dataclass(frozen=True)
class Listener:
    host: str
    port: int
    tls: bool
    lifespan: bool

    @property
    def url(self) -> str:
        scheme = "https" if self.tls else "http"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}"


def plan_listeners(server: ServerConfig, host: str, port: int) -> list[Listener]:
    """Las escuchas para este host efectivo (puede venir de --host, no del YAML)."""
    if not server.tls_enabled:
        return [Listener(host, port, tls=False, lifespan=True)]
    for label, path in (("tls_certfile", server.tls_certfile), ("tls_keyfile", server.tls_keyfile)):
        if path is None or not Path(path).is_file():
            raise ListenerConfigError(f"server.{label}: no existe el fichero '{path}'")
    listeners = [Listener(host, port, tls=True, lifespan=True)]
    if not server.tls_loopback_http or host in LOOPBACK_HOSTS:
        return listeners
    if host in WILDCARD_HOSTS:
        raise ListenerConfigError(
            f"host '{host}' con HTTPS y tls_loopback_http: usa la IP de red concreta; "
            "con un comodín la escucha HTTP de loopback se solaparía con la HTTPS"
        )
    listeners.append(Listener(LOOPBACK_HTTP_HOST, port, tls=False, lifespan=False))
    return listeners


def uvicorn_config(app: Any, listener: Listener, server: ServerConfig, log_level: str) -> uvicorn.Config:
    tls: dict[str, Any] = {}
    if listener.tls:
        tls = {"ssl_certfile": server.tls_certfile, "ssl_keyfile": server.tls_keyfile}
    return uvicorn.Config(
        app,
        host=listener.host,
        port=listener.port,
        log_level=log_level,
        lifespan="on" if listener.lifespan else "off",
        **tls,
    )


def serve(app: Any, listeners: list[Listener], server: ServerConfig, log_level: str) -> None:
    configs = [uvicorn_config(app, item, server, log_level) for item in listeners]
    # Todos los puertos se abren ANTES de arrancar nada: un puerto ocupado
    # falla aquí (bind_socket sale con error) y no con el dispatcher ya en
    # marcha y solo media escucha abierta.
    sockets = [config.bind_socket() for config in configs]
    servers = [uvicorn.Server(config) for config in configs]
    if len(servers) == 1:
        servers[0].run(sockets=sockets)
        return
    asyncio.run(_serve_together(servers, sockets))


async def _serve_together(servers: list[uvicorn.Server], sockets: list[Any]) -> None:
    """Todas o ninguna: si una escucha para, paran todas.

    Solo la principal captura las señales (serve); las secundarias corren el
    bucle sin capturarlas (_serve) y se cierran al marcarles should_exit, que
    es lo mismo que haría su propio manejador.
    """
    main = servers[0]
    tasks = [asyncio.create_task(main.serve(sockets=[sockets[0]]))]
    tasks += [
        asyncio.create_task(item._serve(sockets=[sock]))
        for item, sock in zip(servers[1:], sockets[1:], strict=True)
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for item in servers:
            item.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
