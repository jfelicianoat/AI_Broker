"""Cuarentena de modelos que han dejado de producir salida usable.

Un modelo puede estar en el catálogo, cargar sin protestar y aun así no servir:
pesos corruptos, una cuantización que el runtime no ejecuta bien, una plantilla
rota. El síntoma es siempre el mismo —invocaciones que terminan en un error
definitivo— y el coste, alto: cada intento paga la carga del modelo entera
antes de fallar.

Aquí se decide, con la evidencia que ya deja `model_invocations`, qué modelos
salen de la rotación. La regla es deliberadamente conservadora en las dos
direcciones: hacen falta varios fallos seguidos para apartar a uno, y la
cuarentena caduca sola, para que un modelo reinstalado vuelva sin que nadie
tenga que acordarse de rehabilitarlo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import ModelQuarantineConfig
from app.db import Database

# Identidad sin task_type: un modelo que devuelve basura la devuelve para todo,
# a diferencia de la calidad, que sí depende del tipo de tarea.
QuarantineKey = tuple[str, str, str]


@dataclass(frozen=True)
class QuarantineEntry:
    """Por qué un modelo está apartado. El motivo viaja hasta el catálogo: un
    modelo que desaparece de la selección sin explicación es un misterio que se
    paga depurando."""

    failures: int
    last_code: str
    last_failure_at: str

    @property
    def reason(self) -> str:
        return (
            f"{self.failures} fallos seguidos sin ningún acierto "
            f"(último: {self.last_code} el {self.last_failure_at[:19].replace('T', ' ')})"
        )


def load_quarantine(
    db: Database, settings: ModelQuarantineConfig
) -> dict[QuarantineKey, QuarantineEntry]:
    """Modelos apartados ahora mismo, con el motivo de cada uno.

    Cuenta hacia atrás desde la invocación más reciente: un éxito, por antiguo
    que sea dentro de la ventana, corta la racha y devuelve al modelo a la
    rotación. Solo cuentan los códigos declarados como definitivos; quedarse
    sin memoria o un proveedor caído son circunstancias, no defectos.

    Tampoco cuenta el tráfico excluido del aprendizaje
    (TaskCreateRequest.exclude_from_model_learning): apartar un modelo del
    catálogo por fallar donde alguien estaba INTENTANDO que fallara sería
    castigarlo por hacer justo lo esperable. Ni condena ni absuelve: esas
    invocaciones no rompen una racha ni la alimentan.
    """
    if not settings.enabled:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=settings.window_hours)).isoformat()
    rows = db.query_all(
        """
        SELECT provider, deployment, model, status, error_code, created_at
        FROM model_invocations
        WHERE created_at >= ? AND status IN ('completed', 'failed')
          AND excluded_from_learning = 0
        ORDER BY created_at DESC, id DESC
        """,
        (cutoff,),
    )
    definitive = {code.upper() for code in settings.definitive_codes}
    streaks: dict[QuarantineKey, list] = {}
    settled: set[QuarantineKey] = set()
    for row in rows:
        key: QuarantineKey = (
            str(row["provider"]).lower(),
            str(row["deployment"]).lower(),
            str(row["model"]).lower(),
        )
        if key in settled:
            continue
        if row["status"] == "completed":
            # Racha rota: el modelo ha demostrado que sigue respondiendo.
            settled.add(key)
            streaks.pop(key, None)
            continue
        code = str(row["error_code"] or "").upper()
        if code not in definitive:
            # Un fallo circunstancial no absuelve ni condena: no rompe la racha
            # de fallos definitivos ni cuenta para ella.
            continue
        streaks.setdefault(key, []).append((code, str(row["created_at"])))

    return {
        key: QuarantineEntry(
            failures=len(failures), last_code=failures[0][0], last_failure_at=failures[0][1]
        )
        for key, failures in streaks.items()
        if len(failures) >= settings.consecutive_failures
    }


def quarantine_key(entry: dict) -> QuarantineKey:
    """Clave de una fila del catálogo, con el mismo criterio de minúsculas."""
    return (
        str(entry.get("provider") or "").lower(),
        str(entry.get("deployment") or "").lower(),
        str(entry.get("name") or "").lower(),
    )
