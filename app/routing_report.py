"""Informe descargable de la pestaña Enrutamiento.

Es un HTML autocontenido: las gráficas son SVG generado aquí, sin librerías
ni recursos externos, para que el fichero descargado se abra igual sin el
broker delante y se pueda guardar como PDF desde el diálogo de impresión del
navegador (no hay ninguna librería de PDF instalada y no merece la pena una
dependencia nativa para esto).

El tooltip de cada marca es un <title> de SVG: lo pinta el navegador sin
JavaScript, que la CSP del panel no permitiría en línea.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any

from markupsafe import Markup

from app.db import Database

# Pasos de la paleta de referencia de visualización (serie 1 azul y estados),
# en su variante para superficie oscura; el CSS del informe los cambia a la
# variante clara al imprimir mediante las variables --series-*.
_BAR = "var(--series-1)"
_OK = "var(--status-good)"
_FAIL = "var(--status-critical)"


@dataclass(frozen=True)
class BarRow:
    label: str
    value: float
    display: str
    detail: str = ""


def routing_activity(db: Database, *, window_days: int) -> list[dict[str, Any]]:
    """Actividad diaria de la ventana: invocaciones clasificadas (completadas y
    fallidas) y casos del meta-router. Incluye los días sin actividad, para
    que la gráfica no comprima los huecos y el ritmo se lea bien.

    La población es exactamente la de app.model_stats.load_model_stats (mismo
    corte móvil de window_days × 24 h y fuera el tráfico excluido del
    aprendizaje), para que la suma de los días cuadre con las tablas por
    modelo y los totales del resumen. Por eso el primer día sale parcial."""
    cutoff_at = datetime.now(timezone.utc) - timedelta(days=window_days)
    cutoff = cutoff_at.isoformat()
    start = cutoff_at.date()
    today = datetime.now(timezone.utc).date()
    days: dict[str, dict[str, Any]] = {}
    for offset in range((today - start).days + 1):
        key = (start + timedelta(days=offset)).isoformat()
        days[key] = {"day": key, "completed": 0, "failed": 0, "excluded": 0, "routing_cases": 0}
    for row in db.query_all(
        "SELECT substr(created_at, 1, 10) AS day, status, COUNT(*) AS n "
        "FROM model_invocations WHERE created_at >= ? AND task_type IS NOT NULL "
        "AND status IN ('completed', 'failed') AND excluded_from_learning = 0 "
        "GROUP BY day, status",
        (cutoff,),
    ):
        entry = days.get(str(row["day"]))
        if entry is not None:
            entry[str(row["status"])] += int(row["n"])
    # El excluido se cuenta aparte en vez de mezclarse: no mueve el router,
    # pero es tráfico real y sin él la actividad parecería menor de la que hubo.
    for row in db.query_all(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM model_invocations WHERE created_at >= ? AND task_type IS NOT NULL "
        "AND status IN ('completed', 'failed') AND excluded_from_learning = 1 GROUP BY day",
        (cutoff,),
    ):
        entry = days.get(str(row["day"]))
        if entry is not None:
            entry["excluded"] = int(row["n"])
    for row in db.query_all(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM routing_cases WHERE created_at >= ? GROUP BY day",
        (cutoff,),
    ):
        entry = days.get(str(row["day"]))
        if entry is not None:
            entry["routing_cases"] = int(row["n"])
    return list(days.values())


def strategy_totals(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Estrategias finales sumadas sobre todos los tipos de petición."""
    totals: dict[str, dict[str, int]] = {}
    for bucket in buckets:
        for strat in bucket["strategies"]:
            entry = totals.setdefault(strat["name"], {"total": 0, "completed": 0})
            entry["total"] += strat["total"]
            entry["completed"] += strat["completed"]
    return [
        {
            "name": name,
            "total": data["total"],
            "completed": data["completed"],
            "success_rate": round(data["completed"] / data["total"] * 100) if data["total"] else 0,
        }
        for name, data in sorted(totals.items(), key=lambda kv: kv[1]["total"], reverse=True)
    ]


