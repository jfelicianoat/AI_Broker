"""Estimación de tiempo esperado (app.model_timing).

Lo que se protege aquí es sobre todo lo que el estimador NO debe hacer:
inventar un número cuando no hay evidencia, o extrapolar la latencia en
caliente de un modelo a su comportamiento en frío.
"""
from __future__ import annotations

from app.model_stats import ModelStats
from app.model_timing import BASIS_BLENDED, BASIS_COLD, BASIS_WARM, estimate_seconds


def _stat(**kwargs) -> ModelStats:
    base = {"attempts": 10, "successes": 10, "avg_latency_ms": 2000.0, "avg_cost_usd": 0.0}
    return ModelStats(**{**base, **kwargs})


def test_without_enough_evidence_there_is_no_estimate() -> None:
    assert estimate_seconds(None, loaded=None, min_invocations=3) is None
    assert estimate_seconds(_stat(attempts=2, successes=2), loaded=None, min_invocations=3) is None


def test_without_measured_latency_there_is_no_estimate() -> None:
    """Puede haber intentos registrados y ninguna latencia (todos fallidos):
    contar eso como 0 s sería regalarle el primer puesto al peor modelo."""
    assert estimate_seconds(
        _stat(successes=0, avg_latency_ms=None), loaded=None, min_invocations=3,
    ) is None


def test_a_perfect_model_is_estimated_at_its_measured_latency() -> None:
    estimate = estimate_seconds(_stat(), loaded=None, min_invocations=3)

    assert estimate is not None
    assert estimate.latency_seconds == 2.0
    # Con Laplace, 10 de 10 da 11/12: ni el modelo perfecto llega a 1.0, así
    # que su tiempo esperado queda algo por encima de su latencia.
    assert 2.0 < estimate.seconds < 2.3
    assert estimate.basis == BASIS_BLENDED


def test_failures_lengthen_the_expected_time() -> None:
    """Un modelo que falla la mitad de las veces tarda el doble en dar una
    respuesta buena: el reintento también es espera."""
    fiable = estimate_seconds(_stat(attempts=10, successes=10), loaded=None, min_invocations=3)
    fragil = estimate_seconds(_stat(attempts=10, successes=5), loaded=None, min_invocations=3)

    assert fiable is not None and fragil is not None
    assert fragil.seconds > fiable.seconds
    assert fragil.reliability_penalty_seconds > fiable.reliability_penalty_seconds


def test_the_vram_state_picks_the_matching_measurement() -> None:
    stat = _stat(warm_latency_ms=1000.0, cold_latency_ms=40000.0)

    caliente = estimate_seconds(stat, loaded=True, min_invocations=3)
    frio = estimate_seconds(stat, loaded=False, min_invocations=3)

    assert caliente is not None and frio is not None
    assert caliente.basis == BASIS_WARM
    assert frio.basis == BASIS_COLD
    # El mismo modelo, cuarenta veces más lento por tener que cargarse.
    assert frio.seconds > caliente.seconds * 30


def test_missing_segment_falls_back_to_the_global_average_not_to_the_other() -> None:
    """Si solo hay muestras en caliente y el modelo está frío, se usa la media
    global: peor estimación, pero medida. Usar la de caliente sería afirmar
    algo que no se ha observado."""
    stat = _stat(avg_latency_ms=5000.0, warm_latency_ms=1000.0, cold_latency_ms=None)

    frio = estimate_seconds(stat, loaded=False, min_invocations=3)

    assert frio is not None
    assert frio.basis == BASIS_BLENDED
    assert frio.latency_seconds == 5.0


def test_without_recorded_sizes_the_request_load_is_ignored() -> None:
    """Sin tamaños registrados no se puede calcular un ritmo, y el estimador se
    queda en la latencia media en vez de fabricar una escala."""
    plano = estimate_seconds(_stat(), loaded=None, min_invocations=3)
    con_carga = estimate_seconds(_stat(), loaded=None, min_invocations=3, request_tokens=50000)

    assert plano is not None and con_carga is not None
    assert con_carga.seconds == plano.seconds
    assert con_carga.size_adjusted is False


def test_a_bigger_request_is_estimated_slower() -> None:
    stat = _stat(avg_tokens_input=500.0, avg_tokens_output=500.0)

    corta = estimate_seconds(stat, loaded=None, min_invocations=3, request_tokens=500)
    larga = estimate_seconds(stat, loaded=None, min_invocations=3, request_tokens=20000)

    assert corta is not None and larga is not None
    assert larga.seconds > corta.seconds * 10
    assert larga.size_adjusted is True


def test_the_model_measured_on_big_prompts_wins_the_big_request() -> None:
    """Los dos tardaron 2 s de media, pero uno lo hizo con peticiones veinte
    veces mayores: es mucho más rápido por token. Sin el ajuste por carga
    empatarían, y para un documento largo la elección sería una moneda al aire."""
    lento = _stat(avg_latency_ms=2000.0, avg_tokens_input=100.0, avg_tokens_output=100.0)
    rapido = _stat(avg_latency_ms=2000.0, avg_tokens_input=4000.0, avg_tokens_output=100.0)

    est_lento = estimate_seconds(lento, loaded=None, min_invocations=3, request_tokens=20000)
    est_rapido = estimate_seconds(rapido, loaded=None, min_invocations=3, request_tokens=20000)

    assert est_lento is not None and est_rapido is not None
    assert est_rapido.seconds < est_lento.seconds
    assert est_rapido.tokens_per_second > est_lento.tokens_per_second


def test_the_cold_load_cost_does_not_scale_with_the_request_size() -> None:
    """Cargar el modelo desde disco cuesta lo mismo tanto si luego se le piden
    diez tokens como diez mil: es coste fijo, y escalarlo inflaría el total."""
    stat = _stat(
        avg_latency_ms=11000.0,
        warm_latency_ms=1000.0,
        cold_latency_ms=31000.0,
        avg_tokens_input=500.0,
        avg_tokens_output=500.0,
    )

    frio = estimate_seconds(stat, loaded=False, min_invocations=3, request_tokens=1000)

    assert frio is not None
    # 31 s en frío menos 1 s en caliente = 30 s de carga, medidos.
    assert frio.load_overhead_seconds == 30.0
    # Y la generación se estima con el ritmo en caliente, no con el de la
    # media contaminada por la carga.
    assert frio.latency_seconds < 32.0


def test_explanation_names_the_source_of_the_number() -> None:
    stat = _stat(warm_latency_ms=1000.0)
    texto = estimate_seconds(stat, loaded=True, min_invocations=3).explain()

    assert "s estimados" in texto
    assert "medido en caliente" in texto
    assert "modelo ya en VRAM" in texto
    assert "10 invocaciones" in texto
