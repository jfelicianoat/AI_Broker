from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the AI Broker dashboard with the bootstrap provider.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", default="preview.db")
    args = parser.parse_args()
    database = Path(args.database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(database.parent)
    from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
    from app.main import create_app

    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(database)),
        processing=ProcessingConfig(provider_mode="bootstrap", auto_dispatch=False),
    )
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
