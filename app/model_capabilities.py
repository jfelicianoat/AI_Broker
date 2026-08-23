"""Qué puede hacer un modelo con imágenes, y por qué se le cree.

La pregunta "¿este modelo ve imágenes?" se hacía en dos sitios con dos
respuestas posibles: `app.ingestion.vision` la resolvía para elegir quién
describe las figuras de un documento, y el enrutado no la hacía en absoluto
—cualquier modelo podía acabar atendiendo una petición con imágenes, y un
modelo solo-texto ante una imagen no falla: contesta inventando—. Aquí vive la
única respuesta, con la misma jerarquía de evidencia que gobierna el resto del
broker:

    sondeo real contra el endpoint  >  catálogo externo  >  nada

Un negativo verificado excluye aunque el catálogo afirme lo contrario: es la
misma regla que aplica la página de Modelos. Lo que NO entra aquí es la
heurística por nombre (`app.model_catalog` la usa para *describir* el catálogo
en el panel): adivinar por el nombre que un modelo ve imágenes y mandarle una
foto termina en una respuesta inventada, que es exactamente el fallo silencioso
que este módulo existe para evitar.

La generación de imágenes se mide igual, contra la otra modalidad: `image` en
la SALIDA del modelo, no en su entrada. Hoy ningún proveedor local la declara;
la evidencia sale del catálogo externo cuando el modelo es de cloud.
"""
from __future__ import annotations

from typing import Any

# Capacidades que un runtime puede declarar por su cuenta para decir "genero
# imágenes". Ollama no declara ninguna hoy; los OpenAI-compatibles que exponen
# modelos de imagen sí suelen etiquetarlas.
_IMAGE_OUTPUT_CAPABILITIES = {
    "image_generation", "image-generation", "image-output", "image_output",
    "text-to-image", "text_to_image",
}


def vision_evidence(entry: dict[str, Any]) -> str | None:
    """`probe` si el sondeo lo verificó, `catalog` si solo lo declara
    models.dev, None si no hay razón para creer que ve imágenes."""
    features = entry.get("features") or {}
    if "vision" in features:
        return "probe" if features["vision"] else None
    if (entry.get("catalog") or {}).get("vision"):
        return "catalog"
    return None


def image_output_evidence(entry: dict[str, Any]) -> str | None:
    """`probe` | `catalog` | `declared` si el modelo produce imágenes, None si no.

    `declared` es el runtime del proveedor diciéndolo en su lista de
    capacidades: no está verificado contra el endpoint como el sondeo, pero lo
    afirma quien sirve el modelo, así que vale más que no saber nada.
    """
    features = entry.get("features") or {}
    if "image_output" in features:
        return "probe" if features["image_output"] else None
    if (entry.get("catalog") or {}).get("image_output"):
        return "catalog"
    declared = {str(item).lower() for item in entry.get("capabilities") or []}
    if declared & _IMAGE_OUTPUT_CAPABILITIES:
        return "declared"
    return None


def supports_vision(entry: dict[str, Any]) -> bool:
    return vision_evidence(entry) is not None


def supports_image_output(entry: dict[str, Any]) -> bool:
    return image_output_evidence(entry) is not None


__all__ = [
    "image_output_evidence",
    "supports_image_output",
    "supports_vision",
    "vision_evidence",
]
