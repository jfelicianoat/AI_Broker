"""Activa HTTPS en el broker con el certificado de la CA del broker.

El certificado se emite en el PC principal, que es donde está la clave de la
CA ("AI Broker CA"), y llega a este PC en una carpeta con ca.crt, server.crt y
server.key (por defecto C:\\Agora\\broker-https). Vale para 192.168.1.52,
127.0.0.1 y localhost. Este script:

1. Comprueba el par con un handshake TLS real en esta máquina, una vez por cada
   dirección que tiene que servir: el cliente confía solo en esa CA y exige la
   IP. Si pasa, valen la cadena, las IP del certificado, la caducidad y que la
   clave sea la del certificado; es exactamente lo que comprobarán las apps.
2. Los copia a certs/ (ignorado por git) como server.crt, server.key y
   broker-ca.crt.
3. Pone en broker_config.yaml server.host = 0.0.0.0, server.tls_certfile /
   server.tls_keyfile y server.tls_loopback_http = false, con copia de
   seguridad, y valida la config resultante. Sin HTTP en loopback: el runner de
   Agora 0.2.16 ya habla HTTPS con https://127.0.0.1:<port>.

No reinicia el broker: eso lo hace activar_https_broker.bat.
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import ssl
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402

SOURCE = Path(r"C:\Agora\broker-https")
# Las direcciones por las que llegan los clientes: la red y esta máquina.
SERVED_HOSTS = ("192.168.1.52", "127.0.0.1")


def handshake(cert: Path, key: Path, ca: Path, host: str) -> None:
    """Un servidor TLS efímero con el par y un cliente que solo cree en la CA."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)  # falla aquí si la clave no es la del certificado
    client_ctx = ssl.create_default_context(cafile=str(ca))
    listener = socket.create_server((host, 0))
    port = listener.getsockname()[1]
    errors: list[BaseException] = []

    def accept() -> None:
        try:
            conn, _ = listener.accept()
            with server_ctx.wrap_socket(conn, server_side=True):
                pass
        except (OSError, ssl.SSLError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            with client_ctx.wrap_socket(raw, server_hostname=host):
                pass
    finally:
        thread.join(timeout=10)
        listener.close()


def set_server_keys(text: str, values: dict[str, str]) -> str:
    """Cambia o añade claves de primer nivel dentro de `server:` sin tocar el resto."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "server:")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith(" ")),
        len(lines),
    )
    pending = dict(values)
    for i in range(start + 1, end):
        match = re.match(r"^  (\w+):", lines[i])
        if match and match.group(1) in pending:
            lines[i] = f"  {match.group(1)}: {pending.pop(match.group(1))}"
    lines[end:end] = [f"  {name}: {value}" for name, value in pending.items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cert", type=Path, default=SOURCE / "server.crt")
    parser.add_argument("--key", type=Path, default=SOURCE / "server.key")
    parser.add_argument("--ca", type=Path, default=SOURCE / "ca.crt", help="ca.crt de la CA del broker")
    parser.add_argument("--config", type=Path, default=ROOT / "broker_config.yaml")
    args = parser.parse_args()

    for path in (args.cert, args.key, args.ca, args.config):
        if not path.is_file():
            print(f"No existe {path}")
            return 1
    for host in SERVED_HOSTS:
        try:
            handshake(args.cert, args.key, args.ca, host)
        except (OSError, ssl.SSLError) as exc:
            print(f"El certificado no sirve para https://{host}: {exc}")
            return 1
        print(f"Certificado comprobado: la CA {args.ca} lo acepta para {host}")

    certs = ROOT / "certs"
    certs.mkdir(exist_ok=True)
    for source, name in ((args.cert, "server.crt"), (args.key, "server.key"), (args.ca, "broker-ca.crt")):
        if source.resolve() != (certs / name).resolve():
            shutil.copyfile(source, certs / name)

    original = args.config.read_text(encoding="utf-8")
    updated = set_server_keys(
        original,
        {
            "host": "0.0.0.0",
            "tls_certfile": "certs/server.crt",
            "tls_keyfile": "certs/server.key",
            "tls_loopback_http": "false",
        },
    )
    backup = args.config.with_name(f"{args.config.name}.bak-{datetime.now():%Y%m%d-%H%M%S}-https")
    shutil.copyfile(args.config, backup)
    args.config.write_text(updated, encoding="utf-8")
    try:
        load_config(args.config)
    except Exception as exc:  # la config tiene que seguir arrancando o se deshace
        shutil.copyfile(backup, args.config)
        print(f"La configuración resultante no es válida y se ha restaurado: {exc}")
        return 1
    print(f"broker_config.yaml actualizado (copia en {backup.name})")
    print("Tras reiniciar: solo HTTPS, https://192.168.1.52:8765 y https://127.0.0.1:8765")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
