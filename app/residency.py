"""Contraste entre lo que el broker cree residente y lo que de verdad hay cargado.

Existe por un desajuste medido en esta máquina (APU con memoria unificada):
Ollama admite un modelo comparando su tamaño contra `min(gpu_free, system_free)`
y marca la decisión con `system_limited=true`. En memoria unificada `system_free`
es el pool equivocado —cargar 46 GiB de modelos solo consumió 2,6 GiB de RAM del
sistema—, así que un modelo más grande que la RAM libre del sistema desaloja todo
lo residente aunque queden decenas de GiB libres en la GPU. Y desalojar tampoco
libera RAM del sistema, con lo que el desalojo ni siquiera arregla lo que creía
arreglar.

El presupuesto del broker no ve nada de eso: sus leases siguen vivos sobre
modelos que el runtime ya tiró, y la siguiente invocación paga una recarga que no
aparece explicada en ningún sitio. Este módulo hace visible ese hueco, y es la
única fuente: lo consumen el panel y `scripts/check_model_residency.py`.

Todo lo que se lee aquí es diagnóstico local y de mejor esfuerzo (log del runtime,
memoria del sistema, LM Studio). Ninguna lectura que falle puede tumbar el panel:
lo que no se sabe se declara desconocido y se sigue.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.schemas import ResidencyFinding, ResidencyReport

GIB = 1024**3

# Hueco de reserva por debajo del cual no se dice nada: una reserva va por
# delante de /api/ps durante toda la carga, y el runtime redondea a un decimal
# de GiB, así que exigir precisión aquí solo generaría ruido.
GAP_TOLERANCE_BYTES = GIB

# Ventana para atribuir ese hueco a un desalojo en vez de a una carga en frío.
# Generosa a propósito: cargar 16 GiB ya tarda unos 40 segundos, y durante ese
# rato el estado es indistinguible de un desalojo si solo se mira la reserva.
EVICTION_CORRELATION_MINUTES = 10

# El log del runtime son unos pocos MB y el panel se refresca cada 10 s.
# Releerlo en cada refresco sería gastar E/S en una respuesta que no cambia.
LOG_CACHE_SECONDS = 60.0

_log_cache: dict[str, Any] = {}


# --- lecturas de mejor esfuerzo ----------------------------------------------


def system_free_bytes() -> int | None:
    """RAM física disponible según el sistema operativo.

    Es el número que el runtime usa como `system_free`, y por tanto el techo
    real de admisión aquí. Se lee por `ctypes` en vez de lanzar un proceso
    porque esto entra en la ruta de refresco del panel.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys)
    except Exception:
        return None


def local_openai_provider_roots(config: Any) -> list[str]:
    """Raíces HTTP de los proveedores openai_compatible locales del catálogo.

    Se quita el `/v1` porque el estado de carga no vive bajo el prefijo
    compatible con OpenAI, sino en la API propia del servidor (`/api/v0`).
    """
    roots: list[str] = []
    for provider in getattr(config.providers, "custom", []) or []:
        if not provider.enabled or provider.deployment != "local":
            continue
        root = provider.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        roots.append(root)
    return roots


def lmstudio_loaded_models(base_urls: list[str], timeout: float = 3.0) -> list[str]:
    """Modelos que un servidor tipo LM Studio tiene cargados ahora mismo.

    Es el consumidor que no contabiliza nadie: no salen en `/api/ps` ni en el
    presupuesto del broker, pero ocupan el mismo pool unificado y hunden
    `system_free`, que es justo lo que dispara los desalojos.

    Un servidor que no exponga `/api/v0/models` simplemente no aporta nada: es
    un diagnóstico opcional, no una dependencia del panel.
    """
    loaded: set[str] = set()
    for base_url in base_urls:
        try:
            request = urllib.request.Request(base_url.rstrip("/") + "/api/v0/models")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
            continue
        loaded.update(
            str(item.get("id") or "")
            for item in (payload.get("data") or [])
            if str(item.get("state") or "not-loaded") != "not-loaded"
        )
    return sorted(name for name in loaded if name)


# --- log del runtime ---------------------------------------------------------


