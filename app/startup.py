"""Comprobaciones de arranque y procesos auxiliares del broker.

Extraído de app.main para que create_app sea un ensamblador: aquí viven el
guard fail-closed de credenciales, las alertas de VRAM y coste cero, y el
auto-start de servidores locales (LM Studio).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from app.admin_auth import LOOPBACK_HOSTS, AdminTokenLookupError, resolve_admin_token
from app.config import BrokerConfig
from app.schemas import is_local_deployment

_VRAM_DETECTION_CACHE: dict[str, float | None] = {}


def detect_total_vram_gb() -> float | None:
    """VRAM total de las GPU NVIDIA visibles, o None si no se puede detectar (cacheado)."""
    import subprocess

    if "value" in _VRAM_DETECTION_CACHE:
        return _VRAM_DETECTION_CACHE["value"]
    _VRAM_DETECTION_CACHE["value"] = None
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            totals = [float(line.strip()) for line in proc.stdout.splitlines() if line.strip()]
            if totals:
                _VRAM_DETECTION_CACHE["value"] = sum(totals) / 1024
    except Exception:
        pass
    return _VRAM_DETECTION_CACHE["value"]


def vram_budget_mismatch(budget_gb: float, detected_gb: float | None) -> str | None:
    """Mensaje de alerta si el presupuesto configurado no cabe en la VRAM real."""
    if detected_gb is None or detected_gb <= 0:
        return None
    if budget_gb > detected_gb * 1.5:
        return (
            f"resources.local_vram_budget_gb={budget_gb:.0f} GB pero la VRAM detectada es "
            f"{detected_gb:.1f} GB: el planificador sobresuscribirá la GPU (olas paralelas "
            "imposibles, OOM y timeouts en cascada). Ajusta el presupuesto a la VRAM real."
        )
    return None


def ensure_admin_credential_for_exposed_host(config: BrokerConfig) -> None:
    """Arranque fail-closed: fuera de loopback se exige token admin verificable.

    Escuchar en LAN expone mutaciones y lecturas con prompts/resultados, así
    que sin credencial el broker no arranca (antes solo emitía un warning).
    Un fallo del backend de credenciales también bloquea: no poder verificar
    el token no es lo mismo que no tenerlo. El opt-out explícito es
    server.allow_unauthenticated_lan=true.
    """
    if config.server.host in LOOPBACK_HOSTS or config.server.allow_unauthenticated_lan:
        return
    try:
        token = resolve_admin_token(config)
    except AdminTokenLookupError as error:
        raise RuntimeError(
            f"El broker escucha en {config.server.host} pero el backend de credenciales falló "
            f"({error}). Define el token por variable de entorno, repara el keyring o usa "
            "server.allow_unauthenticated_lan=true bajo tu responsabilidad."
        ) from error
    if not token:
        raise RuntimeError(
            f"El broker escucha en {config.server.host} sin token admin configurado. "
            "Guarda un token (env o keyring) o activa server.allow_unauthenticated_lan=true "
            "bajo tu responsabilidad."
        )


def zero_cost_cloud_providers(config: BrokerConfig) -> list[str]:
    """Providers externos (cloud/api) habilitados sin precios: su gasto real será invisible (coste 0)."""
    zero: list[str] = []
    deepseek = config.providers.deepseek
    if deepseek.enabled and not deepseek.input_cost_per_million and not deepseek.output_cost_per_million:
        zero.append("deepseek")
    for item in config.providers.custom:
        if not item.enabled or is_local_deployment(item.deployment):
            continue
        model_costs = any(
            model.input_cost_per_million or model.output_cost_per_million for model in item.models
        )
        if not item.input_cost_per_million and not item.output_cost_per_million and not model_costs:
            zero.append(item.id)
    return zero


async def auto_start_local_provider_servers(config: BrokerConfig, logger: logging.Logger) -> None:
    for provider in config.providers.custom:
        if not provider.enabled or not provider.auto_start:
            continue
        if provider.deployment != "local":
            logger.warning(
                "provider.auto_start_ignored",
                extra={"provider": provider.id, "reason": "deployment_not_local"},
            )
            continue
        if provider.id.lower() not in {"lmstudio", "lm_studio"}:
            logger.warning(
                "provider.auto_start_ignored",
                extra={"provider": provider.id, "reason": "unsupported_provider"},
            )
            continue
        await ensure_lmstudio_server(provider.base_url, logger)


async def ensure_lmstudio_server(base_url: str, logger: logging.Logger) -> None:
    parsed = urlparse(base_url)
    port = parsed.port or 1234
    status = await run_process(["lms", "server", "status"], timeout_seconds=10)
    status_text = f"{status['stdout']}\n{status['stderr']}".strip()
    if status["returncode"] == 0 and "not running" not in status_text.lower():
        logger.info("provider.auto_start_skipped", extra={"provider": "lmstudio", "detail": status_text})
        return

    started = await run_process(["lms", "server", "start", "--port", str(port)], timeout_seconds=30)
    output = f"{started['stdout']}\n{started['stderr']}".strip()
    if started["returncode"] != 0:
        logger.warning(
            "provider.auto_start_failed",
            extra={"provider": "lmstudio", "returncode": started["returncode"], "detail": output},
        )
        return
    logger.info("provider.auto_started", extra={"provider": "lmstudio", "port": port, "detail": output})


async def run_process(args: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        return {
            "returncode": int(process.returncode or 0),
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"No se encontro el ejecutable: {args[0]}"}
    except TimeoutError:
        return {"returncode": 124, "stdout": "", "stderr": "Timeout al ejecutar el comando"}
