"""Representative live calibration through the broker's API, no provider mocks.

Run from the repository: python -m scripts.benchmark_system1 --repeats 2
Writes synthetic inputs, normalized outputs and measured metrics to docs.
Production DB, routing policy and provider configuration are never rewritten.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import ServerConfig, System1ThresholdConfig, load_config
from app.main import create_app
from app.schemas import TaskCreateRequest

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "docs" / "Modelos system 1"


def validation_cases():
    cases = []
    for name, prompt, expected in [
        ("simple_fact", "¿Cuál es la capital de Francia? Responde con una palabra.", "simple"),
        ("simple_translation", "Traduce al inglés: buenos días.", "simple"),
        ("simple_format", "Convierte esta lista a JSON: rojo, azul, verde.", "simple"),
        ("medium_code", "Escribe una función Python que valide un CSV, elimine duplicados y calcule totales por categoría.", "medium"),
        ("medium_summary", "Resume esta reunión y enumera responsables y plazos: Ana entrega el informe el lunes; Luis valida el miércoles; revisión conjunta el viernes.", "medium"),
        ("medium_plan", "Organiza una mudanza en cuatro semanas: inventario, embalaje, transporte y comprobación final. Da dependencias y un calendario.", "medium"),
        ("complex_proof", "Demuestra que todo grupo finito de orden primo es cíclico y justifica cada paso con el teorema de Lagrange.", "complex"),
        ("complex_security", "Diseña una migración de autorización multi-tenant que evite escaladas de privilegios y ataques de confused deputy; incluye invariantes, pruebas y estrategia de rollback.", "complex"),
        ("complex_architecture", "Diseña un consenso distribuido tolerante a particiones, demuestra sus límites de seguridad y vivacidad y analiza formalmente el comportamiento ante fallos bizantinos.", "complex"),
    ]:
        cases.append({"id": name, "expected": expected, "request": {
            "use_case": "semantic_routing", "decision_type": "choice", "options": ["simple", "medium", "complex"],
            "criteria": {"simple": "lookup, short translation, formatting or one-step answer",
                         "medium": "several ordinary steps, routine coding or summarization",
                         "complex": "proofs, specialist knowledge, architecture, security or long multi-step reasoning"},
            "input": {"request": prompt},
        }})
    for name, state, expected in [
        ("goal_done", {"goal": "Create a CSV report and verify its totals", "report_exists": True,
                       "totals_verified": True, "remaining_steps": []}, True),
        ("goal_unverified", {"goal": "Create a CSV report and verify its totals", "report_exists": True,
                             "totals_verified": False, "remaining_steps": ["verify totals"]}, False),
        ("goal_missing", {"goal": "Implement and test a function", "implemented": False,
                          "tests_passed": False, "remaining_steps": ["implement", "test"]}, False),
        ("goal_done_code", {"goal": "Implement and test a function", "implemented": True,
                            "tests_passed": True, "remaining_steps": []}, True),
    ]:
        cases.append({"id": name, "expected": expected, "request": {
            "use_case": "goal_completion", "decision_type": "binary", "input": state,
        }})
    cases.append({"id": "score_neutral", "expected": 0.0, "request": {
        "use_case": "ranking", "decision_type": "score",
        "instructions": "How frustrated does the customer sound in message?",
        "input": {"message": "Hola, me gustaría saber el horario de apertura. Gracias."},
        "rubric": ["calm and neutral", "concerned but civil", "clearly annoyed", "very angry or using strong language"],
    }})
    return cases


def summarize(rows):
    classes = sorted({str(row["expected"]) for row in rows})
    per_class = {}
    for label in classes:
        tp = sum(str(row["expected"]) == label and str(row["response"]["decision"]) == label for row in rows)
        fp = sum(str(row["expected"]) != label and str(row["response"]["decision"]) == label for row in rows)
        fn = sum(str(row["expected"]) == label and str(row["response"]["decision"]) != label for row in rows)
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        per_class[label] = {"precision": precision, "recall": recall,
                            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
                            "false_positives": fp, "false_negatives": fn, "support": tp + fn}
    latency = sorted(row["response"]["latency_ms"] for row in rows)
    groups = {}
    for row in rows:
        groups.setdefault(row["id"], []).append(row["response"]["decision"])
    accepted = [row for row in rows if row["production_gate_accepted"]]
    route_rows = [row for row in rows if row["request"]["use_case"] == "semantic_routing"]
    down = [row for row in route_rows if row["production_gate_accepted"] and row["response"]["decision"] == "simple"]
    return {
        "calls": len(rows), "accuracy": sum(row["expected"] == row["response"]["decision"] for row in rows) / len(rows),
        "per_class": per_class, "latency_ms_p50": statistics.median(latency),
        "latency_ms_p95": latency[min(len(latency) - 1, int(len(latency) * 0.95))],
        "production_fallback_rate": 1 - len(accepted) / len(rows),
        "production_accepted_accuracy": sum(row["expected"] == row["response"]["decision"] for row in accepted) / len(accepted) if accepted else None,
        "stability": sum(all(value == predictions[0] for value in predictions) for predictions in groups.values()) / len(groups),
        "simple_downrouting_precision": sum(row["expected"] == "simple" for row in down) / len(down) if down else None,
        "unsafe_downrouting": sum(row["production_gate_accepted"] and row["response"]["decision"] in ("simple", "medium")
                                  and ["simple", "medium", "complex"].index(row["response"]["decision"])
                                  < ["simple", "medium", "complex"].index(row["expected"]) for row in route_rows),
        "errors": dict(Counter(row["response"]["reason_code"] for row in rows if not row["response"]["accepted"])),
        "classifier_tokens_input": sum(attempt.get("tokens_input") or 0 for row in rows for attempt in row["response"]["attempts"])
        if any(attempt.get("tokens_input") is not None for row in rows for attempt in row["response"]["attempts"]) else None,
        "classifier_tokens_output": sum(attempt.get("tokens_output") or 0 for row in rows for attempt in row["response"]["attempts"])
        if any(attempt.get("tokens_output") is not None for row in rows for attempt in row["response"]["attempts"]) else None,
        "savings": {"tokens": None, "time": None, "generation_calls": None,
                    "reason": "Shadow mode leaves actual routing unchanged; no measured generation comparison yet."},
        "confidence_is_calibrated": False, "brier_ece": None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", default="benchmark_system1.json")
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args()
    settings = load_config(ROOT / "broker_config.yaml").model_copy(deep=True)
    production_policy = settings.system1.model_dump()
    if args.timeout is not None:
        settings.system1.timeout_seconds = args.timeout
    settings.server = ServerConfig(host="127.0.0.1", admin_token_env="SYSTEM1_BENCHMARK_TOKEN")
    os.environ["SYSTEM1_BENCHMARK_TOKEN"] = "local-benchmark-test-token"
    settings.persistence.database = str(ROOT / ".local" / "system1-live" / "broker.db")
    settings.logging.directory = str(ROOT / ".local" / "system1-live" / "logs")
    settings.logging.console_enabled = False
    settings.processing.auto_dispatch = False
    settings.ingestion.enabled = False
    settings.providers.custom = []
    settings.providers.deepseek.enabled = False
    settings.model_enrichment.enabled = False
    cases = validation_cases()
    DESTINATION.mkdir(parents=True, exist_ok=True)
    (DESTINATION / "validation_system1.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    app = create_app(settings)
    rows = []
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "repeats": args.repeats,
              "cases": len(cases), "production_policy": production_policy,
              "measurement_timeout_seconds": settings.system1.timeout_seconds, "rows": rows,
              "transport": "POST /api/v1/system1/judge through FastAPI TestClient with real adapters"}
    report_path = DESTINATION / args.output
    headers = {"X-Admin-Token": "local-benchmark-test-token"}
    settings.system1.thresholds["benchmark_raw"] = System1ThresholdConfig(confidence=0, min_margin=0)
    with TestClient(app) as client:
        for provider in ["laya_mcp", "ollama_system1"]:
            settings.system1.provider_priority = [provider]
            for repeat in range(args.repeats):
                for case in cases:
                    payload = dict(case["request"])
                    if payload["use_case"] == "semantic_routing":
                        payload["instructions"] = settings.system1.routing.instructions
                    payload["threshold_profile"] = "benchmark_raw"
                    response = client.post("/api/v1/system1/judge", json=payload, headers=headers)
                    response.raise_for_status()
                    result = response.json()
                    profile = settings.system1.thresholds[payload["use_case"]]
                    margin = (result["confidence"] or 0) - max((item["confidence"] for item in result["alternatives"]), default=0)
                    accepted = result["accepted"] and result["confidence"] >= profile.confidence and (
                        payload["decision_type"] == "binary" or margin >= profile.min_margin)
                    rows.append({"id": case["id"], "provider": provider, "repeat": repeat,
                                 "expected": case["expected"], "request": payload, "response": result,
                                 "production_gate_accepted": accepted, "top1_top2_margin": margin})
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{provider} {repeat + 1}/{args.repeats} {case['id']}: expected={case['expected']} "
                          f"decision={result['decision']} confidence={result['confidence']} "
                          f"gate={accepted} error={result['reason_code']} latency={result['latency_ms']}ms", flush=True)
        # Real fallback: deliberately unavailable primary server, live nimble secondary.
        settings.system1.provider_priority = ["laya_mcp", "ollama_system1"]
        original_server = settings.system1.laya.server_id
        settings.system1.laya.server_id = "benchmark-unavailable"
        report["live_provider_fallback"] = client.post("/api/v1/system1/judge", json={
            "use_case": "ranking", "decision_type": "binary", "input": {"message": "Solicito un reembolso."},
            "instructions": "Does message request money back?",
        }, headers=headers).json()
        settings.system1.laya.server_id = original_server
        settings.system1.provider_priority = production_policy["provider_priority"]
        report["configured_primary_example"] = client.post("/api/v1/system1/judge", json={
            "use_case": "ranking", "decision_type": "binary", "input": {"message": "Solicito un reembolso."},
            "instructions": "Does message request money back?",
        }, headers=headers).json()
        original_model = settings.system1.ollama.model
        settings.system1.ollama.model = "benchmark-unavailable-model"
        settings.system1.provider_priority = ["ollama_system1", "laya_mcp"]
        report["live_nimble_to_laya_fallback"] = client.post("/api/v1/system1/judge", json={
            "use_case": "ranking", "decision_type": "binary", "input": {"message": "Solicito un reembolso."},
            "instructions": "Does message request money back?",
        }, headers=headers).json()
        settings.system1.ollama.model = original_model
        settings.system1.provider_priority = production_policy["provider_priority"]
        report["routing_examples"] = []
        for expected, case_id in [("simple", "simple_fact"), ("medium", "medium_code"), ("complex", "complex_proof")]:
            case = next(item for item in cases if item["id"] == case_id)
            task = TaskCreateRequest.model_validate({
                "idempotency_key": f"live-routing-{expected}", "content": {"prompt": case["request"]["input"]["request"]},
                "execution": {"strategy": "single"},
                "model_requirements": {"allowed_providers": ["ollama"], "cloud_allowed": False},
            })
            for shadow in [True, False]:
                settings.system1.routing.shadow_mode = shadow
                selected = client.portal.call(app.state.provider.select, task, 1, ["single"])
                event = next((item for item in reversed(app.state.system1.metrics()["recent"])
                              if item["event"] == "system1.routing"), None)
                report["routing_examples"].append({"expected_tier": expected, "shadow_mode": shadow,
                                                   "selected": selected[0].model_dump(), "telemetry": event})
        settings.system1.routing.shadow_mode = production_policy["routing"]["shadow_mode"]
        report["metrics"] = client.get("/api/v1/system1/metrics", headers=headers).json()
    report["summary"] = {provider: summarize([row for row in rows if row["provider"] == provider])
                         for provider in ["laya_mcp", "ollama_system1"]}
    for provider, summary in report["summary"].items():
        summary["by_use_case"] = {
            use_case: summarize([row for row in rows if row["provider"] == provider and row["request"]["use_case"] == use_case])
            for use_case in {row["request"]["use_case"] for row in rows}
        }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2), flush=True)


if __name__ == "__main__":
    main()
