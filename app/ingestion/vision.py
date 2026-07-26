"""Elección del modelo que describe las figuras de un documento.

Antes la descripción de figuras iba siempre al mismo endpoint y al mismo
modelo escritos en `ingestion.images`: el broker no participaba, así que un
catálogo con varios modelos de visión desperdiciaba todos menos uno, y cambiar
de modelo obligaba a editar la configuración a mano.

Aquí se elige entre **todos** los modelos que declaran visión, con la misma
jerarquía de evidencia que gobierna el resto del broker: un sondeo real contra
el endpoint manda sobre lo que diga el catálogo externo, y lo que diga el
catálogo manda sobre no saber nada. `ingestion.images.model` sigue existiendo,
pero como override explícito, no como única vía.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.config import BrokerConfig


@dataclass(frozen=True)
class VisionTarget:
    """Destino concreto al que enviar una figura."""

    provider: str
    model: str
    # Deployment del catálogo (local/cloud/api). Viaja hasta el registro de la
    # invocación: sin él, las descripciones de figuras no se podrían casar con
    # el resto de la evidencia del mismo modelo.
    deployment: str
    base_url: str
    api_key: str | None
    # `ollama` habla su propio dialecto (`/api/chat` con `images: [b64]`);
    # el resto son OpenAI-compatibles (`/chat/completions` con `image_url`).
    dialect: str
    # De dónde sale la certeza de que este modelo ve imágenes. Viaja hasta los
    # metadatos del fichero para que el panel pueda declararlo.
    evidence: str


def _vision_evidence(entry: dict[str, Any]) -> str | None:
    """`probe` si el sondeo lo verificó, `catalog` si solo lo declara
    models.dev, None si no hay razón para creer que ve imágenes.

    Un negativo verificado excluye aunque el catálogo afirme lo contrario:
    es la misma regla que aplica la página de Modelos.
    """
    features = entry.get("features") or {}
    if "vision" in features:
        return "probe" if features["vision"] else None
    if (entry.get("catalog") or {}).get("vision"):
        return "catalog"
    return None


def _provider_endpoint(config: BrokerConfig, provider_id: str) -> tuple[str, str | None, str] | None:
    """(base_url, api_key, dialecto) del proveedor, o None si no es utilizable."""
    if provider_id == "ollama":
        ollama = config.providers.ollama
        if not ollama.enabled:
            return None
        return ollama.base_url, None, "ollama"
    if provider_id == "deepseek":
        # DeepSeek no tiene modelos de visión hoy; si algún día los tuviera,
        # entraría por aquí como OpenAI-compatible.
        deepseek = config.providers.deepseek
        if not deepseek.enabled:
            return None
        key = os.environ.get(deepseek.api_key_env) if deepseek.api_key_env else None
        return deepseek.base_url, key, "openai"
    for custom in config.providers.custom:
        if custom.id == provider_id and custom.enabled:
            key = os.environ.get(custom.api_key_env) if custom.api_key_env else None
            return custom.base_url, key, "openai"
    return None


def select_vision_target(
    config: BrokerConfig,
    catalog: list[dict[str, Any]],
    ranked_names: list[str] | None = None,
) -> VisionTarget | None:
    """Mejor modelo de visión disponible, o None si no hay ninguno.

    `ranked_names` permite pasar el orden que ya calculó el enrutador por
    evidencia operativa (éxito, latencia, coste). Cuando llega, decide el
    desempate dentro de cada nivel de certeza; cuando no, se conserva el orden
    del catálogo. La certeza manda siempre sobre el score: un modelo sondeado
    va antes que uno que solo está declarado, aunque el declarado puntúe mejor.
    """
    settings = config.ingestion.images
    override = (settings.model or "").strip()

    tiers: dict[str, list[VisionTarget]] = {"probe": [], "catalog": []}
    for entry in catalog:
        evidence = _vision_evidence(entry)
        if evidence is None:
            continue
        endpoint = _provider_endpoint(config, str(entry.get("provider") or ""))
        if endpoint is None:
            continue
        base_url, api_key, dialect = endpoint
        tiers[evidence].append(
            VisionTarget(
                provider=str(entry.get("provider") or ""),
                model=str(entry.get("name") or ""),
                deployment=str(entry.get("deployment") or "local"),
                base_url=base_url,
                api_key=api_key,
                dialect=dialect,
                evidence=evidence,
            )
        )

    candidates = tiers["probe"] + tiers["catalog"]
    if not candidates:
        return None

    # Override explícito: si el usuario nombró un modelo y existe con visión,
    # manda sobre cualquier score. Si lo nombró y NO está, no se le sustituye
    # en silencio por otro: se respeta la intención y se deja fallar arriba.
    if override:
        for target in candidates:
            if target.model == override:
                return target
        return None

    if ranked_names:
        order = {name: index for index, name in enumerate(ranked_names)}
        fallback = len(order)
        for tier in ("probe", "catalog"):
            tiers[tier].sort(key=lambda t: order.get(t.model, fallback))
        candidates = tiers["probe"] + tiers["catalog"]
    return candidates[0]
