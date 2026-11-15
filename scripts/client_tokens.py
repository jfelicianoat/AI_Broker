"""Gestiona los tokens de cliente del broker desde la consola de este PC.

    .venv\\Scripts\\python scripts\\client_tokens.py crear knowledge-orchestrator
    .venv\\Scripts\\python scripts\\client_tokens.py listar
    .venv\\Scripts\\python scripts\\client_tokens.py revocar cli_0123456789abcdef

Usa el token de administración de AI_BROKER_ADMIN_TOKEN o del llavero
(`ai-broker` / `session_admin_token`) y la CA del broker (certs/broker-ca.crt).
El token de un cliente nuevo se imprime una sola vez: el broker solo guarda su
hash. Ver docs/Client_API.md §3.2.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CA = ROOT / "certs" / "broker-ca.crt"


def _admin_token() -> str:
    token = os.environ.get("AI_BROKER_ADMIN_TOKEN")
    if token:
        return token
    import keyring

    token = keyring.get_password("ai-broker", "session_admin_token")
    if not token:
        raise SystemExit("Sin token de administración: define AI_BROKER_ADMIN_TOKEN o arranca el broker.")
    return token


def _client(url: str, ca: Path, http: Any = None) -> Any:
    if http is not None:
        return http
    import httpx

    return httpx.Client(base_url=url, verify=str(ca) if ca.exists() else True, timeout=30,
                        headers={"X-Admin-Token": _admin_token()})


def _fail(response: Any) -> int:
    print(f"ERROR {response.status_code}: {response.text}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None, http: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Tokens de cliente del AI Broker")
    parser.add_argument("--broker-url", default="https://127.0.0.1:8765")
    parser.add_argument("--ca", type=Path, default=DEFAULT_CA)
    sub = parser.add_subparsers(dest="accion", required=True)
    crear = sub.add_parser("crear", help="emite un token para una app")
    crear.add_argument("nombre")
    sub.add_parser("listar", help="lista los tokens (nunca el token en sí)")
    revocar = sub.add_parser("revocar", help="revoca un token por su id")
    revocar.add_argument("id")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    client = _client(args.broker_url, args.ca, http)
    if args.accion == "crear":
        response = client.post("/api/v1/admin/client-tokens", json={"name": args.nombre})
        if response.status_code != 201:
            return _fail(response)
        data = response.json()
        print(f"Token para {data['name']} ({data['id']}). Cópialo ahora: no se vuelve a mostrar.\n")
        print(f"    {data['token']}\n")
        return 0
    if args.accion == "listar":
        response = client.get("/api/v1/admin/client-tokens")
        if response.status_code != 200:
            return _fail(response)
        items = response.json()["items"]
        if not items:
            print("No hay tokens de cliente.")
        for item in items:
            estado = "activo" if item["active"] else f"revocado {item['revoked_at']}"
            print(f"{item['id']}  {item['name']:<28} {item['token_prefix']}…  {estado}  "
                  f"último uso: {item['last_used_at'] or 'nunca'}")
        return 0
    response = client.delete(f"/api/v1/admin/client-tokens/{args.id}")
    if response.status_code != 200:
        return _fail(response)
    print(f"Revocado {response.json()['name']} ({args.id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
