"""Tiempo esperado hasta una respuesta correcta, en segundos.

El enrutado adaptativo puntuaba con un score multiobjetivo sin unidades: un
0.734 no se puede explicar, ni contrastar contra la realidad, ni enseñar en un
panel. Aquí se estima lo único que le importa a quien espera delante de la
pantalla: cuántos segundos va a tardar este modelo en devolver una respuesta
buena.

Dos correcciones sobre la latencia medida, ambas con dato real detrás:

- **Fiabilidad.** Un modelo que falla obliga a reintentar o a devolver un
  error, así que el tiempo hasta una respuesta *útil* es mayor que su latencia.
  Con probabilidad de éxito p, el número esperado de intentos es 1/p, de donde
  sale la división. No es una penalización arbitraria: es la esperanza.
- **Carga en frío.** Un modelo local que no está en VRAM paga la carga desde
  disco dentro de su latencia. Por eso las medidas se segmentan en caliente y
  en frío (ModelStats.warm_latency_ms / cold_latency_ms) y aquí se elige la que
  corresponde al estado actual de la máquina.

Lo que este módulo NO hace: inventar un número para un modelo sin medir. Sin
muestras suficientes devuelve None, y quien pregunta decide qué hacer con esa
ignorancia. El compromiso del proyecto es que ninguna cifra del panel sea una
suposición disfrazada de medida.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.model_stats import ModelStats

# Origen de la latencia usada en la estimación, para poder explicarla.
BASIS_WARM = "warm"
BASIS_COLD = "cold"
BASIS_BLENDED = "blended"

_BASIS_LABELS = {
    BASIS_WARM: "medido en caliente",
    BASIS_COLD: "medido en frío",
    BASIS_BLENDED: "medido sin distinguir carga",
}


@dataclass(frozen=True)
class TimeEstimate:
    """Estimación explicable: el total y las piezas de las que sale."""

    seconds: float
    latency_seconds: float
    success_rate: float
    attempts: int
    basis: str
    loaded: bool | None
    # Parte fija atribuible a cargar el modelo desde disco, cuando hay medidas
    # en caliente y en frío para separarla del tiempo de generación.
    load_overhead_seconds: float = 0.0
    # Tokens de entrada de ESTA petición, si se aplicó ajuste por tamaño.
    request_tokens: int | None = None
    tokens_per_second: float | None = None

    @property
    def size_adjusted(self) -> bool:
        return self.request_tokens is not None

    @property
    def reliability_penalty_seconds(self) -> float:
        """Cuánto del total se debe a que el modelo a veces falla."""
        return self.seconds - self.latency_seconds

    def explain(self) -> str:
        """Una línea legible para el panel y para los logs de decisión."""
        detail = _BASIS_LABELS.get(self.basis, self.basis)
        if self.loaded is False:
            detail += ", modelo no cargado"
        elif self.loaded is True:
            detail += ", modelo ya en VRAM"
        piezas = [f"{self.latency_seconds:.1f} s {detail}"]
        if self.load_overhead_seconds > 0:
            piezas.append(f"incluye {self.load_overhead_seconds:.1f} s de carga")
        if self.tokens_per_second is not None:
            ritmo = f"{self.tokens_per_second:.0f} tokens/s medidos"
            if self.request_tokens is not None:
                ritmo += f" sobre los {self.request_tokens} tokens de esta petición"
            piezas.append(ritmo)
        piezas.append(f"éxito {self.success_rate * 100:.0f}% sobre {self.attempts} invocaciones")
        return f"{self.seconds:.1f} s estimados ({'; '.join(piezas)})"


def estimate_seconds(
    stat: ModelStats | None,
    *,
    loaded: bool | None,
    min_invocations: int,
    request_tokens: int | None = None,
) -> TimeEstimate | None:
    """Tiempo esperado hasta una respuesta correcta, o None si no hay evidencia.

    `loaded` es el estado ACTUAL del modelo en la máquina (None si no aplica,
    como en cloud, o si no se pudo consultar). Determina qué media se usa.

    `request_tokens` son los tokens de entrada de la petición concreta. Con él
    la estimación deja de ser "lo que este modelo tardó de media" y pasa a ser
    "lo que va a tardar con ESTA carga", usando el ritmo medido en tokens por
    segundo. Sin él (o sin tamaños registrados) se cae a la latencia media, que
    describe una petición típica y no necesariamente la que se va a atender."""
    if stat is None or stat.attempts < min_invocations:
        return None
    latency_ms, basis = _pick_latency(stat, loaded)
    if latency_ms is None:
        return None
    overhead_ms, rate_ms = _split_load_overhead(stat, loaded, latency_ms)
    ms_per_token = _ms_per_token(stat, rate_ms)
    scaled_ms, applied = _apply_request_size(stat, rate_ms, ms_per_token, request_tokens)
    latency_seconds = (overhead_ms + scaled_ms) / 1000.0
    success_rate = stat.success_rate
    return TimeEstimate(
        # success_rate viene con suavizado de Laplace, así que nunca es 0 y la
        # división está siempre definida.
        seconds=latency_seconds / success_rate,
        latency_seconds=latency_seconds,
        success_rate=success_rate,
        attempts=stat.attempts,
        basis=basis,
        loaded=loaded,
        load_overhead_seconds=overhead_ms / 1000.0,
        request_tokens=request_tokens if applied else None,
        tokens_per_second=None if ms_per_token is None else 1000.0 / ms_per_token,
    )


def _split_load_overhead(
    stat: ModelStats, loaded: bool | None, latency_ms: float,
) -> tuple[float, float]:
    """Separa la carga desde disco (coste fijo) del tiempo de generación.

    Importa porque solo la segunda parte escala con el tamaño de la petición:
    cargar un modelo de 30B cuesta lo mismo tanto si luego se le piden diez
    tokens como diez mil. La diferencia entre la media en frío y la media en
    caliente ES esa carga, medida, sin constantes inventadas."""
    if loaded is False and stat.cold_latency_ms is not None and stat.warm_latency_ms is not None:
        overhead = max(0.0, stat.cold_latency_ms - stat.warm_latency_ms)
        return overhead, stat.warm_latency_ms
    return 0.0, latency_ms


def _ms_per_token(stat: ModelStats, rate_ms: float) -> float | None:
    """Ritmo observado: la latencia medida dividida entre los tokens que la
    provocaron (entrada más salida). Es propiedad de la medida, no de ninguna
    petición concreta, así que se puede enseñar en el panel tal cual."""
    measured_input = stat.avg_tokens_input
    measured_output = stat.avg_tokens_output
    if measured_input is None or measured_output is None:
        return None
    measured_total = measured_input + measured_output
    if measured_total <= 0:
        return None
    ms_per_token = rate_ms / measured_total
    return ms_per_token if ms_per_token > 0 else None


def _apply_request_size(
    stat: ModelStats, rate_ms: float, ms_per_token: float | None, request_tokens: int | None,
) -> tuple[float, bool]:
    """Reescala el tiempo de generación al tamaño de esta petición.

    A los tokens de entrada se le suman los de salida que ese modelo genera de
    media: cuánto va a responder no se sabe de antemano, pero sí se ha
    observado. Devuelve también si el ajuste llegó a aplicarse, porque sin
    tamaños registrados la estimación sigue siendo la latencia media y el panel
    no debe presentarla como si estuviera ajustada."""
    if request_tokens is None or ms_per_token is None or stat.avg_tokens_output is None:
        return rate_ms, False
    return ms_per_token * (request_tokens + stat.avg_tokens_output), True


def _pick_latency(stat: ModelStats, loaded: bool | None) -> tuple[float | None, str]:
    """La media que describe la situación actual, con reserva a la global.

    Si el modelo está cargado pero solo hay muestras en frío (o al revés), se
    cae a la media global: es peor estimación, pero sigue siendo una medida.
    Extrapolar de una población a la otra sí sería inventar."""
    if loaded is True and stat.warm_latency_ms is not None:
        return stat.warm_latency_ms, BASIS_WARM
    if loaded is False and stat.cold_latency_ms is not None:
        return stat.cold_latency_ms, BASIS_COLD
    return stat.avg_latency_ms, BASIS_BLENDED
