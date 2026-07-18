"""Meta-router: elige estrategia concreta para tareas `strategy: auto`.

La clasificación es TÉCNICA, no de dominio: mira si la petición necesita datos
actuales (→ agent con skills), si es ambigua/compleja y hay presupuesto para
varias opiniones (→ mixture), o si es directa (→ single, el mejor modelo). Cada
decisión se devuelve con sus señales y motivos para que sea trazable y para que
el aprendizaje futuro (pieza 3) tenga casos etiquetados desde el principio.

Módulo sin efectos: no toca red, catálogo ni BD. El coordinador aplica la
decisión y persiste el caso.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import StrategyRouterConfig
from app.schemas import DataClassification, TaskCreateRequest

# Señales de que la respuesta depende de información que el modelo no tiene
# (posterior a su corte de conocimiento, actual, o verificable en la web).
_RECENCY_TERMS = (
    "hoy", "ahora", "actual", "actualmente", "reciente", "recientes", "última",
    "últimas", "último", "últimos", "noticias", "novedad", "novedades", "precio",
    "cotización", "clima", "tiempo que hace", "en directo", "en vivo",
    "today", "now", "current", "currently", "latest", "recent", "news", "price",
    "weather", "this week", "this year",
)
# Señales de cálculo exacto (mejor con la skill calculator que a ojo).
_CALC_TERMS = ("calcula", "cuánto es", "cuanto es", "suma", "multiplica", "porcentaje", "calculate", "compute")
# Señales de tarea abierta que se beneficia de varias perspectivas.
_DELIBERATION_TERMS = (
    "compara", "comparación", "contrasta", "analiza", "análisis", "evalúa",
    "evaluación", "pros y contras", "ventajas y desventajas", "en profundidad",
    "detalladamente", "exhaustivo", "argumenta", "debate", "valora",
    "compare", "contrast", "analyze", "analyse", "evaluate", "pros and cons",
    "in depth", "thoroughly", "trade-offs", "tradeoffs",
)
_YEAR_RE = re.compile(r"\b20[2-9]\d\b")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)


@dataclass
class RoutingDecision:
    strategy: str  # single | agent | mixture_of_agents
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    learned: bool = False


# Señales booleanas que definen el "tipo" de petición para agrupar casos.
_BUCKET_KEYS = ("needs_recent", "needs_calc", "has_url", "deliberative", "long_prompt", "high_stakes")


def signal_bucket(signals: dict[str, Any]) -> str:
    """Huella estable de una petición: mismas señales → mismo grupo de casos."""
    return ",".join(f"{key}={1 if signals.get(key) else 0}" for key in _BUCKET_KEYS)


_BUCKET_LABELS = {
    "needs_recent": "info actual",
    "needs_calc": "cálculo",
    "has_url": "URL",
    "deliberative": "deliberativa",
    "long_prompt": "prompt largo",
    "high_stakes": "datos sensibles",
}


def heuristic_for_bucket(bucket: str) -> str:
    """Estrategia que la heurística elegiría para un bucket (asume presupuesto
    suficiente, ya que la señal de presupuesto no forma parte del bucket)."""
    signals: dict[str, Any] = {"budget_ok_for_mixture": True}
    for part in bucket.split(","):
        key, _, value = part.partition("=")
        if key in _BUCKET_KEYS:
            signals[key] = value == "1"
    return strategy_for_signals(signals).strategy


def describe_bucket(bucket: str) -> str:
    """Traduce una huella (needs_recent=1,...) a etiquetas legibles de las
    señales activas, p. ej. 'info actual · cálculo'. 'petición directa' si ninguna."""
    active = []
    for part in bucket.split(","):
        key, _, value = part.partition("=")
        if value == "1" and key in _BUCKET_LABELS:
            active.append(_BUCKET_LABELS[key])
    return " · ".join(active) if active else "petición directa"


def recommend_from_cases(
    heuristic: str,
    cases: list[dict[str, Any]],
    *,
    min_cases: int,
    escalation_threshold: float,
    failure_threshold: float,
) -> tuple[str, str] | None:
    """Recomienda una estrategia a partir de la evidencia de casos previos del
    mismo bucket. Devuelve (estrategia, motivo) o None si no hay evidencia
    suficiente o la heurística ya es la mejor. Reglas honestas, sin métricas de
    calidad inventadas: usa escalados (la respuesta single no bastó) y fracasos."""
    if len(cases) < min_cases:
        return None

    # Regla 1 — escalado: si los casos que empezaron en single escalaron a
    # menudo, es más barato ir directo a mixture y ahorrar el intento single.
    single_cases = [c for c in cases if c["chosen_strategy"] == "single"]
    if heuristic == "single" and len(single_cases) >= min_cases:
        escalated = sum(1 for c in single_cases if c["escalated"])
        rate = escalated / len(single_cases)
        if rate >= escalation_threshold:
            return (
                "mixture_of_agents",
                f"aprendido: en {escalated}/{len(single_cases)} casos similares el "
                "modelo único no bastó y hubo que escalar",
            )

    # Regla 2 — fracaso: si la estrategia heurística falla mucho en este bucket,
    # se prefiere la estrategia terminal con mejor tasa de éxito.
    heuristic_cases = [c for c in cases if c["final_strategy"] == heuristic]
    if len(heuristic_cases) >= min_cases:
        failures = sum(1 for c in heuristic_cases if c["status"] != "completed")
        if failures / len(heuristic_cases) >= failure_threshold:
            best = _best_by_success(cases)
            if best is not None and best != heuristic:
                return (best, f"aprendido: {heuristic} falló en {failures}/{len(heuristic_cases)} casos similares")
    return None


def _best_by_success(cases: list[dict[str, Any]]) -> str | None:
    stats: dict[str, list[int]] = {}
    for case in cases:
        strategy = case["final_strategy"]
        bucket = stats.setdefault(strategy, [0, 0])
        bucket[0] += 1
        if case["status"] == "completed":
            bucket[1] += 1
    if not stats:
        return None
    # Mejor tasa de éxito; empate por más casos (más evidencia).
    return max(stats, key=lambda s: (stats[s][1] / stats[s][0], stats[s][0]))


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def strategy_for_signals(signals: dict[str, Any]) -> RoutingDecision:
    """Decisión heurística a partir de las señales ya extraídas. Precedencia:
    agent (necesita datos que ningún modelo tiene) > mixture (ambigua y hay
    presupuesto) > single. Reutilizado por la clasificación de una petición y
    por la vista de enrutamiento (que solo conoce las señales del bucket)."""
    needs_recent = bool(signals.get("needs_recent"))
    needs_calc = bool(signals.get("needs_calc"))
    has_url = bool(signals.get("has_url"))
    deliberative = bool(signals.get("deliberative"))
    long_prompt = bool(signals.get("long_prompt"))
    high_stakes = bool(signals.get("high_stakes"))
    budget_ok_for_mixture = bool(signals.get("budget_ok_for_mixture", True))

    if needs_recent or needs_calc or has_url:
        reasons = []
        if needs_recent:
            reasons.append("la petición depende de información actual")
        if needs_calc:
            reasons.append("requiere cálculo exacto")
        if has_url:
            reasons.append("referencia una URL a leer")
        return RoutingDecision("agent", reasons, signals)

    if (deliberative or long_prompt or high_stakes) and budget_ok_for_mixture:
        reasons = []
        if deliberative:
            reasons.append("tarea deliberativa (comparar/analizar/evaluar)")
        if long_prompt:
            reasons.append(f"prompt extenso ({signals.get('prompt_chars', 0)} caracteres)")
        if high_stakes:
            reasons.append("clasificación de datos sensible")
        return RoutingDecision("mixture_of_agents", reasons, signals)

    reasons = ["petición directa: un solo modelo es suficiente"]
    if (deliberative or long_prompt or high_stakes) and not budget_ok_for_mixture:
        reasons.append("mixture descartado por presupuesto insuficiente")
    return RoutingDecision("single", reasons, signals)


def classify_request(request: TaskCreateRequest, config: StrategyRouterConfig) -> RoutingDecision:
    """Extrae las señales técnicas de la petición y decide con strategy_for_signals."""
    prompt = request.content.prompt or ""
    lowered = prompt.lower()
    budget = request.model_requirements.max_cost_usd
    signals = {
        "prompt_chars": len(prompt),
        "needs_recent": _contains_any(lowered, _RECENCY_TERMS) or bool(_YEAR_RE.search(prompt)),
        "needs_calc": _contains_any(lowered, _CALC_TERMS),
        "has_url": bool(_URL_RE.search(prompt)),
        "deliberative": _contains_any(lowered, _DELIBERATION_TERMS),
        "long_prompt": len(prompt) >= config.mixture_min_prompt_chars,
        "high_stakes": request.risk.data_classification == DataClassification.confidential,
        "budget_ok_for_mixture": budget is None or budget >= config.mixture_min_budget_usd,
    }
    return strategy_for_signals(signals)
