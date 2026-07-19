"""Corpus dorado de conversión: regresión frente a cambios de motor.

Cada caso sube un fichero real de tests/fixtures/ingestion y comprueba que el
Markdown resultante conserva el contenido clave. Se afirma por sub-cadenas, no
por igualdad exacta: al actualizar Docling/MarkItDown el formato puede variar
sin que eso sea una regresión; perder el contenido sí lo es.

Los casos que dependen de un motor pesado se saltan si el paquete no está
instalado (mismo contrato que en producción: ENGINE_MISSING no rompe nada).
El caso Docling es lento (carga modelos de layout) y solo corre con
AI_BROKER_CORPUS_PDF=1 para no penalizar el ciclo local ni el CI.
"""
import importlib.util
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BrokerConfig, IngestionConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app

CORPUS = Path(__file__).parent / "fixtures" / "ingestion"


def engine_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        ingestion=IngestionConfig(storage_dir=str(tmp_path / "files")),
    )
    return TestClient(create_app(config))


def convert_fixture(client: TestClient, name: str, timeout: float) -> str:
    with (CORPUS / name).open("rb") as handle:
        body = client.post("/api/v1/files", files={"file": (name, handle)}).json()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/files/{body['file_id']}").json()
        if state["status"] in {"ready", "failed"}:
            break
        time.sleep(0.2)
    assert state["status"] == "ready", f"{name}: {state.get('error')}"
    return client.get(state["markdown_url"]).text


def test_corpus_markdown_passthrough(tmp_path):
    with make_client(tmp_path) as client:
        markdown = convert_fixture(client, "notas.md", timeout=10)
    assert "# Notas de arquitectura" in markdown
    assert "modo WAL" in markdown


def test_corpus_csv_fenced(tmp_path):
    with make_client(tmp_path) as client:
        markdown = convert_fixture(client, "ventas.csv", timeout=10)
    assert markdown.startswith("```csv")
    assert "Valencia,45678" in markdown


@pytest.mark.skipif(not engine_available("markitdown"), reason="markitdown no instalado")
def test_corpus_docx_markitdown(tmp_path):
    with make_client(tmp_path) as client:
        markdown = convert_fixture(client, "informe.docx", timeout=60)
    # Contenido íntegro: título, cifra clave y frase final.
    assert "Informe trimestral de ventas" in markdown
    assert "45.678 euros" in markdown
    assert "Valencia" in markdown


@pytest.mark.skipif(
    not engine_available("docling") or os.environ.get("AI_BROKER_CORPUS_PDF") != "1",
    reason="docling no instalado o AI_BROKER_CORPUS_PDF!=1 (caso lento, opt-in)",
)
def test_corpus_pdf_docling(tmp_path):
    with make_client(tmp_path) as client:
        markdown = convert_fixture(client, "acta.pdf", timeout=600)
    assert "reunion tecnica" in markdown
    assert "9.500 euros" in markdown
    assert "fase siete" in markdown
