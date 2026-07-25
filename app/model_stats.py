"""Métricas operativas por modelo, agregadas desde model_invocations.

La materia prima la producen los checkpoints de invocación (started →
completed/failed): aquí se convierte en la evidencia que consume la
selección adaptativa del router. Las invocaciones 'started' o 'ambiguous'
se excluyen: no se sabe cómo terminaron.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.db import Database

# Clave de identidad de un modelo EN UN TIPO DE TAREA dado:
# (provider, deployment, name, task_type), siempre en minúsculas para casar
# con las comparaciones del router. Segmentar por task_type (app.task_classifier)
# evita que un modelo bueno en prosa "gane" también el ranking de código solo
# por tener más volumen agregado.
ModelKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class ModelStats:
    attempts: int
    successes: int
    avg_latency_ms: float | None
    avg_cost_usd: float | None

    @property
    def success_rate(self) -> float:
        """Tasa de éxito con suavizado de Laplace: sin historial vale 0.5 y
        un único fallo no hunde al modelo a 0 (ni un único acierto lo sube a 1)."""
        return (self.successes + 1) / (self.attempts + 2)


@dataclass
class _Accumulator:
    attempts: int = 0
    successes: int = 0
    latencies: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)


def load_model_stats(db: Database, *, window_days: int) -> dict[ModelKey, ModelStats]:
    """Agrega éxito, latencia media y coste medio por (modelo, tipo de tarea)
    en la ventana dada. Las filas anteriores a la columna task_type (NULL) se
    descartan: sin clasificar, no se puede saber a qué segmento pertenecen."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = db.query_all(
        """
        SELECT provider, deployment, model, task_type, status, latency_ms, cost_usd
        FROM model_invocations
        WHERE created_at >= ? AND status IN ('completed', 'failed') AND task_type IS NOT NULL
        """,
        (cutoff,),
    )
    accumulator: dict[ModelKey, _Accumulator] = {}
    for row in rows:
        key: ModelKey = (
            str(row["provider"]).lower(),
            str(row["deployment"]).lower(),
            str(row["model"]).lower(),
            str(row["task_type"]).lower(),
        )
        bucket = accumulator.setdefault(key, _Accumulator())
        bucket.attempts += 1
        if row["status"] == "completed":
            bucket.successes += 1
            if row["latency_ms"] is not None:
                bucket.latencies.append(float(row["latency_ms"]))
            if row["cost_usd"] is not None:
                bucket.costs.append(float(row["cost_usd"]))
    return {
        key: ModelStats(
            attempts=bucket.attempts,
            successes=bucket.successes,
            avg_latency_ms=sum(bucket.latencies) / len(bucket.latencies) if bucket.latencies else None,
            avg_cost_usd=sum(bucket.costs) / len(bucket.costs) if bucket.costs else None,
        )
        for key, bucket in accumulator.items()
    }
