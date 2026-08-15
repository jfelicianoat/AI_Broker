"""Qué se rompe si se borra un modelo del disco.

El panel deja borrar un modelo local, y esa acción es irreversible. Antes de
preguntar «¿seguro?» conviene poder decir *qué* deja de funcionar: una
configuración que lo nombra a mano queda apuntando a algo que ya no existe y
falla en la siguiente tarea que la use, y borrar el único modelo con cierta
capacidad deja al broker sin poder atender esa clase de peticiones.

Módulo sin efectos ni dependencias de red: recibe la configuración y el
catálogo, y devuelve avisos en texto.
"""
from __future__ import annotations

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
