"""Meta-router: clasificación heurística y flujo end-to-end de strategy: auto."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import (
    BrokerConfig,
    PersistenceConfig,
    ProcessingConfig,
    StrategyRouterConfig,
)
from app.main import create_app
from app.schemas import TaskCreateRequest
from app.strategy_router import classify_request


def _request(prompt: str, **overrides) -> TaskCreateRequest:
    payload = {"idempotency_key": "router:test", "content": {"prompt": prompt}}
    payload.update(overrides)
    return TaskCreateRequest.model_validate(payload)


def test_classifier_routes_recency_and_calc_to_agent() -> None:
    config = StrategyRouterConfig(enabled=True)
    assert classify_request(_request("¿Qué noticias hay hoy sobre IA?"), config).strategy == "agent"
    assert classify_request(_request("Dame el precio actual del oro"), config).strategy == "agent"
    assert classify_request(_request("Calcula cuánto es 1234 * 55"), config).strategy == "agent"
    assert classify_request(_request("Resume esta página https://ejemplo.com"), config).strategy == "agent"
    assert classify_request(_request("Qué pasó en 2026 con los modelos"), config).strategy == "agent"


def test_classifier_routes_deliberative_to_mixture() -> None:
    config = StrategyRouterConfig(enabled=True)
    decision = classify_request(_request("Compara las ventajas y desventajas de Postgres y MySQL"), config)
    assert decision.strategy == "mixture_of_agents"
    assert any("deliberativa" in r for r in decision.reasons)


def test_classifier_long_prompt_to_mixture_and_budget_gate() -> None:
    config = StrategyRouterConfig(enabled=True, mixture_min_prompt_chars=50, mixture_min_budget_usd=0.01)
    long_prompt = "Explica en detalle " + ("x " * 60)
    assert classify_request(_request(long_prompt), config).strategy == "mixture_of_agents"
    # Con presupuesto por debajo del mínimo, no escala a mixture.
    capped = _request(long_prompt, model_requirements={"max_cost_usd": 0.001})
    decision = classify_request(capped, config)
    assert decision.strategy == "single"
    assert any("presupuesto" in r for r in decision.reasons)


def test_classifier_simple_prompt_to_single() -> None:
    config = StrategyRouterConfig(enabled=True)
    decision = classify_request(_request("¿Cuál es la capital de Francia?"), config)
    assert decision.strategy == "single"


def _client(tmp_path: Path, router: StrategyRouterConfig) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        strategy_router=router,
    )
    return TestClient(create_app(config, config_path=tmp_path / "broker_config.yaml"))


def test_auto_strategy_resolves_and_records_case(tmp_path: Path) -> None:
    with _client(tmp_path, StrategyRouterConfig(enabled=True)) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "auto:simple",
            "content": {"prompt": "¿Cuál es la capital de Francia?"},
            "execution": {"strategy": "auto"},
        })
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    routed = [e for e in detail["events"] if e["event_type"] == "strategy.routed"]
    assert len(routed) == 1
    # El caso queda registrado con señales y decisión para el aprendizaje futuro.
    assert routed[0]["payload"]["chosen_strategy"] == "single"
    assert "signals" in routed[0]["payload"]
    assert "prompt_chars" in routed[0]["payload"]["signals"]


def test_auto_strategy_disabled_router_falls_back_to_single(tmp_path: Path) -> None:
    with _client(tmp_path, StrategyRouterConfig(enabled=False)) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "auto:disabled",
            "content": {"prompt": "Compara las ventajas y desventajas de A y B en profundidad"},
            "execution": {"strategy": "auto"},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    routed = [e for e in detail["events"] if e["event_type"] == "strategy.routed"]
    # Router apagado: se resuelve a single aunque el prompt fuera deliberativo.
    assert routed[0]["payload"]["chosen_strategy"] == "single"


def test_auto_strategy_record_cases_can_be_disabled(tmp_path: Path) -> None:
    router = StrategyRouterConfig(enabled=True, record_cases=False)
    with _client(tmp_path, router) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "auto:nocases",
            "content": {"prompt": "hola"},
            "execution": {"strategy": "auto"},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    routed = [e for e in detail["events"] if e["event_type"] == "strategy.routed"]
    assert routed == []


def test_confidence_escalation_promotes_low_confidence_single_to_mixture(tmp_path: Path, monkeypatch) -> None:
    from app.providers.base import ModelOutput
    from app.providers.bootstrap import BootstrapModelProvider

    # El juez (rol confidence_judge) devuelve baja confianza; el resto responde normal.
    original = BootstrapModelProvider.propose

    async def judging_propose(self, request, model, ordinal):
        if "PREGUNTA" in request.content.prompt and "RESPUESTA" in request.content.prompt:
            return ModelOutput("0.2", 5, 1, 0.0, 1.0)
        return await original(self, request, model, ordinal)

    monkeypatch.setattr(BootstrapModelProvider, "propose", judging_propose)
    router = StrategyRouterConfig(enabled=True, confidence_escalation=True, escalation_min_confidence=0.6)
    with _client(tmp_path, router) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "auto:escalate",
            "content": {"prompt": "¿Cuál es la capital de Francia?"},
            "execution": {"strategy": "auto"},
            "model_requirements": {"allowed_providers": ["ollama"]},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    types = [e["event_type"] for e in detail["events"]]
    assert "strategy.confidence" in types
    assert "strategy.escalated" in types
    # Terminó como mixture: la síntesis del árbitro produce el resultado.
    assert "Síntesis" in detail["result"]["assistant_content"]


def test_confidence_escalation_keeps_single_when_confident(tmp_path: Path, monkeypatch) -> None:
    from app.providers.base import ModelOutput
    from app.providers.bootstrap import BootstrapModelProvider

    original = BootstrapModelProvider.propose

    async def judging_propose(self, request, model, ordinal):
        if "PREGUNTA" in request.content.prompt and "RESPUESTA" in request.content.prompt:
            return ModelOutput("0.95", 5, 1, 0.0, 1.0)
        return await original(self, request, model, ordinal)

    monkeypatch.setattr(BootstrapModelProvider, "propose", judging_propose)
    router = StrategyRouterConfig(enabled=True, confidence_escalation=True, escalation_min_confidence=0.6)
    with _client(tmp_path, router) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "auto:noescalate",
            "content": {"prompt": "¿Cuál es la capital de Francia?"},
            "execution": {"strategy": "auto"},
            "model_requirements": {"allowed_providers": ["ollama"]},
        })
        task_id = created.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    assert detail["task"]["status"] == "completed"
    types = [e["event_type"] for e in detail["events"]]
    assert "strategy.confidence" in types
    assert "strategy.escalated" not in types
    # Terminó como single (sin la síntesis del árbitro).
    assert "Síntesis" not in detail["result"]["assistant_content"]


def test_recommend_from_cases_learns_to_skip_single_after_escalations() -> None:
    from app.strategy_router import recommend_from_cases, signal_bucket

    # Bucket con 5 casos que empezaron single y escalaron: aprende mixture.
    cases = [
        {"chosen_strategy": "single", "final_strategy": "mixture_of_agents",
         "escalated": True, "status": "completed", "cost_usd": 0.0, "latency_ms": 1.0}
        for _ in range(5)
    ]
    rec = recommend_from_cases("single", cases, min_cases=5, escalation_threshold=0.5, failure_threshold=0.4)
    assert rec is not None
    assert rec[0] == "mixture_of_agents"
    assert "escalar" in rec[1]

    # Sin evidencia suficiente: no recomienda nada.
    assert recommend_from_cases("single", cases[:2], min_cases=5, escalation_threshold=0.5, failure_threshold=0.4) is None

    # Señales iguales → mismo bucket.
    s = {"needs_recent": False, "needs_calc": False, "deliberative": True}
    assert signal_bucket(s) == signal_bucket(dict(s))


def test_recommend_from_cases_avoids_failing_strategy() -> None:
    from app.strategy_router import recommend_from_cases

    # 'agent' falla mucho; 'single' completa. Aprende a evitar agent.
    cases = [
        {"chosen_strategy": "agent", "final_strategy": "agent",
         "escalated": False, "status": "failed", "cost_usd": 0.0, "latency_ms": 1.0}
        for _ in range(4)
    ] + [
        {"chosen_strategy": "single", "final_strategy": "single",
         "escalated": False, "status": "completed", "cost_usd": 0.0, "latency_ms": 1.0}
        for _ in range(5)
    ]
    rec = recommend_from_cases("agent", cases, min_cases=4, escalation_threshold=0.5, failure_threshold=0.4)
    assert rec is not None
    assert rec[0] == "single"


def test_adaptive_learning_routes_directly_to_mixture_after_escalations(tmp_path: Path, monkeypatch) -> None:
    from app.providers.base import ModelOutput
    from app.providers.bootstrap import BootstrapModelProvider

    original = BootstrapModelProvider.propose

    async def low_confidence_judge(self, request, model, ordinal):
        if "PREGUNTA" in request.content.prompt and "RESPUESTA" in request.content.prompt:
            return ModelOutput("0.1", 5, 1, 0.0, 1.0)
        return await original(self, request, model, ordinal)

    monkeypatch.setattr(BootstrapModelProvider, "propose", low_confidence_judge)
    router = StrategyRouterConfig(
        enabled=True, confidence_escalation=True, adaptive_learning=True,
        escalation_min_confidence=0.6, learning_min_cases=3,
    )
    with _client(tmp_path, router) as client:
        # 3 peticiones idénticas que escalan (single insuficiente) → alimentan casos.
        for i in range(3):
            r = client.post("/api/v1/tasks", json={
                "idempotency_key": f"learn-{i}",
                "content": {"prompt": "¿Cuál es la capital de Francia?"},
                "execution": {"strategy": "auto"},
                "model_requirements": {"allowed_providers": ["ollama"]},
            })
            client.post("/api/v1/dispatcher/tick")
            detail = client.get(f"/api/v1/dashboard/tasks/{r.json()['task_id']}").json()
            assert detail["result"] is not None

        # La 4ª, con el mismo tipo de petición, ya se enruta directa a mixture.
        r = client.post("/api/v1/tasks", json={
            "idempotency_key": "learn-final",
            "content": {"prompt": "¿Cuál es la capital de Alemania?"},
            "execution": {"strategy": "auto"},
            "model_requirements": {"allowed_providers": ["ollama"]},
        })
        task_id = r.json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        detail = client.get(f"/api/v1/dashboard/tasks/{task_id}").json()

    routed = [e for e in detail["events"] if e["event_type"] == "strategy.routed"][0]["payload"]
    assert routed["chosen_strategy"] == "mixture_of_agents"
    assert routed["learned"] is True
    # No hubo intento single desperdiciado: no se registró escalado en esta tarea.
    assert "strategy.escalated" not in [e["event_type"] for e in detail["events"]]


def test_routing_dashboard_shows_learned_insights(tmp_path: Path, monkeypatch) -> None:
    from app.providers.base import ModelOutput
    from app.providers.bootstrap import BootstrapModelProvider

    original = BootstrapModelProvider.propose

    async def low_judge(self, request, model, ordinal):
        if "PREGUNTA" in request.content.prompt and "RESPUESTA" in request.content.prompt:
            return ModelOutput("0.1", 5, 1, 0.0, 1.0)
        return await original(self, request, model, ordinal)

    monkeypatch.setattr(BootstrapModelProvider, "propose", low_judge)
    router = StrategyRouterConfig(
        enabled=True, confidence_escalation=True, adaptive_learning=True, learning_min_cases=3,
    )
    with _client(tmp_path, router) as client:
        empty = client.get("/dashboard/routing")
        assert empty.status_code == 200
        assert "Aún no hay casos" in empty.text

        for i in range(4):
            client.post("/api/v1/tasks", json={
                "idempotency_key": f"insight-{i}",
                "content": {"prompt": "¿Cuál es la capital de Francia?"},
                "execution": {"strategy": "auto"},
                "model_requirements": {"allowed_providers": ["ollama"]},
            })
            client.post("/api/v1/dispatcher/tick")
        page = client.get("/dashboard/routing").text

    assert "petición directa" in page
    # La heurística elegiría single, pero el aprendizaje recomienda mixture.
    assert "mixture_of_agents" in page
    assert "no bastó" in page or "escalar" in page
