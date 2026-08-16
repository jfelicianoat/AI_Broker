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
    # Latencia media segmentada por si el modelo estaba ya cargado en VRAM.
    # Un modelo local en frío paga la carga desde disco dentro de su propia
    # latencia: mezclar ambos casos en una sola media da un número que no
    # describe ninguna de las dos situaciones. None = sin muestras de ese tipo
    # (lo normal en cloud, que nunca carga nada, y en filas antiguas).
    warm_latency_ms: float | None = None
    cold_latency_ms: float | None = None
    # Tamaño medio de las peticiones que produjeron esas latencias. Sin este
    # dato la latencia media es un número sin escala: un modelo medido con
    # preguntas de dos líneas parecería igual de rápido con un documento de
    # 40.000 tokens. Permite estimar en tokens/segundo (app.model_timing).
    avg_tokens_input: float | None = None
    avg_tokens_output: float | None = None

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
    warm_latencies: list[float] = field(default_factory=list)
    cold_latencies: list[float] = field(default_factory=list)
    tokens_input: list[float] = field(default_factory=list)
    tokens_output: list[float] = field(default_factory=list)


def load_model_stats(db: Database, *, window_days: int) -> dict[ModelKey, ModelStats]:
    """Agrega éxito, latencia media y coste medio por (modelo, tipo de tarea)
    en la ventana dada. Las filas anteriores a la columna task_type (NULL) se
    descartan: sin clasificar, no se puede saber a qué segmento pertenecen.

    Queda fuera el tráfico marcado como excluido del aprendizaje
    (TaskCreateRequest.exclude_from_model_learning). Sin ese filtro, cualquiera
    que someta un modelo a prompts deliberadamente difíciles le baja la
    fiabilidad al router: medir un modelo lo degradaba en producción. Ese
    tráfico sigue contando en coste y uso — lo único que no hace es mover los
    indicadores que alimentan decisiones automáticas."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = db.query_all(
        """
        SELECT provider, deployment, model, task_type, status, latency_ms, cost_usd, was_loaded,
               tokens_input, tokens_output
        FROM model_invocations
        WHERE created_at >= ? AND status IN ('completed', 'failed') AND task_type IS NOT NULL
          AND excluded_from_learning = 0
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
                latency = float(row["latency_ms"])
                bucket.latencies.append(latency)
                # was_loaded NULL (filas previas a la columna, o proveedores
                # que no cargan modelos) solo cuenta en la media global.
                if row["was_loaded"] is not None:
                    target = bucket.warm_latencies if row["was_loaded"] else bucket.cold_latencies
                    target.append(latency)
            if row["cost_usd"] is not None:
                bucket.costs.append(float(row["cost_usd"]))
            # Solo cuentan los tamaños de las invocaciones cuya latencia se ha
            # medido: si no fueran las mismas filas, los tokens/segundo
            # saldrían de dividir dos poblaciones distintas.
            if row["latency_ms"] is not None:
                bucket.tokens_input.append(float(row["tokens_input"] or 0))
                bucket.tokens_output.append(float(row["tokens_output"] or 0))

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        key: ModelStats(
            attempts=bucket.attempts,
            successes=bucket.successes,
            avg_latency_ms=mean(bucket.latencies),
            avg_cost_usd=mean(bucket.costs),
            warm_latency_ms=mean(bucket.warm_latencies),
            cold_latency_ms=mean(bucket.cold_latencies),
            avg_tokens_input=mean(bucket.tokens_input),
            avg_tokens_output=mean(bucket.tokens_output),
        )
        for key, bucket in accumulator.items()
    }
