"""Medidor del consenso frente a sus modelos solos (scripts/medir_consenso.py)."""
from __future__ import annotations

import calendar
import datetime as dt
import json
from typing import Any

import pytest

from scripts import medir_consenso as mc

GOLD = json.loads(mc.DEFAULT_GOLD.read_text(encoding="utf-8"))


# ------------------------------------------------- el conjunto de referencia


def test_gold_answers_are_right_where_python_can_check_them() -> None:
    answers = {item["id"]: item["respuesta"] for item in GOLD["items"]}
    llegada = dt.datetime(2026, 1, 1, 14, 35) + dt.timedelta(hours=2, minutes=50)
    assert answers["C01"] == llegada.strftime("%H:%M")
    assert answers["C02"] == str("ferrocarril".count("r"))
    assert answers["C05"] == ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"][
        dt.date(2000, 1, 1).weekday()]
    primes = [n for n in range(2, 30) if all(n % d for d in range(2, n))]
    assert answers["C06"] == str(sum(primes))
    assert answers["C09"] == str(17 * 23 - 144 // 12)
    dias = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
    assert answers["C10"] == dias[(2 + 100) % 7]
    assert answers["C11"] == str(7 * 24 * 60)
    assert answers["C12"] == str(round(1.2 * 0.8 * 100))
    assert answers["C13"] == sorted(["pera", "manzana", "kiwi", "uva", "banana"])[2]
    assert answers["C14"] == str(calendar.monthrange(2100, 2)[1])
    assert answers["C15"] == "murcielago"[::-1]
    assert answers["C16"] == str(next(x for x in range(-20, 21) for y in range(-20, 21)
                                      if x + 2 * y == 11 and 3 * x - y == 5))
    assert answers["C17"] == str(2 ** 20 % 7)


def test_gold_ids_are_unique_and_kinds_known() -> None:
    ids = [item["id"] for item in GOLD["items"]]
    assert len(ids) == len(set(ids))
    assert {item["tipo"] for item in GOLD["items"]} <= {"numero", "texto"}


# ------------------------------------------------------------ puntuación


@pytest.mark.parametrize(("text", "expected"), [
    ("cuentas...\nRESPUESTA: 5", "5"),
    ("RESPUESTA: 3\nrevisando...\nRespuesta: **4**.", "4"),
    ("tanteo: 2. RESPUESTA: 2", "2"),
    ("RESPUESTA: 17:35. RESPUESTA: 17:25", "17:25"),
    ("RESPUESTA: sábado. RESPUESTA: sábado.", "sábado"),
    ("sin línea final", None),
    ("", None),
    (None, None),
])
def test_extract_answer(text: str | None, expected: str | None) -> None:
    assert mc.extract_answer(text) == expected


@pytest.mark.parametrize(("answer", "expected", "kind", "ok"), [
    ("5 céntimos", "5", "numero", True),
    ("0,05", "5", "numero", False),  # 0,05 € no es «5 céntimos»: la pregunta pide céntimos
    ("10080", "10080", "numero", True),
    ("17:25 h", "17:25", "numero", True),
    ("17:35", "17:25", "numero", False),
    ("Sábado", "sabado", "texto", True),
    ("el sábado 1", "sabado", "texto", True),
    ("domingo", "sabado", "texto", False),
    (None, "3", "numero", False),
    ("ninguno", "3", "numero", False),
])
def test_is_correct(answer: str | None, expected: str, kind: str, ok: bool) -> None:
    assert mc.is_correct(answer, expected, kind) is ok


# ------------------------------------------------------------ agregado


def _r(item: str, moa: bool, **singles: bool) -> mc.ItemResult:
    res = mc.ItemResult(item, "x", moa_correct=moa, moa_synthesized=True)
    res.singles = {m: {"answer": None, "correct": ok, "error": None} for m, ok in singles.items()}
    return res


def test_summary_compares_with_best_model_and_oracle() -> None:
    results = [
        _r("1", True, a=True, b=False),
        _r("2", True, a=False, b=False),   # el consenso acierta donde nadie solo
        _r("3", False, a=True, b=True),    # el consenso pierde contra el mejor
        _r("4", True, a=True, b=False),
    ]
    summary = mc.summarize(results)
    assert summary["consenso"] == {"aciertos": 3, "tasa": 0.75}
    assert summary["mejor_modelo_solo"]["modelo"] == "a"
    assert summary["mejor_modelo_solo"]["tasa"] == 0.75
    assert summary["mejor_modelo_solo"]["frente_al_consenso"] == {"consenso_gana": 1, "consenso_pierde": 1, "igual": 2}
    assert summary["oraculo_por_pregunta"] == {"aciertos": 3, "tasa": 0.75}
    assert summary["por_modelo"]["b"]["tasa"] == 0.25


def test_summary_empty_and_degraded() -> None:
    assert mc.summarize([])["consenso"]["tasa"] is None
    degraded = _r("1", True, a=True)
    degraded.moa_synthesized = False
    assert mc.summarize([degraded])["consensos_degradados"] == 1


# ------------------------------------------------------- recorrido completo


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


class FakeBroker:
    """Broker simulado: el consenso acierta, el modelo A acierta y B falla."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.posted: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.polls: dict[str, int] = {}

    def post(self, path: str, json: dict[str, Any]) -> FakeResponse:
        assert path == "/api/v1/tasks"
        self.posted.append(json)
        task_id = f"task_{len(self.posted)}"
        target = (json["model_requirements"].get("target_model") or {}).get("model")
        answer = {"A": "RESPUESTA: 17:25", "B": "RESPUESTA: 17:35", None: "RESPUESTA: 17:25"}[target]
        self.tasks[task_id] = {"status": "completed", "result": {"assistant_content": answer,
                                                                  "consensus": {"synthesized": True}}}
        return FakeResponse(202, {"task_id": task_id})

    def get(self, path: str) -> FakeResponse:
        if path.endswith("/invocations"):
            return FakeResponse(200, {"items": [
                {"role": "proposer", "status": "completed", "model": {"provider": "ollama", "deployment": "local", "model": "A"}},
                {"role": "proposer", "status": "completed", "model": {"provider": "ollama", "deployment": "local", "model": "B"}},
                {"role": "proposer", "status": "failed", "model": {"provider": "ollama", "deployment": "local", "model": "C"}},
                {"role": "arbiter", "status": "completed", "model": {"provider": "ollama", "deployment": "local", "model": "Z"}},
            ]})
        task_id = path.rsplit("/", 1)[1]
        self.polls[task_id] = self.polls.get(task_id, 0) + 1
        if self.polls[task_id] == 1:
            return FakeResponse(200, {"status": "proposing"})
        return FakeResponse(200, self.tasks[task_id])

    def delete(self, path: str) -> FakeResponse:
        self.deleted.append(path)
        return FakeResponse(200, {})


def test_run_pits_the_consensus_against_its_own_proposers() -> None:
    broker = FakeBroker()
    client = mc.BrokerClient("x", "t", None, http=broker, sleep=lambda _: None)
    results = mc.run(client, GOLD, limit=1, proposers=3, cloud=False, timeout_seconds=60, log=lambda _: None)
    assert len(broker.posted) == 3  # el consenso + los dos proponentes que respondieron
    moa, *singles = broker.posted
    assert moa["execution"]["strategy"] == "mixture_of_agents"
    assert all(p["exclude_from_model_learning"] is True for p in broker.posted)
    assert all(p["model_requirements"]["cloud_allowed"] is False for p in broker.posted)
    assert [s["model_requirements"]["target_model"]["model"] for s in singles] == ["A", "B"]
    assert all(s["model_requirements"]["fallback_allowed"] is False for s in singles)
    assert len({p["idempotency_key"] for p in broker.posted}) == 3
    res = results[0]
    assert res.moa_correct and res.singles["ollama/A"]["correct"] and not res.singles["ollama/B"]["correct"]


def test_wait_cancels_a_task_past_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = FakeBroker()
    broker.tasks["task_9"] = {"status": "proposing"}
    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(mc.time, "monotonic", lambda: next(clock))
    client = mc.BrokerClient("x", "t", None, http=broker, sleep=lambda _: None)
    task = client.wait("task_9", timeout_seconds=10)
    assert task["status"] == "timed_out"
    assert broker.deleted == ["/api/v1/tasks/task_9"]


def test_failed_task_is_scored_as_wrong_with_its_code() -> None:
    assert mc._answer_of({"status": "failed", "error": {"code": "PROVIDER_UNAVAILABLE"}}) == (None, "PROVIDER_UNAVAILABLE")
