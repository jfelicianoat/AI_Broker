from __future__ import annotations

import argparse
import os
import secrets

import uvicorn

from app.config import load_config
from app.main import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI Broker with broker_config.yaml")
    parser.add_argument("--config", default="broker_config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()

    # La config se carga una sola vez y la app se construye aquí con ese mismo
    # path: host/puerto, comportamiento del broker y el YAML que edita el
    # dashboard salen todos de --config. Con el factory string de Uvicorn la
    # app recargaba la config por defecto e ignoraba --config para todo lo que
    # no fuera host/puerto.
    config = load_config(args.config)
    host = args.host or config.server.host
    port = args.port or config.server.port
    # Token admin efímero: se genera uno nuevo en cada arranque y se publica en
    # la variable de entorno que resolve_admin_token consulta con máxima
    # prioridad (por encima del keyring). Si la variable ya viene definida
    # desde fuera se respeta ese valor: es la vía para fijar un token estable.
    env_name = config.server.admin_token_env
    if env_name:
        token = os.environ.get(env_name)
        origen = "definido externamente via %s" % env_name
        if not token:
            token = secrets.token_urlsafe(24)
            os.environ[env_name] = token
            origen = "generado para esta sesion"
        print("=" * 58)
        print("  Panel:")
        print()
        print("    http://%s:%s/dashboard" % (host, port))
        print()
        print("  Token de administracion (%s):" % origen)
        print()
        print("    %s" % token)
        print()
        print("  Usalo en el login del panel o en la cabecera")
        print("  X-Admin-Token de la API. Cambia en cada arranque.")
        print("=" * 58, flush=True)
    app = create_app(config, config_path=args.config)
    # Al pasar la instancia (no un import string) Uvicorn corre en un único
    # proceso; el broker asume un solo worker por su estado en SQLite.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
