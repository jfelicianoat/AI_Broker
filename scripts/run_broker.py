from __future__ import annotations

import argparse

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
    app = create_app(config, config_path=args.config)
    # Al pasar la instancia (no un import string) Uvicorn corre en un único
    # proceso; el broker asume un solo worker por su estado en SQLite.
    uvicorn.run(
        app,
        host=args.host or config.server.host,
        port=args.port or config.server.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
