"""Contrasta lo que el broker cree residente contra lo que el runtime tiene cargado.

Misma informacion que el panel "Residencia real" del dashboard, en consola y con
codigo de salida, para poder engancharlo a una tarea programada. La logica vive
en `app.residency` y NO se duplica aqui: si divergieran, la consola y el panel
dirian cosas distintas del mismo instante.

Uso (con el Python del venv, que es el que tiene keyring):
    .venv/Scripts/python.exe scripts/check_model_residency.py
    .venv/Scripts/python.exe scripts/check_model_residency.py --json
    .venv/Scripts/python.exe scripts/check_model_residency.py --since-hours 72

Salida: 0 sin hallazgos, 1 con hallazgos, 2 si no se pudo comprobar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.residency import GIB, build_report  # noqa: E402

# El script habla con el broker por HTTP en vez de cargar su configuracion: asi
# refleja el estado del proceso que esta corriendo de verdad, con sus reservas
# vivas, y no el de una instancia recien construida sin leases.
DEFAULT_BROKER = "http://127.0.0.1:8765"
DEFAULT_LMSTUDIO = "http://127.0.0.1:1234"


def broker_token() -> str:
    """Token admin: entorno primero, si no el llavero donde el broker publica
    el token efimero de esta sesion (server.publish_session_token: keyring)."""
    token = os.getenv("AI_BROKER_ADMIN_TOKEN") or ""
    if token:
        return token
    try:
        import keyring

        return keyring.get_password("ai-broker", "session_admin_token") or ""
    except Exception:
        return ""


def read_resources(base_url: str) -> tuple[dict[str, Any] | None, str | None]:
    token = broker_token()
    headers = {"X-Admin-Token": token} if token else {}
    request = urllib.request.Request(f"{base_url}/api/v1/dashboard/resources", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            hint = "sin token: ejecutalo con el Python del venv" if not token else "token rechazado"
            return None, f"el broker respondio {error.code} ({hint})"
        return None, f"el broker respondio {error.code}"
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        return None, f"broker no alcanzable: {error}"


def to_ascii(text: str) -> str:
    """Quita tildes y enes en vez de perder el caracter entero."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return stripped.encode("ascii", "replace").decode("ascii")


def gib(value: int | None) -> str:
    return "N/D" if value is None else f"{value / GIB:.1f} GB"


def render(resources: dict[str, Any] | None, error: str | None, report: Any) -> str:
    lines = ["=== Residencia real: broker vs runtime ===", ""]
    if error:
        lines.append(f"Broker  : NO DISPONIBLE ({error})")
    elif resources is not None:
        usable = int(resources["vram_budget_bytes"]) - int(resources["vram_safety_margin_bytes"])
        lines.append(
            f"Broker  : pool {resources['memory_pool']}, presupuesto util {gib(usable)}, "
            f"en uso {gib(resources['used_vram_bytes'])}, "
            f"reservado {gib(resources['reserved_vram_bytes'])}"
        )
        for model in resources.get("loaded_models") or []:
            lines.append(
                f"          - {model['model']}: {gib(model['size_vram_bytes'])} "
                f"(ctx {model.get('context_length') or 'N/D'}, {model['lease_count']} leases)"
            )
        if not resources.get("loaded_models"):
            lines.append("          (ningun modelo cargado)")

    lines.append(f"Runtime : pool anunciado {gib(report.runtime_pool_bytes)}")
    lines.append(
        f"Sistema : RAM libre {gib(report.system_free_bytes)}  <- techo que aplica el runtime"
    )
    lines.append(
        f"Externo : {len(report.lmstudio_loaded)} modelo(s) fuera del presupuesto"
        + (": " + ", ".join(report.lmstudio_loaded) if report.lmstudio_loaded else "")
    )
    lines.append(
        f"Desalojos en {report.eviction_window_hours} h: "
        + (str(report.eviction_count) if report.log_available else "log no disponible")
    )
    lines.append("")

    if not report.findings:
        lines.append("Sin hallazgos: lo residente coincide con lo que el broker cree tener.")
    else:
        lines.append(f"{len(report.findings)} hallazgo(s):")
        for index, finding in enumerate(report.findings, start=1):
            lines.append("")
            lines.append(f"  {index}. [{finding.level.upper()}] {finding.title}")
            lines.append(f"     {finding.detail}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Contrasta el presupuesto local del broker con el estado real del runtime",
    )
    parser.add_argument("--broker", default=os.getenv("AI_BROKER_URL", DEFAULT_BROKER))
    parser.add_argument("--lmstudio", default=DEFAULT_LMSTUDIO)
    parser.add_argument("--since-hours", type=int, default=48)
    parser.add_argument("--json", action="store_true", help="salida legible por maquina")
    args = parser.parse_args()

    resources, error = read_resources(args.broker.rstrip("/"))
    report = build_report(
        budget_bytes=int((resources or {}).get("vram_budget_bytes") or 0),
        reserved_bytes=int((resources or {}).get("reserved_vram_bytes") or 0),
        loaded_models=list((resources or {}).get("loaded_models") or []),
        lmstudio_urls=[args.lmstudio.rstrip("/")],
        since_hours=args.since_hours,
    )

    if args.json:
        print(json.dumps({
            "resources": resources,
            "resources_error": error,
            "residency": report.model_dump(mode="json"),
        }, ensure_ascii=False, indent=2))
    else:
        # Salida ASCII a proposito: el stdout de un hijo va en cp1252 en este
        # equipo y un simbolo fuera de esa pagina mata el proceso en silencio.
        # Se translitera en vez de sustituir por "?" para no destrozar el texto:
        # "ultimo" se lee, "?ltimo" distrae de lo que el hallazgo esta diciendo.
        print(to_ascii(render(resources, error, report)))

    if resources is None:
        return 2
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
