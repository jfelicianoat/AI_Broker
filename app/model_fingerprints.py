"""Señal de cambio de configuración de un modelo conocido.

Un modelo que empeora de la noche a la mañana es hoy indetectable: se manifiesta
como «este modelo se ha vuelto tonto» sin ninguna pista de por qué. La causa
puede ser una reinstalación con otra cuantización, una actualización del runtime
o una plantilla de chat reescrita sin tocar los pesos.

Aquí se guarda la última huella vista de cada modelo y se deja constancia cuando
cambia, diciendo QUÉ componentes cambiaron. Esa distinción es lo que hace útil
la señal: «cambió el digest» es otro modelo, «cambió solo la plantilla» es el
mismo modelo ejecutado de otra manera, y el segundo caso es justo el que no se
veía.

Lo que este módulo NO hace: segmentar las métricas ni la cuarentena por huella.
Es la consecuencia lógica de tener la señal, toca el router adaptativo y trae
consigo la pregunta de qué se hace con la evidencia histórica acumulada, así que
merece su propia entrega. De momento el evento queda registrado y a la vista.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.db import Database, dumps_json, loads_json
from app.execution_fingerprint import changed_components

logger = logging.getLogger("ai_broker.model_fingerprints")

FingerprintKey = tuple[str, str, str]

# Resultado del registro, para que quien llame sepa si hubo señal.
FIRST_SEEN = "first_seen"
UNCHANGED = "unchanged"
CHANGED = "changed"


def record_fingerprint(
    db: Database, key: FingerprintKey, current: dict[str, Any]
) -> str:
    """Guarda la huella vigente de un modelo y señala el cambio si lo hay.

    La primera vez que se ve un modelo NO es un cambio: no hay nada con lo que
    comparar, y tratarlo como tal llenaría el historial de eventos falsos en el
    primer arranque. El evento se emite una sola vez por cambio porque la fila
    se actualiza en la misma transacción que lo registra.
    """
    provider, deployment, model = key
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT fingerprint_hash, components_json FROM model_fingerprints "
            "WHERE provider = ? AND deployment = ? AND model = ?",
            (provider, deployment, model),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO model_fingerprints (provider, deployment, model, fingerprint_hash, "
                "components_json, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    provider, deployment, model, current["hash"],
                    dumps_json(current.get("components") or {}), now, now,
                ),
            )
            return FIRST_SEEN
        if str(row["fingerprint_hash"]) == current["hash"]:
            return UNCHANGED
        previous_components: dict[str, Any] = loads_json(row["components_json"], {}) or {}
        previous = {"hash": str(row["fingerprint_hash"]), "components": previous_components}
        changed = changed_components(previous, current)
        connection.execute(
            "UPDATE model_fingerprints SET fingerprint_hash = ?, components_json = ?, updated_at = ? "
            "WHERE provider = ? AND deployment = ? AND model = ?",
            (
                current["hash"], dumps_json(current.get("components") or {}), now,
                provider, deployment, model,
            ),
        )
        connection.execute(
            "INSERT INTO events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                None,
                "model.fingerprint_changed",
                dumps_json({
                    "provider": provider,
                    "deployment": deployment,
                    "model": model,
                    "changed": changed,
                    "previous_hash": previous["hash"],
                    "current_hash": current["hash"],
                    "previous": {name: previous_components.get(name) for name in changed},
                    "current": {
                        name: (current.get("components") or {}).get(name) for name in changed
                    },
                }),
                now,
            ),
        )
    logger.info(
        "model.fingerprint_changed",
        extra={
            "event": "model.fingerprint_changed",
            "model": f"{provider}/{deployment}/{model}",
            "changed": changed,
        },
    )
    return CHANGED


def fingerprint_recorder(db: Database):
    """Callback para el proveedor enrutado (ver RoutedModelProvider).

    Se le pasa la BD desde `create_app`, igual que a las métricas y a la
    cuarentena: el proveedor no conoce la persistencia, y un problema
    escribiendo la señal no puede dejar al broker sin catálogo."""

    def record(key: FingerprintKey, current: dict[str, Any]) -> str:
        try:
            return record_fingerprint(db, key, current)
        except Exception:
            logger.warning("model.fingerprint_record_failed", exc_info=True)
            return UNCHANGED

    return record
