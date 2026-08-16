"""Huella de la configuración que produce una respuesta.

Un nombre de modelo no es una identidad. `qwen3:8b` de hoy y `qwen3:8b` de
dentro de dos meses pueden ser binarios distintos, con otra cuantización o con
los pesos actualizados, y hasta ahora el broker no lo notaba. Y los pesos
tampoco bastan: la misma red neuronal, con los mismos pesos, responde distinto
si cambia la plantilla de chat, el tokenizador o el runtime que la ejecuta.
Actualizar Ollama puede cambiar la plantilla con la que se formatean los
mensajes sin tocar un solo byte de los pesos, y el `digest` seguirá siendo
idéntico.

Las consecuencias son del broker, no de un cliente: `app.model_stats` agrega
meses de invocaciones bajo un nombre, `app.model_quarantine` puede estar
apartando un modelo por fallos de una configuración que ya no existe y
`app.model_timing` estima tiempos con muestras de dos ejecuciones diferentes.

Cada componente viaja con su procedencia, misma política que el resto del
catálogo (ver `app.model_catalog`):

- `verificado`: obtenido del entorno de ejecución.
- `declarado`: dicho por el cliente o por la configuración, sin comprobar.
- `no_disponible`: no se pudo obtener.

**Nunca se rellena un hueco por inferencia.** Una huella incompleta es un
resultado legítimo; una huella inventada, no. En particular, ningún componente
se deduce del nombre del modelo.

Alcance deliberado: aquí vive la identidad de la CONFIGURACIÓN que ejecuta
(quién, con qué pesos, con qué plantilla, sobre qué runtime), no la de la
petición. Los parámetros de generación pedidos son propios de cada invocación y
viajan aparte (`generation` en la telemetría de invocación); meterlos en la
huella la haría distinta en cada petición y dejaría de servir para lo único que
sirve: saber si dos respuestas salieron de la misma máquina.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

ComponentStatus = Literal["verificado", "declarado", "no_disponible"]

VERIFIED: ComponentStatus = "verificado"
DECLARED: ComponentStatus = "declarado"
UNAVAILABLE: ComponentStatus = "no_disponible"

# Capa A — identidad declarada: lo que se pidió.
LAYER_DECLARED = ("provider", "deployment", "model")
# Capa B — identidad técnica observable: lo que se puede comprobar del entorno.
LAYER_OBSERVABLE = (
    "digest",
    "quantization",
    "parameter_size",
    "family",
    "tokenizer",
    "chat_template",
    "runtime",
    "runtime_version",
)
# Capa C — identidad remota: lo que el proveedor dice de sí mismo.
LAYER_REMOTE = ("system_fingerprint", "remote_model", "api_version")

# El conjunto de componentes es FIJO. Un componente que no se puede obtener
# aparece igualmente, con estado `no_disponible`: la ausencia de un dato es
# información real sobre el límite de lo que se puede afirmar del modelo, y
# omitir la clave la escondería.
COMPONENT_NAMES = (*LAYER_DECLARED, *LAYER_OBSERVABLE, *LAYER_REMOTE)


def component(value: Any, status: ComponentStatus) -> dict[str, Any]:
    """Un componente con su procedencia. Sin valor, el estado es no_disponible.

    Se normaliza aquí para que un proveedor no pueda declarar `verificado` algo
    que en realidad llegó vacío."""
    text = None if value is None else str(value).strip()
    if not text:
        return {"value": None, "status": UNAVAILABLE}
    return {"value": text, "status": status}


def missing() -> dict[str, Any]:
    return {"value": None, "status": UNAVAILABLE}


def digest_of(text: str | None, *, length: int = 32) -> str | None:
    """Hash corto de un texto largo (plantilla de chat, metadatos del
    tokenizador). Se guarda el hash y no el texto: lo que interesa es si
    cambió, no su contenido, y la plantilla de algunos modelos ocupa páginas."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def build(components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Huella completa: todos los componentes más su hash canónico.

    Los que el llamante no aporte quedan como `no_disponible`, de forma que dos
    proveedores distintos producen huellas con la misma forma y se pueden
    comparar componente a componente."""
    normalized = {
        name: components.get(name) or missing()
        for name in COMPONENT_NAMES
    }
    return {"components": normalized, "hash": canonical_hash(normalized)}


def canonical_hash(components: dict[str, dict[str, Any]]) -> str:
    """Hash estable del conjunto: mismo orden de claves, misma serialización.

    Dos arranques del broker con la misma configuración producen el mismo
    valor, que es lo que permite usarlo como identidad.

    El estado entra en el hash junto al valor a propósito: un componente que
    pasa de `verificado` a `declarado` describe una situación distinta —lo que
    se puede afirmar de esa ejecución ha cambiado— aunque el texto coincida.
    """
    payload = {
        name: [
            (components.get(name) or {}).get("value"),
            (components.get(name) or {}).get("status") or UNAVAILABLE,
        ]
        for name in sorted(components)
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def merge(base: dict[str, Any] | None, extra: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    """Añade a una huella los componentes observados al servir la respuesta.

    Es el caso de la capa C: `system_fingerprint` y el identificador de modelo
    devuelto no están en el catálogo, solo en la respuesta concreta. Un
    componente ya presente y disponible no se pisa con uno vacío."""
    if base is None and not extra:
        return None
    components = dict((base or {}).get("components") or {})
    for name, value in (extra or {}).items():
        if value and value.get("value") is not None:
            components[name] = value
    return build(components)


def changed_components(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Qué componentes difieren entre dos huellas, en orden estable.

    Distinguir «cambió el digest» de «cambió solo la plantilla» es justo lo que
    hace útil la señal: lo primero es otro modelo, lo segundo es el mismo modelo
    ejecutado de otra manera, y solo el segundo caso es invisible sin esto."""
    before = previous.get("components") or {}
    after = current.get("components") or {}
    changed: list[str] = []
    for name in COMPONENT_NAMES:
        left = before.get(name) or missing()
        right = after.get(name) or missing()
        if (left.get("value"), left.get("status")) != (right.get("value"), right.get("status")):
            changed.append(name)
    # Componentes fuera del conjunto conocido (una versión más nueva escribió la
    # fila): se comparan igual, para no perder un cambio por no reconocerlo.
    for name in sorted(set(before) | set(after)):
        if name not in COMPONENT_NAMES and (before.get(name) != after.get(name)):
            changed.append(name)
    return changed