def report_summary(
    buckets: list[dict[str, Any]],
    model_routing: dict[str, Any],
    activity: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = [row for group in model_routing["groups"] for row in group["rows"]]
    attempts = sum(row["attempts"] for row in rows)
    # Con los aciertos crudos: success_rate está suavizado (Laplace) y
    # recomponerlo inflaba o hundía el total según el tamaño de cada fila.
    successes = sum(row["successes"] for row in rows)
    return {
        # Los tipos de petición acumulan todo el histórico (el aprendizaje no
        # tiene ventana); la actividad diaria, solo la ventana. Se dan ambos.
        "routing_cases": sum(bucket["total"] for bucket in buckets),
        "routing_cases_window": sum(day["routing_cases"] for day in activity),
        "excluded": sum(day["excluded"] for day in activity),
        "request_types": len(buckets),
        "learned": sum(1 for bucket in buckets if bucket["recommendation"]),
        "invocations": attempts,
        "models": len({(row["provider"], row["model"]) for row in rows}),
        "success_rate": round(successes / attempts * 100) if attempts else None,
        "active_days": sum(1 for day in activity if day["completed"] or day["failed"]),
    }


def hbar_chart(rows: list[BarRow], *, title: str, max_value: float | None = None) -> Markup:
    """Barras horizontales de una sola serie, con la etiqueta a la izquierda y
    el valor rotulado al final de cada barra."""
    if not rows:
        return Markup("")
    width, label_w, value_w, row_h, bar_h = 760, 260, 90, 30, 16
    plot_w = width - label_w - value_w
    height = row_h * len(rows) + 8
    top = max_value if max_value else max((row.value for row in rows), default=0) or 1
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(title)}" preserveAspectRatio="xMinYMin meet">'
    ]
    for index, row in enumerate(rows):
        y = index * row_h + 4
        bar_w = max(2.0, plot_w * (row.value / top)) if row.value > 0 else 0.0
        tip = escape(f"{row.label}: {row.display}" + (f" — {row.detail}" if row.detail else ""))
        label = row.label if len(row.label) <= 34 else row.label[:33] + "…"
        parts.append(
            f'<g><title>{tip}</title>'
            f'<rect class="hit" x="0" y="{y}" width="{width}" height="{row_h}"/>'
            f'<text class="axis-label" x="{label_w - 10}" y="{y + row_h / 2}" '
            f'text-anchor="end" dominant-baseline="middle">{escape(label)}</text>'
            f'<rect x="{label_w}" y="{y + (row_h - bar_h) / 2}" width="{plot_w}" '
            f'height="{bar_h}" class="track" rx="4"/>'
        )
        if bar_w:
            parts.append(
                f'<rect x="{label_w}" y="{y + (row_h - bar_h) / 2}" width="{bar_w:.1f}" '
                f'height="{bar_h}" rx="4" fill="{_BAR}"/>'
            )
        parts.append(
            f'<text class="value-label" x="{label_w + plot_w + 10}" y="{y + row_h / 2}" '
            f'dominant-baseline="middle">{escape(row.display)}</text></g>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def daily_chart(activity: list[dict[str, Any]], *, title: str) -> Markup:
    """Columnas apiladas por día: completadas abajo, fallidas encima, con 2 px
    de superficie entre segmentos para que no se fundan."""
    if not activity:
        return Markup("")
    width, height, left, bottom, top_pad = 760, 240, 44, 30, 12
    plot_w, plot_h = width - left - 8, height - bottom - top_pad
    peak = max((day["completed"] + day["failed"] for day in activity), default=0)
    scale_top = _nice_ceiling(peak)
    slot = plot_w / len(activity)
    bar_w = max(2.0, min(22.0, slot - 3))
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(title)}" preserveAspectRatio="xMinYMin meet">'
    ]
    for step in range(5):
        value = scale_top * step / 4
        y = top_pad + plot_h - plot_h * step / 4
        parts.append(
            f'<line class="grid" x1="{left}" x2="{width - 8}" y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text class="axis-label" x="{left - 6}" y="{y:.1f}" text-anchor="end" '
            f'dominant-baseline="middle">{_compact(value)}</text>'
        )
    label_every = max(1, round(len(activity) / 8))
    for index, day in enumerate(activity):
        x = left + index * slot + (slot - bar_w) / 2
        base = top_pad + plot_h
        ok_h = plot_h * day["completed"] / scale_top
        fail_h = plot_h * day["failed"] / scale_top
        when = date.fromisoformat(day["day"])
        tip = escape(
            f"{when:%d/%m/%Y}: {day['completed']} completadas, {day['failed']} fallidas, "
            f"{day['routing_cases']} casos del meta-router"
        )
        parts.append(
            f'<g><title>{tip}</title><rect class="hit" x="{left + index * slot:.1f}" '
            f'y="{top_pad}" width="{slot:.1f}" height="{plot_h}"/>'
        )
        if ok_h:
            parts.append(
                f'<rect x="{x:.1f}" y="{base - ok_h:.1f}" width="{bar_w:.1f}" '
                f'height="{ok_h:.1f}" fill="{_OK}"/>'
            )
        if fail_h:
            gap = 2 if ok_h else 0
            parts.append(
                f'<rect x="{x:.1f}" y="{base - ok_h - gap - fail_h:.1f}" width="{bar_w:.1f}" '
                f'height="{fail_h:.1f}" fill="{_FAIL}"/>'
            )
        parts.append("</g>")
        if index % label_every == 0:
            parts.append(
                f'<text class="axis-label" x="{x + bar_w / 2:.1f}" y="{height - 10}" '
                f'text-anchor="middle">{when:%d/%m}</text>'
            )
    parts.append(
        f'<line class="baseline" x1="{left}" x2="{width - 8}" '
        f'y1="{top_pad + plot_h}" y2="{top_pad + plot_h}"/></svg>'
    )
    return Markup("".join(parts))


