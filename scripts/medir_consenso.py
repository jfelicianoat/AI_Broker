"""¿Acierta el consenso (mixture_of_agents) más que el mejor de sus modelos solo?

Para cada pregunta de un conjunto de referencia con respuesta única
(benchmarks/consenso/oro_v1.json):

1. lanza la pregunta como `mixture_of_agents` con selección automática;
2. lee en /invocations qué modelos fueron proponentes;
3. lanza la misma pregunta a cada uno de esos proponentes por separado
   (`single` + `target_model`, sin sustitución).

Así cada pregunta compara el consenso con los mismos modelos que lo formaron,
no con un modelo cualquiera. Todas las tareas van con
`exclude_from_model_learning: true`: medir no debe cambiar la selección del
router (regla 6 de la auditoría del 12-nov-2026). Por defecto, solo modelos
locales (`cloud_allowed: false`): la medición no cuesta dinero.

Uso (en el PC del broker):

    .venv\\Scripts\\python scripts\\medir_consenso.py --limite 6
    .venv\\Scripts\\python scripts\\medir_consenso.py --salida docs\\CONSENSO_MEDICION.json

El token se lee de AI_BROKER_ADMIN_TOKEN o del llavero (`ai-broker` /
`session_admin_token`), igual que el runner. Sale con código 0 aunque el
consenso pierda: es una medición, no una puerta.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLD = ROOT / "benchmarks" / "consenso" / "oro_v1.json"
DEFAULT_CA = ROOT / "certs" / "broker-ca.crt"

# Roles de invocación que proponen una respuesta en un consenso. El árbitro,
# el juez y el refinador de la segunda ronda no cuentan: no son "un modelo
# respondiendo solo".
PROPOSER_ROLES = frozenset({"proposer", "generalist", "specialist", "skeptic", "analyst", "reviewer"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out"})

# Hasta el final de línea o hasta el siguiente "RESPUESTA:": hay modelos que
# lo repiten en la misma línea ("2. RESPUESTA: 2") y manda el último.
_ANSWER_RE = re.compile(r"RESPUESTA\s*[:：]\s*([^\n]*?)\s*(?=RESPUESTA\s*[:：]|\n|$)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


# ------------------------------------------------------------ puntuación


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()


def extract_answer(text: str | None) -> str | None:
    """Valor de la última línea `RESPUESTA: …`; None si el modelo no la dio."""
    if not text:
        return None
    matches = _ANSWER_RE.findall(text)
    if not matches:
        return None
    value = matches[-1].strip()
    value = re.sub(r"[*_`]+", "", value)  # negritas de markdown
    return value.strip().rstrip(".").strip() or None


def is_correct(answer: str | None, expected: str, kind: str) -> bool:
    if answer is None:
        return False
    got, want = _fold(answer), _fold(expected)
    if kind == "numero":
        if ":" in want:  # horas: 17:25 frente a "17:25 h"
            return want in got.replace(" ", "")
        numbers = _NUMBER_RE.findall(got)
        if not numbers:
            return False
        try:
            return abs(float(numbers[0].replace(",", ".")) - float(want.replace(",", "."))) < 1e-9
        except ValueError:
            return False
    words = re.findall(r"[a-z0-9]+", got)
    return want in words or got == want


# ------------------------------------------------------------ agregado


@dataclass
class ItemResult:
    item_id: str
    expected: str
    moa_answer: str | None = None
    moa_correct: bool = False
    moa_error: str | None = None
    moa_synthesized: bool | None = None
    singles: dict[str, dict[str, Any]] = field(default_factory=dict)  # modelo -> {answer, correct, error}


def summarize(results: Iterable[ItemResult]) -> dict[str, Any]:
    """Aciertos del consenso frente a cada modelo y frente al mejor de ellos.

    "Mejor modelo solo" se define dos veces, porque las dos preguntas son
    distintas: el modelo con más aciertos en todo el conjunto (lo que podrías
    elegir de antemano) y el oráculo por pregunta (si cualquiera de los
    proponentes acertó: el techo que ningún árbitro puede superar eligiendo).
    """
    results = list(results)
    n = len(results)
    moa_ok = sum(r.moa_correct for r in results)
    per_model: dict[str, dict[str, int]] = {}
    for r in results:
        for model, single in r.singles.items():
            stats = per_model.setdefault(model, {"preguntas": 0, "aciertos": 0, "fallos_de_ejecucion": 0})
            stats["preguntas"] += 1
            stats["aciertos"] += int(bool(single.get("correct")))
            stats["fallos_de_ejecucion"] += int(single.get("error") is not None)
    best_model = None
    if per_model:
        best_model = max(per_model, key=lambda m: (per_model[m]["aciertos"] / per_model[m]["preguntas"], m))
    # Comparación pregunta a pregunta contra el mejor modelo, solo donde participó.
    gana = pierde = empata = 0
    oracle_ok = 0
    for r in results:
        oracle_ok += int(any(s.get("correct") for s in r.singles.values()))
        if best_model and best_model in r.singles:
            best = bool(r.singles[best_model].get("correct"))
            if r.moa_correct and not best:
                gana += 1
            elif best and not r.moa_correct:
                pierde += 1
            else:
                empata += 1
    best_stats = per_model.get(best_model or "", {})
    return {
        "preguntas": n,
        "consenso": {"aciertos": moa_ok, "tasa": round(moa_ok / n, 3) if n else None},
        "por_modelo": {
            m: {**s, "tasa": round(s["aciertos"] / s["preguntas"], 3)} for m, s in sorted(per_model.items())
        },
        "mejor_modelo_solo": {
            "modelo": best_model,
            "tasa": round(best_stats["aciertos"] / best_stats["preguntas"], 3) if best_stats else None,
            "frente_al_consenso": {"consenso_gana": gana, "consenso_pierde": pierde, "igual": empata},
        },
        "oraculo_por_pregunta": {"aciertos": oracle_ok, "tasa": round(oracle_ok / n, 3) if n else None},
        "consensos_degradados": sum(1 for r in results if r.moa_synthesized is False),
        "aviso": (
            "Muestra pequeña: con menos de ~50 preguntas, una diferencia de 1 o 2 aciertos "
            "no distingue una estrategia de otra."
        ),
    }


# ------------------------------------------------------------ broker


class BrokerClient:
    def __init__(self, base_url: str, token: str, ca: Path | None, *, poll_seconds: float = 3.0,
                 http: Any = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if http is None:
            import httpx

            http = httpx.Client(base_url=base_url, verify=str(ca) if ca else True, timeout=60,
                                headers={"X-Admin-Token": token})
        self.http = http
        self.poll_seconds = poll_seconds
        self.sleep = sleep

    def submit(self, payload: dict[str, Any]) -> str:
        response = self.http.post("/api/v1/tasks", json=payload)
        if response.status_code not in (200, 201, 202):
            raise RuntimeError(f"POST /api/v1/tasks → {response.status_code}: {response.text[:300]}")
        return str(response.json()["task_id"])

    def wait(self, task_id: str, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            task = self.http.get(f"/api/v1/tasks/{task_id}").json()
            if task.get("status") in TERMINAL:
                return dict(task)
            if time.monotonic() > deadline:
                self.http.delete(f"/api/v1/tasks/{task_id}")
                return {"status": "timed_out", "error": {"code": "MEASUREMENT_TIMEOUT"}}
            self.sleep(self.poll_seconds)

    def proposers(self, task_id: str) -> list[dict[str, str]]:
        items = self.http.get(f"/api/v1/tasks/{task_id}/invocations").json().get("items", [])
        seen: dict[tuple[str, str, str], dict[str, str]] = {}
        for item in items:
            model = item.get("model") or {}
            if item.get("role") in PROPOSER_ROLES and item.get("status") == "completed":
                key = (str(model.get("provider")), str(model.get("deployment")), str(model.get("model")))
                seen.setdefault(key, {"provider": key[0], "deployment": key[1], "model": key[2]})
        return list(seen.values())


def _payload(question: str, key: str, cloud: bool, *, moa: dict[str, Any] | None = None,
             target: dict[str, str] | None = None) -> dict[str, Any]:
    requirements: dict[str, Any] = {"cloud_allowed": cloud}
    if target is not None:
        requirements.update(target_model=target, fallback_allowed=False)
    payload: dict[str, Any] = {
        "idempotency_key": key,
        "content": {"prompt": question, "metadata": {"origin": "medir_consenso"}},
        "output": {"format": "markdown", "language": "es"},
        "generation": {"temperature": 0.0, "seed": 7, "max_output_tokens": 3000},
        "model_requirements": requirements,
        "exclude_from_model_learning": True,
    }
    if moa is not None:
        payload["execution"] = moa
    return payload


def _answer_of(task: dict[str, Any]) -> tuple[str | None, str | None]:
    if task.get("status") != "completed":
        error = task.get("error") or {}
        return None, str(error.get("code") or task.get("status"))
    result = task.get("result") or {}
    return result.get("assistant_content") or result.get("result_markdown"), None


def run(client: BrokerClient, gold: dict[str, Any], *, limit: int | None, proposers: int, cloud: bool,
        timeout_seconds: float, log: Callable[[str], None] = print) -> list[ItemResult]:
    run_id = uuid.uuid4().hex[:8]
    fmt = gold["instruccion_formato"]
    results: list[ItemResult] = []
    for item in gold["items"][:limit]:
        question = f"{item['pregunta']}\n\n{fmt}"
        res = ItemResult(item["id"], item["respuesta"])
        moa = {"strategy": "mixture_of_agents", "max_proposers": proposers,
               "selection": {"mode": "auto", "proposer_count": proposers}}
        task_id = client.submit(_payload(question, f"medir-consenso:{run_id}:{item['id']}:moa", cloud, moa=moa))
        task = client.wait(task_id, timeout_seconds)
        text, res.moa_error = _answer_of(task)
        res.moa_answer = extract_answer(text)
        res.moa_correct = is_correct(res.moa_answer, item["respuesta"], item["tipo"])
        res.moa_synthesized = ((task.get("result") or {}).get("consensus") or {}).get("synthesized")
        for index, target in enumerate(client.proposers(task_id)):
            single_id = client.submit(_payload(
                question, f"medir-consenso:{run_id}:{item['id']}:single{index}", cloud, target=target))
            text, error = _answer_of(client.wait(single_id, timeout_seconds))
            answer = extract_answer(text)
            res.singles[f"{target['provider']}/{target['model']}"] = {
                "answer": answer, "correct": is_correct(answer, item["respuesta"], item["tipo"]), "error": error,
            }
        log(f"{item['id']}: consenso={'OK' if res.moa_correct else 'mal'} ({res.moa_answer!r}) · "
            + ", ".join(f"{m}={'OK' if s['correct'] else 'mal'}" for m, s in res.singles.items()))
        results.append(res)
    return results


def _token() -> str:
    token = os.environ.get("AI_BROKER_ADMIN_TOKEN")
    if token:
        return token
    import keyring

    token = keyring.get_password("ai-broker", "session_admin_token")
    if not token:
        raise SystemExit("Sin token: define AI_BROKER_ADMIN_TOKEN o arranca el broker (publica el del llavero).")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--broker-url", default="https://127.0.0.1:8765")
    parser.add_argument("--ca", type=Path, default=DEFAULT_CA)
    parser.add_argument("--oro", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--limite", type=int, default=None, help="solo las N primeras preguntas")
    parser.add_argument("--proponentes", type=int, default=3)
    parser.add_argument("--permitir-nube", action="store_true", help="deja que el router use modelos cloud")
    parser.add_argument("--timeout", type=float, default=900.0, help="segundos por tarea")
    parser.add_argument("--salida", type=Path, default=None, help="JSON con el detalle y el resumen")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    gold = json.loads(args.oro.read_text(encoding="utf-8"))
    client = BrokerClient(args.broker_url, _token(), args.ca if args.ca.exists() else None)
    started = time.monotonic()
    results = run(client, gold, limit=args.limite, proposers=args.proponentes, cloud=args.permitir_nube,
                  timeout_seconds=args.timeout)
    summary = summarize(results)
    summary["duracion_s"] = round(time.monotonic() - started, 1)
    summary["conjunto"] = gold["version"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.salida:
        detail = [r.__dict__ for r in results]
        args.salida.write_text(json.dumps({"resumen": summary, "detalle": detail}, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