_LOG_FIELD = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
_LOG_TIME = re.compile(r"^time=(\S+)")
_SIZE_GIB = re.compile(r"^([\d.]+)\s*GiB$")


def _log_directory() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Ollama"


def _fields(line: str) -> dict[str, str]:
    return {key: (quoted if quoted else bare) for key, quoted, bare in _LOG_FIELD.findall(line)}


def _bytes_from_gib(value: str | None) -> int | None:
    if not value:
        return None
    match = _SIZE_GIB.match(value.strip())
    return int(float(match.group(1)) * GIB) if match else None


def _timestamp(line: str) -> datetime | None:
    match = _LOG_TIME.match(line)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1))
    except ValueError:
        return None


def scan_runtime_log(since_hours: int = 48) -> dict[str, Any]:
    """Desalojos por `system_limited` y pool que anuncia el runtime, cacheado.

    El pool se toma del inventario de arranque (`inference compute`), que es la
    única fuente que dice cuánta memoria cree tener el runtime — dato que no
    expone por API y sin el cual no se puede afirmar que un desalojo sobraba.
    """
    key = f"{since_hours}"
    cached = _log_cache.get(key)
    if cached and time.monotonic() < cached["expires"]:
        return cached["value"]

    directory = _log_directory()
    files = sorted(directory.glob("server*.log")) if directory.is_dir() else []
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    evictions: list[dict[str, Any]] = []
    pool_bytes: int | None = None

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if pool_bytes is None and "inference compute" in line:
                pool_bytes = _bytes_from_gib(_fields(line).get("total"))
                continue
            if "evicting" not in line or "system_limited=true" not in line:
                continue
            stamp = _timestamp(line)
            if stamp is None or stamp < since:
                continue
            data = _fields(line)
            evictions.append({
                "at": stamp,
                "predicted_bytes": _bytes_from_gib(data.get("predicted")),
                "gpu_free_bytes": _bytes_from_gib(data.get("gpu_free")),
                "system_free_bytes": _bytes_from_gib(data.get("system_free")),
            })

    evictions.sort(key=lambda item: item["at"])
    value = {
        "evictions": evictions,
        "pool_bytes": pool_bytes,
        "available": bool(files),
    }
    _log_cache[key] = {"value": value, "expires": time.monotonic() + LOG_CACHE_SECONDS}
    return value


def _recent_eviction(evictions: list[dict[str, Any]]) -> dict[str, Any] | None:
    limit = datetime.now(timezone.utc) - timedelta(minutes=EVICTION_CORRELATION_MINUTES)
    for item in reversed(evictions):
        if item["at"] >= limit:
            return item
    return None


# --- informe -----------------------------------------------------------------


def _gib(value: int | None) -> str:
    """Misma unidad y misma etiqueta que el filtro `gb` del panel.

    El proyecto llama GB a los bytes divididos por 1024**3 en todas partes.
    Escribir GiB solo aqui haria parecer que el panel de recursos de arriba y
    este hablan de magnitudes distintas para los mismos bytes.
    """
    return "desconocido" if value is None else f"{value / GIB:.1f} GB"


def _field(model: Any, name: str) -> int:
    """Un campo del modelo cargado, venga como dict o como objeto tipado.

    `resource_snapshot` devuelve dicts y el panel los tipa después; aceptar los
    dos evita obligar a un lado a convertir solo para poder diagnosticar.
    """
    value = model.get(name) if isinstance(model, dict) else getattr(model, name, 0)
    return int(value or 0)


