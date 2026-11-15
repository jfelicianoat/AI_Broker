"""AB-06: /health publica la versión de contrato, la misma que capabilities."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app
from app.schemas import CONTRACT_VERSION


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


def test_health_reports_the_same_contract_version_as_capabilities(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        health = client.get("/health")
        capabilities = client.get("/api/v1/capabilities")
        ready = client.get("/health/ready")
    assert health.status_code == 200 and capabilities.status_code == 200
    assert health.json()["contract_version"] == capabilities.json()["contract_version"]
    assert health.json()["contract_version"] == CONTRACT_VERSION
    assert ready.json()["contract_version"] == CONTRACT_VERSION


def test_contract_version_is_not_hardcoded_elsewhere() -> None:
    """Una sola fuente: si alguien vuelve a escribir la versión a mano en
    main.py, health y capabilities pueden separarse."""
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert f'contract_version="{CONTRACT_VERSION}"' not in source
