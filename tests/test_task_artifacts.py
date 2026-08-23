"""Los ficheros que produce una tarea se pueden recoger.

El caso que lo motiva es una imagen generada por un modelo: se guardaba en
`state/tasks/…`, se anotaba en la tabla `artifacts` y ahí se quedaba. No cabe en
`result` —ese documento se lee entero en cada sondeo del estado, y un PNG en
base64 dentro lo convertiría en megabytes por lectura—, ninguna ruta HTTP la
servía y el panel no la enseñaba. Es decir: el broker sabía dibujar y no sabía
entregar el dibujo.
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.artifacts import ArtifactStore
from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.db import Database
from app.main import create_app
from app.providers.base import GeneratedImage, ModelOutput, returned_images
from app.repository import TaskRepository

PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


def _task_with_image(tmp_path: Path, client: TestClient) -> tuple[str, str]:
    """Crea una tarea y le cuelga una imagen como artefacto, igual que hace el
    coordinador cuando un modelo responde con una."""
    response = client.post("/api/v1/tasks", json={
        "idempotency_key": f"artifacts:{tmp_path.name}",
        "content": {"prompt": "dibuja un gato"},
    })
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    db = Database(Path(str(tmp_path / "broker.db")), "WAL")
    try:
        repository = TaskRepository(db)
        store = ArtifactStore(Path(str(tmp_path / "tasks")))
        record = store.write_bytes(task_id, "single/image_01.png", PIXEL_PNG)
        artifact_id = repository.record_artifact(task_id, None, None, "image_output", record)
    finally:
        db.close()
    return task_id, artifact_id


def test_the_generated_image_can_be_listed_and_downloaded(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        task_id, artifact_id = _task_with_image(tmp_path, client)

        listing = client.get(f"/api/v1/tasks/{task_id}/artifacts")
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["artifact_type"] == "image_output"
        assert item["filename"] == "image_01.png"
        # El tipo MIME va en el listado para que un cliente sepa si puede
        # pintarlo sin descargarlo antes para averiguarlo.
        assert item["media_type"] == "image/png"
        assert item["size_bytes"] == len(PIXEL_PNG)
        assert item["available"] is True

        download = client.get(item["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "image/png"
        assert download.content == PIXEL_PNG


def test_a_pruned_artifact_is_listed_as_gone_instead_of_pretending(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        task_id, artifact_id = _task_with_image(tmp_path, client)
        # La retención borra el fichero pero no la fila: el registro de que
        # existió es información, y ofrecer una descarga que da 404 no lo es.
        Path(str(tmp_path / "tasks" / task_id / "single" / "image_01.png")).unlink()

        item = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()["items"][0]
        assert item["available"] is False
        gone = client.get(item["download_url"])
        # 410 y no 404: existió y se borró a propósito, que no es lo mismo que
        # no haber existido nunca.
        assert gone.status_code == 410


def test_an_unknown_artifact_or_task_is_a_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        task_id, _ = _task_with_image(tmp_path, client)
        assert client.get(f"/api/v1/tasks/{task_id}/artifacts/art_inexistente").status_code == 404
        assert client.get("/api/v1/tasks/task_inexistente/artifacts").status_code == 404


def test_an_artifact_of_another_task_is_not_reachable(tmp_path: Path) -> None:
    """El identificador se resuelve contra la pareja (tarea, artefacto): pedir
    el artefacto de otra tarea no lo sirve aunque el id exista."""
    with _client(tmp_path) as client:
        task_id, artifact_id = _task_with_image(tmp_path, client)
        otra = client.post("/api/v1/tasks", json={
            "idempotency_key": "artifacts:otra",
            "content": {"prompt": "otra cosa"},
        }).json()["task_id"]
        assert client.get(f"/api/v1/tasks/{otra}/artifacts/{artifact_id}").status_code == 404


def test_the_dashboard_shows_the_image_instead_of_only_naming_it(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        task_id, artifact_id = _task_with_image(tmp_path, client)
        page = client.get(f"/dashboard/tasks/{task_id}")
        assert page.status_code == 200
        html = page.text
        ruta = f"/dashboard/tasks/{task_id}/artifacts/{artifact_id}"
        # Se pinta, no solo se enumera: si el modelo dibujó algo, mirarlo es la
        # razón entera de abrir esta pantalla.
        assert f'<img src="{ruta}"' in html
        assert "image_01.png" in html

        servido = client.get(ruta)
        assert servido.status_code == 200
        assert servido.content == PIXEL_PNG


def test_an_uploaded_image_can_be_looked_at_from_the_panel(tmp_path: Path) -> None:
    """La simetría del caso anterior: una imagen subida no tiene Markdown, así
    que sin una ruta propia su fila del panel no ofrecía nada que mirar."""
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    config.ingestion.storage_dir = str(tmp_path / "files")
    with TestClient(create_app(config)) as client:
        subida = client.post(
            "/api/v1/files",
            files={"file": ("gato.png", PIXEL_PNG, "image/png")},
        )
        assert subida.status_code == 202
        file_id = subida.json()["file_id"]
        # Nace lista: no hay conversión que esperar.
        assert subida.json()["status"] == "ready"

        vista = client.get(f"/dashboard/files/{file_id}/original")
        assert vista.status_code == 200
        assert vista.headers["content-type"] == "image/png"
        assert vista.content == PIXEL_PNG

        tabla = client.get("/dashboard/fragments/files")
        assert f"/dashboard/files/{file_id}/original" in tabla.text


def test_the_capability_is_announced(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/v1/capabilities").json()["task_artifacts"] is True


def test_the_task_state_stays_free_of_image_bytes(tmp_path: Path) -> None:
    """La imagen no puede colarse en el resultado JSON: ese documento se lee
    entero en cada sondeo."""
    output = ModelOutput(
        content="ahí lo tienes",
        tokens_input=1,
        tokens_output=1,
        cost_usd=0.0,
        latency_ms=1.0,
        images=(GeneratedImage(media_type="image/png", data_base64=base64.b64encode(PIXEL_PNG).decode()),),
    )
    technical = output.technical_output()
    assert technical["images_returned"] == 1
    assert "data_base64" not in str(technical)


def test_returned_images_understands_both_provider_dialects() -> None:
    ollama = returned_images({"images": [base64.b64encode(PIXEL_PNG).decode()]})
    openai = returned_images({"images": [
        {"image_url": {"url": f"data:image/webp;base64,{base64.b64encode(PIXEL_PNG).decode()}"}},
    ]})
    assert ollama[0].media_type == "image/png"
    assert ollama[0].extension == ".png"
    assert openai[0].media_type == "image/webp"
    assert openai[0].extension == ".webp"


if __name__ == "__main__":
    pytest.main([__file__])
