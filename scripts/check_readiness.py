from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

# Con HTTPS activado el broker solo contesta por https, tambien en loopback, con
# el certificado de su propia CA (certs/broker-ca.crt, lo deja activar_https.py).
BROKER_CA = Path(__file__).resolve().parent.parent / "certs" / "broker-ca.crt"
DEFAULT_URL = ("https" if BROKER_CA.is_file() else "http") + "://127.0.0.1:8765/health/ready"


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait until AI Broker readiness is healthy/degraded")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--cafile", default=str(BROKER_CA) if BROKER_CA.is_file() else None)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    context = ssl.create_default_context(cafile=args.cafile) if args.cafile else None
    deadline = time.monotonic() + args.timeout
    last_error = "not checked"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(args.url, timeout=min(args.interval, 5.0), context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("dependencies", {}).get("sqlite", {}).get("status") == "healthy":
                    print(json.dumps({"ready": True, "status": payload.get("status")}, ensure_ascii=False))
                    return 0
                last_error = json.dumps(payload, ensure_ascii=False)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = str(error)
        time.sleep(args.interval)
    print(json.dumps({"ready": False, "last_error": last_error}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