def _nice_ceiling(value: float) -> float:
    if value <= 4:
        return 4
    magnitude = 10 ** (len(str(int(value))) - 1)
    for factor in (1, 2, 2.5, 5, 10):
        if value <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _compact(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def build_report_charts(
    buckets: list[dict[str, Any]],
    model_routing: dict[str, Any],
    affinity: dict[str, Any],
    activity: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    charts: dict[str, Any] = {
        "activity": daily_chart(activity, title="Invocaciones por día"),
        "failures": hbar_chart(
            [
                BarRow(f["code"], f["count"], str(f["count"]),
                       ", ".join(f"{name} ({n})" for name, n in f["top_models"]))
                for f in (failures or [])
            ],
            title="Fallos por tipo",
        ),
        "buckets": hbar_chart(
            [
                BarRow(b["label"], b["total"], f"{b['total']} caso{'' if b['total'] == 1 else 's'}")
                for b in buckets
            ],
            title="Casos por tipo de petición",
        ),
        "strategies": hbar_chart(
            [
                BarRow(s["name"], s["success_rate"], f"{s['success_rate']} %",
                       f"{s['completed']} de {s['total']} completadas")
                for s in strategies
            ],
            title="Tasa de éxito por estrategia final",
            max_value=100,
        ),
        "groups": {},
    }
    for group in model_routing["groups"]:
        measured = [row for row in group["rows"] if row["estimated_seconds"] is not None][:15]
        charts["groups"][group["task_type"]] = {
            "time": hbar_chart(
                [
                    BarRow(f"{row['provider']}/{row['model']}", row["estimated_seconds"],
                           f"{row['estimated_seconds']:.1f} s", f"{row['attempts']} invocaciones")
                    for row in measured
                ],
                title=f"Tiempo estimado por modelo — {group['label']}",
            ),
            "success": hbar_chart(
                [
                    BarRow(f"{row['provider']}/{row['model']}", row["observed_rate"] or 0,
                           f"{row['observed_rate']} %",
                           f"{row['successes']} de {row['attempts']}; suavizado {row['success_rate']} %")
                    for row in sorted(group["rows"], key=lambda r: -r["attempts"])[:15]
                ],
                title=f"Tasa de éxito por modelo — {group['label']}",
                max_value=100,
            ),
        }
    if affinity.get("enabled") and affinity.get("groups"):
        charts["affinity"] = hbar_chart(
            [
                BarRow(g["label"], g["remaining"],
                       f"{g['remaining']} de {affinity['catalog_size']}",
                       f"{g['excluded']} apartados")
                for g in affinity["groups"]
            ],
            title="Candidatos que quedan por tipo de tarea",
            max_value=affinity["catalog_size"] or None,
        )
    return charts


# Qué significa cada código de fallo, para alguien que no ha leído el código.
# Los que no estén aquí se muestran con su nombre sin explicación: mejor eso
# que una explicación inventada.
ERROR_EXPLANATIONS = {
    "INVALID_PROVIDER_RESPONSE": (
        "El modelo terminó sin escribir respuesta. En los modelos que «piensan» antes de "
        "contestar casi siempre es que gastaron todo el límite de salida razonando y no "
        "llegaron a la respuesta."
    ),
    "PROMPT_ECHOED": "El modelo devolvió su propio enunciado (o casi entero) en lugar de una respuesta.",
    "DEGENERATE_OUTPUT": "La salida entró en bucle, repitiendo el mismo fragmento una y otra vez.",
    "OUTPUT_TRUNCATED": (
        "La respuesta se cortó al agotar el límite de salida: el modelo no llegó a terminarla. "
        "Si el límite se había recortado para caber en su ventana de contexto, el mensaje lo dice."
    ),
    "MODEL_ERROR": "El servidor del modelo devolvió un error mientras generaba.",
    "CONTEXT_LIMIT_EXCEEDED": "La petición no cabía en la memoria de trabajo (ventana de contexto) del modelo.",
    "MODEL_OUT_OF_MEMORY": "La tarjeta gráfica se quedó sin memoria al cargar o ejecutar el modelo.",
    "VRAM_INSUFFICIENT": "No había memoria de vídeo libre suficiente para ese modelo en ese momento.",
    "MODEL_LOAD_FAILED": "El modelo no llegó a cargarse en memoria.",
    "MODEL_UNAVAILABLE": "El modelo ya no estaba disponible en su proveedor.",
    "PROVIDER_UNAVAILABLE": "El servicio que sirve el modelo no respondía.",
    "TASK_CANCELLED": "Alguien canceló la tarea mientras el modelo trabajaba.",
    "EMBEDDING_INPUT_TOO_LONG": "El texto a convertir en vector era más largo de lo que admite el modelo.",
}

# Fallos que dicen «este modelo no sirve» y alimentan la cuarentena, frente a
# las circunstancias del momento (memoria, red, cancelación).
_MODEL_FAULT_CODES = {"INVALID_PROVIDER_RESPONSE", "PROMPT_ECHOED", "DEGENERATE_OUTPUT"}

# Misma población que app.model_stats.load_model_stats.
_POPULATION = "created_at >= ? AND task_type IS NOT NULL AND excluded_from_learning = 0"


def _cutoff(window_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()


def _percentile(values: list[float], fraction: float) -> float:
    """Percentil por rango más cercano: con pocas muestras no interpola
    valores que nunca ocurrieron."""
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    return ordered[max(0, min(len(ordered), rank) - 1)]


def latency_profile(db: Database, *, window_days: int) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """P50/P95 de latencia y arranques en frío por (modelo, tipo de tarea),
    sobre la misma población que las tablas de selección adaptativa."""
    samples: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in db.query_all(
        "SELECT provider, deployment, model, task_type, latency_ms, was_loaded "
        f"FROM model_invocations WHERE {_POPULATION} AND status = 'completed'",
        (_cutoff(window_days),),
    ):
        key = (
            str(row["provider"]).lower(), str(row["deployment"]).lower(),
            str(row["model"]).lower(), str(row["task_type"]).lower(),
        )
        entry = samples.setdefault(key, {"latencies": [], "cold": 0, "warm": 0})
        if row["latency_ms"] is not None:
            entry["latencies"].append(float(row["latency_ms"]))
        # was_loaded NULL (cloud o filas antiguas) no es ni frío ni caliente.
        if row["was_loaded"] == 0:
            entry["cold"] += 1
        elif row["was_loaded"] == 1:
            entry["warm"] += 1
    profile = {}
    for key, entry in samples.items():
        latencies = entry["latencies"]
        profile[key] = {
            "p50_ms": round(_percentile(latencies, 0.5)) if latencies else None,
            "p95_ms": round(_percentile(latencies, 0.95)) if latencies else None,
            "cold_starts": entry["cold"],
            "warm_starts": entry["warm"],
        }
    return profile


def failure_breakdown(db: Database, *, window_days: int) -> list[dict[str, Any]]:
    """Fallos de la ventana por código, con los modelos que más los dan."""
    by_code: dict[str, dict[str, Any]] = {}
    for row in db.query_all(
        "SELECT COALESCE(error_code, 'SIN_CODIGO') AS code, provider, model, COUNT(*) AS n "
        f"FROM model_invocations WHERE {_POPULATION} AND status = 'failed' "
        "GROUP BY code, provider, model",
        (_cutoff(window_days),),
    ):
        entry = by_code.setdefault(str(row["code"]), {"count": 0, "models": []})
        entry["count"] += int(row["n"])
        entry["models"].append((f"{row['provider']}/{row['model']}", int(row["n"])))
    breakdown = []
    for code, entry in sorted(by_code.items(), key=lambda kv: kv[1]["count"], reverse=True):
        models = sorted(entry["models"], key=lambda item: item[1], reverse=True)
        breakdown.append({
            "code": code,
            "count": entry["count"],
            "model_count": len(models),
            "top_models": models[:3],
            "explanation": ERROR_EXPLANATIONS.get(code),
            "model_fault": code in _MODEL_FAULT_CODES,
        })
    return breakdown


def worst_day_detail(db: Database, day: str) -> dict[str, Any] | None:
    """El modelo y el código que más fallaron un día concreto."""
    row = db.query_one(
        "SELECT provider, model, COALESCE(error_code, 'SIN_CODIGO') AS code, COUNT(*) AS n "
        "FROM model_invocations WHERE substr(created_at, 1, 10) = ? AND status = 'failed' "
        "AND task_type IS NOT NULL AND excluded_from_learning = 0 "
        "GROUP BY provider, model, code ORDER BY n DESC LIMIT 1",
        (day,),
    )
    if row is None:
        return None
    return {"model": f"{row['provider']}/{row['model']}", "code": str(row["code"]), "count": int(row["n"])}


def worst_failure_day(activity: list[dict[str, Any]], *, min_calls: int = 30) -> dict[str, Any] | None:
    """Día con mayor proporción de fallos entre los que tienen volumen: con 5
    llamadas, 2 fallos son un 40 % que no dice nada."""
    rated = [day for day in activity if day["completed"] + day["failed"] >= min_calls]
    if not rated:
        return None
    return max(rated, key=lambda day: day["failed"] / (day["completed"] + day["failed"]))


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:.0f} %" if whole else "—"


def report_narrative(
    *,
    summary: dict[str, Any],
    activity: list[dict[str, Any]],
    buckets: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    model_routing: dict[str, Any],
    failures: list[dict[str, Any]],
    affinity: dict[str, Any],
    router: Any,
    worst_day: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """Frases que cuentan, en lenguaje llano, lo que dicen los datos.

    Se generan con reglas a partir de las mismas cifras de las tablas, no con
    un modelo de lenguaje: así cada frase se puede comprobar contra la tabla
    de al lado y el informe no puede inventarse una conclusión. Solo se afirma
    lo que se puede contar; las interpretaciones van como advertencias."""
    notes: dict[str, list[str]] = {
        "overview": [], "activity": [], "strategies": [], "models": [], "failures": [], "affinity": [],
    }
    invocations = summary["invocations"]
    if invocations:
        notes["overview"].append(
            f"En los últimos {model_routing['stats_window_days']} días el broker hizo "
            f"{invocations} llamadas a modelos que cuentan para su aprendizaje, y el "
            f"{summary['success_rate']} % terminaron sin error técnico."
        )
    if summary["excluded"]:
        notes["overview"].append(
            f"Hubo además {summary['excluded']} llamadas marcadas como «excluidas del aprendizaje» "
            "(pruebas deliberadas, por ejemplo): se ejecutaron, pero no cambian la opinión del "
            "router sobre ningún modelo, y por eso no entran en las cifras principales."
        )
    notes["overview"].append(
        "«Sin error técnico» quiere decir que el modelo devolvió una respuesta utilizable, "
        "no que la respuesta fuera correcta: el broker todavía no puntúa la calidad del contenido."
    )

    busy = [day for day in activity if day["completed"] + day["failed"]]
    if busy:
        peak = max(busy, key=lambda day: day["completed"] + day["failed"])
        notes["activity"].append(
            f"El día de más trabajo fue el {peak['label']}, con {peak['completed'] + peak['failed']} llamadas."
        )
        worst = worst_failure_day(activity)
        if worst is not None:
            total = worst["completed"] + worst["failed"]
            sentence = (
                f"El día con más fallos en proporción fue el {worst['label']}: "
                f"{worst['failed']} de {total} llamadas ({_pct(worst['failed'], total)})."
            )
            # Solo se señala a un modelo si de verdad concentra los fallos.
            if worst_day and worst_day["count"] * 3 >= worst["failed"]:
                sentence += (
                    f" Buena parte vino de un solo modelo, {worst_day['model']}: "
                    f"{worst_day['count']} fallos de tipo {worst_day['code']}."
                )
            notes["activity"].append(sentence)

    if len(strategies) > 1:
        best = max(strategies, key=lambda s: s["success_rate"])
        weakest = min(strategies, key=lambda s: s["success_rate"])
        if best["name"] != weakest["name"]:
            notes["strategies"].append(
                f"La estrategia con más éxito fue {best['name']} ({best['success_rate']} % de "
                f"{best['total']} casos) y la de menos, {weakest['name']} "
                f"({weakest['success_rate']} % de {weakest['total']})."
            )
            notes["strategies"].append(
                "Ojo al compararlas: cada estrategia se usa para tipos de petición distintos, así "
                "que la diferencia puede deberse a lo difícil que es cada tipo y no a la estrategia."
            )
    if not getattr(router, "adaptive_learning", False):
        notes["strategies"].append(
            "El aprendizaje de estrategias está desactivado: el router elige siempre por reglas "
            "fijas, y la columna «Recomendación» enseña lo que haría si estuviera activo."
        )
    overridden = [b for b in buckets if b.get("heuristic_overridden")]
    if overridden:
        notes["strategies"].append(
            f"En {_plural(len(overridden), 'tipo', 'tipos')} de petición la estrategia que corrió no es la que dictan "
            "las reglas, porque las tareas venían marcadas como datos locales o confidenciales y, "
            "sin acceso a internet, no tiene sentido usar el agente investigador."
        )

    for group in model_routing["groups"]:
        usable = [row for row in group["rows"] if row["usable"] and row["estimated_seconds"] is not None]
        if not usable:
            continue
        fastest = min(usable, key=lambda row: row["estimated_seconds"])
        busiest = max(group["rows"], key=lambda row: row["attempts"])
        sentence = (
            f"{group['label']}: el modelo que el router prefiere ahora es "
            f"{fastest['provider']}/{fastest['model']}, con un tiempo esperado de "
            f"{fastest['estimated_seconds']:.0f} s"
        )
        if busiest is not fastest:
            sentence += (
                f"; el más usado ha sido {busiest['provider']}/{busiest['model']} "
                f"({busiest['attempts']} llamadas)"
            )
        notes["models"].append(sentence + ".")
        weak = sorted(
            (
                row for row in group["rows"]
                if row["observed_rate"] is not None and row["observed_rate"] < 80 and row["attempts"] >= 10
            ),
            key=lambda row: row["observed_rate"],
        )
        for row in weak[:3]:
            notes["models"].append(
                f"{group['label']}: {row['provider']}/{row['model']} falla a menudo "
                f"({_plural(row['successes'], 'acierto', 'aciertos')} de {row['attempts']}, {row['observed_rate']} %)."
            )

    total_failures = sum(item["count"] for item in failures)
    if total_failures:
        top = failures[0]
        notes["failures"].append(
            f"Hubo {total_failures} fallos. El más frecuente es {top['code']} "
            f"({top['count']}, el {_pct(top['count'], total_failures)} del total), repartido entre "
            f"{_plural(top['model_count'], 'modelo', 'modelos')}."
        )
        model_faults = sum(item["count"] for item in failures if item["model_fault"])
        notes["failures"].append(
            f"{model_faults} de esos fallos ({_pct(model_faults, total_failures)}) son del propio modelo "
            "(no escribió respuesta, repitió el enunciado o entró en bucle). Son los que cuentan "
            "para la cuarentena: dos seguidos sin ningún acierto entre medias apartan al modelo "
            "durante 24 horas. El resto son circunstancias del momento, como falta de memoria."
        )

    if affinity.get("enabled") and affinity.get("groups"):
        excluded = [g for g in affinity["groups"] if g["excluded"] and not g["ignored"]]
        if excluded:
            parts = ", ".join(f"{g['excluded']} en {g['label'].lower()}" for g in excluded)
            notes["affinity"].append(
                f"De los {affinity['catalog_size']} modelos del catálogo, el filtro aparta {parts}."
            )
    return notes


# --- Interpretación escrita por un modelo del broker -------------------------
#
# Es opcional y va aparte de «Lo más destacado»: aquella prosa sale de reglas y
# se puede comprobar contra las tablas; esta la escribe un modelo y puede
# equivocarse, así que el informe la presenta como opinión firmada por el
# modelo que la escribió, nunca mezclada con los datos.

INTERPRETATION_REQUEST_PREFIX = "routing-report-interpretation"

_INTERPRETATION_INSTRUCTIONS = """\
Eres un analista que explica el funcionamiento de un servicio llamado AI Broker a una persona \
que no es experta. El broker recibe peticiones, elige una estrategia (un solo modelo, varios \
modelos con un árbitro, o un agente con herramientas) y elige qué modelo de lenguaje local la \
atiende según el tiempo que espera que tarde.

Debajo tienes los datos de los últimos días. Escribe una interpretación en español, en 3 a 5 \
párrafos breves y claros, sin tablas ni encabezados:
- Qué está funcionando bien y qué no, en palabras sencillas.
- Qué conviene vigilar, y por qué.
- Termina con 2 o 3 sugerencias concretas, presentadas como sugerencias y no como hechos.

Reglas estrictas:
- Usa solo las cifras que aparecen en los datos; no inventes números, modelos ni causas.
- Cada vez que des un porcentaje, di de qué es (por ejemplo, «la tercera parte de los fallos» \
no es lo mismo que «falla una de cada tres veces»). No atribuyas a un modelo una cifra que en los datos \
se refiere a otra cosa.
- Las sugerencias deben ser cosas que se pueden hacer en el broker: apartar o dejar de usar un \
modelo, cambiar el límite de salida o de razonamiento de un modelo, revisar qué modelos se \
usan para un tipo de tarea, activar el aprendizaje de estrategias o investigar un fallo \
concreto. Los modelos no se pueden reentrenar desde aquí.
- «Éxito» significa que el modelo devolvió una respuesta utilizable, no que fuera correcta: \
no afirmes nada sobre la calidad de las respuestas.
- Si algo no se puede concluir con estos datos, dilo.
- Escribe directamente la interpretación, sin presentarte ni repetir estas instrucciones.

DATOS
"""


def interpretation_prompt(
    *,
    summary: dict[str, Any],
    narrative: dict[str, list[str]],
    model_routing: dict[str, Any],
    failures: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
) -> str:
    """Datos para el modelo, en texto compacto. Parte de las frases de
    «Lo más destacado», que ya están verificadas, y añade lo justo de las
    tablas para que pueda relacionar unas cosas con otras."""
    lines = [_INTERPRETATION_INSTRUCTIONS]
    lines.append(
        f"Resumen: {summary['invocations']} llamadas a modelos, {summary['success_rate']} % sin "
        f"error técnico, {summary['models']} modelos medidos, {summary['excluded']} llamadas "
        "de prueba excluidas del aprendizaje."
    )
    for section in ("overview", "activity", "failures", "strategies", "models", "affinity"):
        for sentence in narrative.get(section, []):
            lines.append(f"- {sentence}")
    if strategies:
        lines.append("Estrategias (casos, % de éxito): " + "; ".join(
            f"{s['name']} {s['total']}, {s['success_rate']} %" for s in strategies
        ))
    for group in model_routing["groups"]:
        rows = sorted(group["rows"], key=lambda row: -row["attempts"])[:6]
        described = []
        for row in rows:
            estimate = f"{row['estimated_seconds']:.0f} s esperados" if row["estimated_seconds"] is not None else "sin estimación"
            described.append(
                f"{row['model']} ({row['attempts']} llamadas, {row['observed_rate']} % éxito, {estimate})"
            )
        lines.append(f"Modelos más usados en {group['label'].lower()}: " + "; ".join(described))
    if failures:
        lines.append("Fallos por tipo: " + "; ".join(
            f"{item['code']} {item['count']}"
            + (f" ({item['explanation']})" if item["explanation"] else "")
            for item in failures[:6]
        ))
    return "\n".join(lines)


def interpretation_request_payload(prompt: str, *, stamp: str, timeout_seconds: int) -> dict[str, Any]:
    """Petición de broker para la interpretación.

    - `local_only`: son datos internos del broker y no salen de la máquina.
    - Excluida del aprendizaje: es trabajo auxiliar del propio broker y no
      debe mover la opinión del router sobre el modelo que la escriba.
    - Prioridad 10 (más urgente que el 100 por defecto): la está esperando
      alguien delante del panel; sin ella haría cola tras todo lo encolado.
    """
    return {
        "idempotency_key": f"{INTERPRETATION_REQUEST_PREFIX}:{stamp}",
        "request_id": f"{INTERPRETATION_REQUEST_PREFIX}:{stamp}",
        "content": {"prompt": prompt},
        "output": {"format": "markdown", "language": "es"},
        "generation": {"temperature": 0.3, "max_output_tokens": 2000},
        "model_requirements": {"cloud_allowed": False},
        "execution": {"strategy": "single", "timeout_seconds": timeout_seconds},
        "risk": {"data_classification": "local_only"},
        "priority": 10,
        "exclude_from_model_learning": True,
    }


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    """Cifras normalizadas: «2.182» y «2182» son la misma; «51,3» y «51.3»
    también. Un punto seguido de tres cifras se toma como separador de miles."""
    found = set()
    for raw in _NUMBER.findall(text):
        value = re.sub(r"\.(?=\d{3}(?:\D|$))", "", raw).replace(",", ".")
        found.add(value.rstrip("0").rstrip(".") if "." in value else value)
    return found


def unsupported_numbers(text: str, data: str) -> list[str]:
    """Cifras del texto del modelo que no aparecen en los datos que recibió.

    No demuestra que el texto esté mal (puede haber sumado dos cifras), pero
    sí que esa cifra no se puede comprobar en el informe, y eso hay que
    decirlo. Se ignoran los números de una cifra: «2 o 3 sugerencias» no es
    un dato."""
    available = _numbers(data)
    return sorted(
        (value for value in _numbers(text) if value not in available and len(value.replace(".", "")) > 1),
        key=lambda value: float(value),
    )


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BULLET = re.compile(r"^[-*•]\s+")


def _inline(text: str) -> str:
    """Escapa y deja pasar solo las negritas **así**."""
    return _BOLD.sub(r"<strong>\1</strong>", escape(text))


def interpretation_html(text: str) -> Markup:
    """Markdown mínimo y seguro: párrafos, listas con guion o asterisco y
    negritas. Todo lo demás se escapa; el texto viene de un modelo y no puede
    meter HTML en el informe."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    parts = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        # Encabezados que el modelo ponga pese a la instrucción: se quedan
        # como texto en negrita en vez de romper la jerarquía del informe.
        lines = [re.sub(r"^#{1,6}\s*", "", line) for line in lines]
        if all(_BULLET.match(line) for line in lines):
            items = "".join(f"<li>{_inline(_BULLET.sub('', line))}</li>" for line in lines)
            parts.append(f"<ul>{items}</ul>")
        else:
            paragraph = " ".join(lines)
            parts.append(f"<p>{_inline(paragraph)}</p>")
    return Markup("".join(parts))


# --- PDF ---------------------------------------------------------------------


class PdfUnavailable(RuntimeError):
    """No se puede generar el PDF en esta máquina; el mensaje dice qué falta."""


def render_pdf(html: str) -> bytes:
    """Imprime el informe a PDF con Chromium sin interfaz (Playwright).

    API síncrona a propósito: se llama desde un hilo del threadpool, donde
    Playwright crea su propio bucle de eventos. Así no depende de que el bucle
    de uvicorn en Windows admita subprocesos. Los estilos @media print del
    informe se aplican igual que al imprimir desde el navegador."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise PdfUnavailable(
            "Falta Playwright en el entorno del broker: pip install playwright y "
            "python -m playwright install chromium-headless-shell"
        ) from error
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                return page.pdf(
                    format="A4",
                    print_background=True,
                    margin={"top": "14mm", "bottom": "14mm", "left": "14mm", "right": "14mm"},
                )
            finally:
                browser.close()
    except PlaywrightError as error:
        raise PdfUnavailable(
            f"Chromium no ha podido generar el PDF ({error.message.splitlines()[0]}). "
            "Si falta el navegador: python -m playwright install chromium-headless-shell"
        ) from error
