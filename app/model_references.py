"""Qué se rompe si se borra un modelo del disco.

El panel deja borrar un modelo local, y esa acción es irreversible. Antes de
preguntar «¿seguro?» conviene poder decir *qué* deja de funcionar: una
configuración que lo nombra a mano queda apuntando a algo que ya no existe y
falla en la siguiente tarea que la use, y borrar el único modelo con cierta
capacidad deja al broker sin poder atender esa clase de peticiones.

Módulo sin efectos ni dependencias de red: recibe la configuración y el
catálogo, y devuelve avisos en texto. La única lectura es el tamaño de algún
blob en disco, en `deletion_footprint`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.config import BrokerConfig
from app.schemas import is_local_deployment

# Capacidades cuya pérdida deja al broker sin poder atender una clase entera de
# peticiones. `completion` queda fuera: quedarse con un solo modelo de chat es
# normal, y avisar de ello en cada borrado sería ruido.
_CRITICAL_CAPABILITIES = ("embedding",)


def deletion_warnings(
    config: BrokerConfig,
    catalog: list[dict[str, Any]],
    provider_id: str,
    model_name: str,
) -> list[str]:
    """Avisos de lo que dejará de funcionar al borrar ese modelo.

    Lista vacía = nada en la configuración lo nombra y no es el último de su
    clase. No bloquea el borrado: la decisión sigue siendo del operador, que es
    quien sabe si le importa.
    """
    warnings: list[str] = []
    warnings.extend(_config_references(config, model_name))
    warnings.extend(_capabilities_lost(catalog, provider_id, model_name))
    return warnings


def _config_references(config: BrokerConfig, model_name: str) -> list[str]:
    """Sitios de `broker_config.yaml` que nombran ese modelo exactamente.

    Solo coincidencia exacta: los patrones de `task_affinity` quedan fuera a
    propósito porque una exclusión que deja de casar con algo no rompe nada,
    solo sobra.
    """
    found: list[str] = []
    if config.ingestion.images.model == model_name:
        found.append(
            "ingestion.images.model — la descripción de figuras de los documentos "
            "dejará de funcionar hasta que configures otro modelo de visión"
        )
    if config.providers.deepseek.default_model == model_name:
        found.append("providers.deepseek.default_model — es el modelo por defecto de DeepSeek")
    for provider in config.providers.custom:
        for declared in provider.models:
            if declared.name == model_name:
                found.append(
                    f"providers.custom[{provider.id}].models — ese proveedor lo declara "
                    "y seguirá ofreciéndolo aunque ya no exista"
                )
    return found


def _capabilities_lost(
    catalog: list[dict[str, Any]], provider_id: str, model_name: str,
) -> list[str]:
    """Capacidades que se quedan sin ningún modelo LOCAL al borrar este.

    Se mira solo lo local a propósito: quien borra para hacer sitio en el disco
    quiere saber qué deja de poder hacer sin salir de la máquina, y decir que
    «queda un modelo cloud» sería contestar a otra pregunta —además de una que
    su clasificación de datos puede tener prohibida.
    """
    lost: list[str] = []
    target = next(
        (
            item for item in catalog
            if item.get("name") == model_name
            and str(item.get("provider") or "").lower() == provider_id.lower()
        ),
        None,
    )
    if target is None:
        return lost
    target_capabilities = {str(item) for item in (target.get("capabilities") or [])}
    for capability in _CRITICAL_CAPABILITIES:
        if capability not in target_capabilities:
            continue
        survivors = [
            item for item in catalog
            if item is not target
            and is_local_deployment(item.get("deployment"))
            and capability in {str(value) for value in (item.get("capabilities") or [])}
        ]
        if not survivors:
            lost.append(
                f"es el ÚNICO modelo local con capacidad «{capability}»: sin él, "
                "las tareas que la necesiten no tendrán dónde ejecutarse en esta máquina"
            )
    return lost


@dataclass(frozen=True)
class DeletionFootprint:
    """Lo que de verdad libera borrar un modelo.

    `freed_bytes` es None cuando no se puede saber: sin la lista de ficheros
    del modelo, o cuando comparte parte de ellos y los que no comparte no se
    pueden medir desde aquí. Mejor no dar cifra que dar una inventada."""

    freed_bytes: int | None
    shared_with: tuple[str, ...]


def deletion_footprint(
    catalog: list[dict[str, Any]],
    provider_id: str,
    model_name: str,
) -> DeletionFootprint:
    """Espacio que libera borrar este modelo y con quién comparte ficheros.

    Ollama guarda cada modelo como un manifiesto que apunta a blobs, y varios
    modelos pueden apuntar a los mismos: un alias (`x:latest` y `x:30b`) o un
    modelo creado desde otro con distinto system prompt. Al borrar, Ollama solo
    quita los blobs que nadie más usa, así que el tamaño del modelo no es lo
    que se libera: con un alias no se libera nada.

    `blobs` son las rutas de los ficheros grandes (pesos, proyector) que da
    `/api/show`. Solo cuando la coincidencia es parcial hay que medir los
    ficheros propios en disco, y eso solo funciona si Ollama corre en esta
    misma máquina."""
    provider = provider_id.lower()
    local = [
        item for item in catalog
        if str(item.get("provider") or "").lower() == provider
        and is_local_deployment(item.get("deployment"))
    ]
    entry = next((item for item in local if item.get("name") == model_name), None)
    mine = set((entry or {}).get("blobs") or [])
    if entry is None or not mine:
        return DeletionFootprint(freed_bytes=None, shared_with=())
    shared_with: list[str] = []
    used_by_others: set[str] = set()
    for item in local:
        if item.get("name") == model_name:
            continue
        theirs = mine & set(item.get("blobs") or [])
        if theirs:
            shared_with.append(str(item.get("name")))
            used_by_others |= theirs
    if not shared_with:
        return DeletionFootprint(freed_bytes=int(entry.get("size_bytes") or 0), shared_with=())
    own = mine - used_by_others
    if not own:
        return DeletionFootprint(freed_bytes=0, shared_with=tuple(sorted(shared_with)))
    try:
        freed: int | None = sum(os.path.getsize(path) for path in own)
    except OSError:
        freed = None
    return DeletionFootprint(freed_bytes=freed, shared_with=tuple(sorted(shared_with)))
