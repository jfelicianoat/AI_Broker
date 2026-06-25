from __future__ import annotations

import argparse

import uvicorn

from app.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI Broker with broker_config.yaml")
    parser.add_argument("--config", default="broker_config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()

    config = load_config(args.config)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=args.host or config.server.host,
        port=args.port or config.server.port,
        workers=1,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