def build_report(
    *,
    budget_bytes: int,
    reserved_bytes: int,
    loaded_models: list[Any],
    lmstudio_urls: list[str],
    since_hours: int = 48,
) -> ResidencyReport:
    """Informe de residencia a partir del snapshot que ya tiene el panel.

    `loaded_models` son los `DashboardLoadedModel` del snapshot de recursos: se
    reciben ya resueltos en vez de volver a pedir `/api/ps` para que el panel
    describa el MISMO instante que pinta arriba y no dos lecturas distintas.
    """
    log = scan_runtime_log(since_hours)
    evictions = log["evictions"]
    pool_bytes = log["pool_bytes"]
    free_ram = system_free_bytes()
    lmstudio = lmstudio_loaded_models(lmstudio_urls)
    findings: list[ResidencyFinding] = []

    leased = sum(
        _field(model, "size_vram_bytes")
        for model in loaded_models
        if _field(model, "lease_count") > 0
    )
    gap = reserved_bytes - leased
    if gap > GAP_TOLERANCE_BYTES:
        culprit = _recent_eviction(evictions)
        if culprit is not None:
            findings.append(ResidencyFinding(
                level="problema",
                title="Desalojo silencioso: el broker cree residente un modelo que el runtime tiró",
                detail=(
                    f"Reservados {_gib(reserved_bytes)} frente a {_gib(leased)} residentes con "
                    f"lease. El runtime desalojó teniendo {_gib(culprit['gpu_free_bytes'])} "
                    "libres en la GPU. La siguiente invocación paga una recarga que nada explica."
                ),
            ))
        else:
            findings.append(ResidencyFinding(
                level="info",
                title="Reserva por delante del runtime",
                detail=(
                    f"Reservados {_gib(reserved_bytes)}, residentes con lease {_gib(leased)}. "
                    "Sin desalojo reciente en el log es una carga en frío en curso; solo "
                    "preocupa si persiste sin que el modelo llegue a aparecer cargado."
                ),
            ))

    if lmstudio:
        findings.append(ResidencyFinding(
            level="aviso",
            title=f"LM Studio retiene {len(lmstudio)} modelo(s) que no cuenta nadie",
            detail=(
                ", ".join(lmstudio) + ". No entran en este presupuesto ni en el runtime, "
                "pero ocupan el mismo pool. Descargarlos es la vía más directa de subir "
                "el techo de admisión."
            ),
        ))

    absurd = [
        item for item in evictions
        if item["gpu_free_bytes"] and item["predicted_bytes"]
        and item["gpu_free_bytes"] > item["predicted_bytes"] * 2
    ]
    if absurd:
        last = absurd[-1]
        findings.append(ResidencyFinding(
            level="problema",
            title=f"{len(absurd)} desalojo(s) con la GPU holgada en {since_hours} h",
            detail=(
                f"El último quiso meter {_gib(last['predicted_bytes'])} con "
                f"{_gib(last['gpu_free_bytes'])} libres en la GPU, y desalojó porque la RAM "
                f"libre del sistema era {_gib(last['system_free_bytes'])}. Desalojar no libera "
                "RAM del sistema, así que no resolvió nada."
            ),
        ))

    if free_ram is not None and loaded_models:
        biggest = max(_field(model, "size_vram_bytes") for model in loaded_models)
        if biggest > free_ram:
            findings.append(ResidencyFinding(
                level="problema",
                title="El modelo residente mayor ya no pasaría la admisión",
                detail=(
                    f"Ocupa {_gib(biggest)} y la RAM libre del sistema es {_gib(free_ram)}. "
                    "La próxima carga desalojará lo que haya."
                ),
            ))
        elif biggest > free_ram * 0.7:
            findings.append(ResidencyFinding(
                level="aviso",
                title="Poco margen antes de que el techo muerda",
                detail=(
                    f"Modelo mayor {_gib(biggest)}, RAM libre del sistema {_gib(free_ram)}. "
                    "Lo que sube ese techo es liberar RAM del sistema, no liberar GPU."
                ),
            ))

    if pool_bytes is not None and budget_bytes > pool_bytes:
        findings.append(ResidencyFinding(
            level="aviso",
            title="El presupuesto configurado excede el pool real",
            detail=(
                f"El presupuesto equivale a {_gib(budget_bytes)} y el runtime anuncia "
                f"{_gib(pool_bytes)}: el broker puede admitir trabajo que la GPU no sostiene."
            ),
        ))

    last_eviction = evictions[-1]["at"] if evictions else None
    return ResidencyReport(
        checked_at=datetime.now(timezone.utc),
        system_free_bytes=free_ram,
        runtime_pool_bytes=pool_bytes,
        lmstudio_loaded=lmstudio,
        eviction_count=len(evictions),
        eviction_window_hours=since_hours,
        last_eviction_at=last_eviction,
        log_available=bool(log["available"]),
        findings=findings,
    )
