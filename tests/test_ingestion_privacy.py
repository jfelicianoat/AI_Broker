"""R4: el permiso del documento protege la conversión y las tareas."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from app.config import IngestionImagesConfig, OpenAICompatibleProviderConfig
from app.db import Database
from app.ingestion import engines
from app.ingestion.service import AttachmentError
from app.ingestion.vision import VisionTarget, select_vision_target
from app.schemas import DataClassification, TaskCreateRequest
from tests.test_ingestion import make_client, wait_for_file


def _upload(client, **policy):
    return client.post(
        "/api/v1/files",
        files={"file": ("informe.pdf", b"%PDF-1.7 contenido")},
        data=policy,
    )


def _cloud_vision(client, monkeypatch, calls: list, *, pictures: int = 1):
    config = client.app.state.config
    config.providers.custom = [
        OpenAICompatibleProviderConfig(
            id="cloudvision",
            enabled=True,
            base_url="https://vision.example.invalid/v1",
            deployment="cloud",
        )
    ]

    class Catalog:
        async def models(self):
            return [{"name": "visor", "provider": "cloudvision", "deployment": "cloud", "features": {"vision": True}}]

    client.app.state.ingestion.provider = Catalog()
    monkeypatch.setattr(
        engines,
        "convert_pdf_docling",
        lambda *a, **k: engines.DoclingResult(
            markdown="Texto\n" + (engines.IMAGE_PLACEHOLDER + "\n") * pictures,
            pages=1,
            ocr_enabled=True,
            pictures=[b"imagen"] * pictures,
        ),
    )

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [{"message": {"content": "Descripción de prueba"}}],
            },
        )

    monkeypatch.setattr(httpx, "post", post)


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"data_classification": "local_only"},
        {"data_classification": "confidential"},
        {"data_classification": "internal"},
        {"data_classification": "public", "cloud_allowed": "false"},
    ],
)
def test_cloud_only_catalog_never_receives_unapproved_documents(tmp_path, monkeypatch, policy):
    calls = []
    with make_client(tmp_path, images=IngestionImagesConfig(enabled=True, model="visor")) as client:
        _cloud_vision(client, monkeypatch, calls)
        response = _upload(client, **policy)
        assert response.status_code == 202
        first = response.json()
        assert first["cloud_allowed"] is False and first["privacy_policy_known"] is True
        state = wait_for_file(client, first["file_id"])
        assert state["status"] == "ready"
        assert state["meta"]["pictures_described"] == 0
        assert "imagen no descrita" in client.get(state["markdown_url"]).text
    assert calls == []


def test_explicit_permission_enables_cloud_and_records_its_origin(tmp_path, monkeypatch):
    calls = []
    with make_client(tmp_path, images=IngestionImagesConfig(enabled=True, model="visor")) as client:
        _cloud_vision(client, monkeypatch, calls)
        first = _upload(client, data_classification="internal", cloud_allowed="true").json()
        state = wait_for_file(client, first["file_id"])
        assert state["status"] == "ready" and state["cloud_allowed"] is True
        assert state["meta"]["vision_deployment"] == "cloud"
        assert state["meta"]["pictures_described"] == 1
    assert len(calls) == 1
    assert "image_url" in str(calls[0][1]["json"])
    assert "Texto" in str(calls[0][1]["json"])


@pytest.mark.parametrize("classification", ["local_only", "confidential"])
def test_contradictory_permission_is_rejected_and_temp_file_is_removed(tmp_path, classification):
    with make_client(tmp_path) as client:
        response = _upload(client, data_classification=classification, cloud_allowed="true")
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "FILE_PRIVACY_INVALID"
        assert client.app.state.ingestion.list_files() == []
        assert list(client.app.state.ingestion.incoming_dir.glob("*")) == []


def test_low_level_sender_refuses_cloud_even_if_selection_is_bypassed(monkeypatch):
    target = VisionTarget("cloudvision", "visor", "cloud", "https://vision.example.invalid", None, "openai", "probe")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="no autoriza"):
        engines.describe_image(target, b"imagen", "contenido privado", 2)
    with pytest.raises(ValueError, match="no autoriza"):
        engines.describe_image(
            target, b"imagen", "contenido privado", 2, cloud_allowed=True, data_classification="confidential"
        )
    assert calls == []


def test_permission_is_rechecked_before_each_picture(tmp_path, monkeypatch):
    calls = []
    with make_client(tmp_path, images=IngestionImagesConfig(enabled=True, model="visor")) as client:
        _cloud_vision(client, monkeypatch, calls, pictures=2)
        first = _upload(client, data_classification="internal", cloud_allowed="true").json()
        post = httpx.post

        def revoke_after_first_call(*args, **kwargs):
            result = post(*args, **kwargs)
            client.app.state.db.execute("UPDATE ingested_files SET cloud_allowed = 0 WHERE id = ?", (first["file_id"],))
            return result

        monkeypatch.setattr(httpx, "post", revoke_after_first_call)
        state = wait_for_file(client, first["file_id"])
        assert state["status"] == "ready"
        assert state["meta"]["pictures_described"] == 1
        assert state["meta"]["pictures_describe_errors"] == 1
    assert len(calls) == 1


def test_deduplication_never_inherits_another_uploads_permission(tmp_path):
    with make_client(tmp_path) as client:
        cloud = _upload(client, data_classification="internal", cloud_allowed="true").json()
        local = _upload(client).json()
        confidential = _upload(client, data_classification="confidential").json()
        assert len({cloud["file_id"], local["file_id"], confidential["file_id"]}) == 3
        assert local["cloud_allowed"] is False
        repeated = _upload(client).json()
        assert repeated["file_id"] == local["file_id"] and repeated["created"] is False


def _task(file_id, **kwargs):
    return {
        "idempotency_key": "privacy:task",
        "content": {
            "prompt": "Resume",
            "attachments": [
                {"type": "broker_file", "metadata": {"file_id": file_id}},
            ],
        },
        **kwargs,
    }


def test_local_document_restricts_the_saved_task_and_implicit_tools(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
        response = client.post("/api/v1/tasks", json=_task(first["file_id"]))
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        saved = client.app.state.repository.get_task_request(task_id)
        assert saved.risk.data_classification == DataClassification.local_only
        assert saved.model_requirements.cloud_allowed is False
        assert "web_search" not in saved.execution.agent.skills
        client.post("/api/v1/dispatcher/tick")
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "completed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_requirements": {"target_model": {"provider": "deepseek", "deployment": "cloud", "model": "visor"}}},
        {"execution": {"strategy": "agent", "agent": {"skills": ["web_search"]}}},
    ],
)
def test_explicit_egress_conflicts_with_a_local_document(tmp_path, overrides):
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
        response = client.post("/api/v1/tasks", json=_task(first["file_id"], **overrides))
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "ATTACHED_FILE_PRIVACY_CONFLICT"


def test_task_is_rechecked_when_a_document_becomes_more_restricted(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post(
            "/api/v1/files",
            files={"file": ("nota.txt", b"texto")},
            data={
                "data_classification": "internal",
                "cloud_allowed": "true",
            },
        ).json()
        wait_for_file(client, first["file_id"])
        payload = _task(
            first["file_id"],
            model_requirements={
                "target_model": {
                    "provider": "deepseek",
                    "deployment": "cloud",
                    "model": "visor",
                }
            },
        )
        response = client.post("/api/v1/tasks", json=payload)
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        client.app.state.db.execute("UPDATE ingested_files SET cloud_allowed = 0 WHERE id = ?", (first["file_id"],))
        client.post("/api/v1/dispatcher/tick")
        result = client.get(f"/api/v1/tasks/{task_id}").json()
        assert result["status"] == "failed"
        assert result["error"]["code"] == "ATTACHED_FILE_PRIVACY_CONFLICT"
        assert (
            client.app.state.db.query_one("SELECT COUNT(*) FROM model_invocations WHERE task_id = ?", (task_id,))[0]
            == 0
        )


def test_mixed_attachments_use_the_strictest_permission(tmp_path):
    with make_client(tmp_path) as client:
        permitted = client.post(
            "/api/v1/files",
            files={"file": ("publico.txt", b"uno")},
            data={
                "data_classification": "public",
                "cloud_allowed": "true",
            },
        ).json()
        local = client.post("/api/v1/files", files={"file": ("privado.txt", b"dos")}).json()
        for file_id in (permitted["file_id"], local["file_id"]):
            wait_for_file(client, file_id)
        payload = _task(permitted["file_id"])
        payload["content"]["attachments"].append({"type": "broker_file", "metadata": {"file_id": local["file_id"]}})
        response = client.post("/api/v1/tasks", json=payload)
        assert response.status_code == 202
        saved = client.app.state.repository.get_task_request(response.json()["task_id"])
        assert saved.model_requirements.cloud_allowed is False


def test_cloud_document_does_not_override_a_local_task(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post(
            "/api/v1/files",
            files={"file": ("nota.txt", b"texto")},
            data={
                "data_classification": "internal",
                "cloud_allowed": "true",
            },
        ).json()
        wait_for_file(client, first["file_id"])
        response = client.post(
            "/api/v1/tasks", json=_task(first["file_id"], risk={"data_classification": "local_only"})
        )
        saved = client.app.state.repository.get_task_request(response.json()["task_id"])
        assert saved.model_requirements.cloud_allowed is False


def test_dashboard_exposes_and_persists_explicit_permission(tmp_path):
    with make_client(tmp_path) as client:
        page = client.get("/dashboard/files")
        assert 'name="data_classification"' in page.text and 'name="cloud_allowed"' in page.text
        assert "Solo en este equipo" in page.text
        token = client.cookies.get("ai_broker_dashboard_csrf")
        response = client.post(
            "/dashboard/actions/files/upload",
            headers={"X-CSRF-Token": token},
            files={"file": ("nota.txt", b"texto")},
            follow_redirects=False,
            data={"csrf_token": token, "data_classification": "internal", "cloud_allowed": "true"},
        )
        assert response.status_code == 303
        record = client.app.state.ingestion.list_files()[0]
        assert record.cloud_allowed is True and record.data_classification == DataClassification.internal
        assert "Nube autorizada" in client.get("/dashboard/files").text


def test_legacy_rows_are_local_unknown_and_not_reused_as_new_permission(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
        client.app.state.db.execute(
            "UPDATE ingested_files SET privacy_policy_version = 0 WHERE id = ?", (first["file_id"],)
        )
        state = client.get(f"/api/v1/files/{first['file_id']}").json()
        assert state["privacy_policy_known"] is False and state["cloud_allowed"] is False
        second = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        assert second["file_id"] != first["file_id"]


def test_database_migration_never_grants_cloud_to_old_documents(tmp_path):
    database = tmp_path / "broker.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ingested_files (id TEXT, sha256 TEXT, status TEXT)")
        connection.execute("INSERT INTO ingested_files VALUES ('old', 'abc', 'ready')")
    db = Database(database)
    try:
        db.init_schema()
        row = db.query_one("SELECT data_classification, cloud_allowed, privacy_policy_version FROM ingested_files")
        assert tuple(row) == ("local_only", 0, 0)
    finally:
        db.close()


def test_cloud_config_cannot_be_disguised_by_a_local_catalog_entry(tmp_path):
    with make_client(tmp_path) as client:
        config = client.app.state.config
        config.ingestion.images.enabled = True
        config.providers.custom = [
            OpenAICompatibleProviderConfig(
                id="cloudvision",
                enabled=True,
                base_url="https://vision.example.invalid",
                deployment="cloud",
            )
        ]
        catalog = [{"name": "visor", "provider": "cloudvision", "deployment": "local", "features": {"vision": True}}]
        assert select_vision_target(config, catalog) is None
        assert (
            select_vision_target(
                config, catalog, data_classification=DataClassification.internal, cloud_allowed=True
            ).deployment
            == "cloud"
        )


def test_local_document_blocks_egress_mcp_even_on_dispatch(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
        request = TaskCreateRequest.model_validate(
            _task(
                first["file_id"],
                execution={
                    "strategy": "agent",
                    "agent": {"skills": ["calculator"], "mcp_servers": ["externo"]},
                },
            )
        )
        with pytest.raises(AttachmentError, match="servidores"):
            client.app.state.ingestion.apply_attachment_policy(request, egress_server_ids=["externo"])
