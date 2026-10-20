"""R2: recuperar adjuntos en otro almacén y reparar referencias rotas."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.artifacts import ArtifactStore
from app.config import BrokerConfig, IngestionConfig, PersistenceConfig, ProcessingConfig
from app.db import Database
from app.ingestion.service import IngestionService
from app.main import create_app
from app.maintenance import create_state_backup, restore_state_backup, verify_state_backup
from app.state_lock import StateLock
from tests.test_ingestion import make_client, wait_for_file


def _config(root: Path) -> BrokerConfig:
    return BrokerConfig(
        persistence=PersistenceConfig(database=str(root / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        ingestion=IngestionConfig(storage_dir=str(root / "files"), isolate_conversions=False),
    )


def _backup(root: Path, output: Path):
    return create_state_backup(
        database_path=root / "broker.db",
        artifacts_root=root / "tasks",
        files_root=root / "files",
        output_path=output,
    )


def _restore(backup: Path, root: Path, *, replace: bool = False):
    restore_state_backup(
        backup_path=backup,
        database_path=root / "broker.db",
        artifacts_root=root / "tasks",
        files_root=root / "files",
        replace=replace,
    )


def test_restore_elsewhere_runs_a_task_with_its_documents_and_artifacts(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    text = "El documento contiene la cifra 12345."
    with TestClient(create_app(_config(source))) as client:
        client.app.state.coordinator.artifacts = ArtifactStore(source / "tasks")
        uploaded = client.post("/api/v1/files", files={"file": ("nota.txt", text.encode())}).json()
        state = wait_for_file(client, uploaded["file_id"])
        assert state["status"] == "ready"
        payload = {
            "idempotency_key": "restore:task",
            "content": {
                "prompt": "Repite la cifra del documento",
                "attachments": [
                    {"type": "broker_file", "metadata": {"file_id": uploaded["file_id"]}},
                ],
            },
        }
        task_id = client.post("/api/v1/tasks", json=payload).json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "completed"
    backup = tmp_path / "backup.zip"
    _backup(source, backup)
    manifest = verify_state_backup(backup)
    assert any(r["path"].startswith("files/") for r in manifest["files"])
    assert {r["table"] for r in manifest["references"]} == {"ingested_files", "artifacts"}
    # Simula perder el almacén del equipo anterior.
    shutil.rmtree(source)
    _restore(backup, restored)
    with TestClient(create_app(_config(restored))) as client:
        client.app.state.coordinator.artifacts = ArtifactStore(restored / "tasks")
        assert client.get(f"/api/v1/files/{uploaded['file_id']}/markdown").text == text
        record = client.app.state.ingestion.get(uploaded["file_id"])
        assert Path(record.original_path).is_relative_to(restored)
        assert Path(record.markdown_path).is_relative_to(restored)
        assert client.get(f"/api/v1/files/{uploaded['file_id']}").json()["cloud_allowed"] is False
        artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()["items"]
        assert artifacts
        artifact_id = artifacts[0]["artifact_id"]
        assert client.get(f"/api/v1/tasks/{task_id}/artifacts/{artifact_id}").status_code == 200
        payload["idempotency_key"] = "restore:new-task"
        new_id = client.post("/api/v1/tasks", json=payload).json()["task_id"]
        client.post("/api/v1/dispatcher/tick")
        assert client.get(f"/api/v1/tasks/{new_id}").json()["status"] == "completed"


@pytest.mark.parametrize("missing", ["original", "markdown", "both", "corrupt", "original_corrupt"])
def test_resubmission_repairs_same_id_and_enqueues_again(tmp_path, missing):
    with make_client(tmp_path) as client:
        body = b"documento recuperable"
        first = client.post("/api/v1/files", files={"file": ("nota.txt", body)}).json()
        assert wait_for_file(client, first["file_id"])["status"] == "ready"
        record = client.app.state.ingestion.get(first["file_id"])
        if missing in {"original", "both"}:
            Path(record.original_path).unlink()
        if missing in {"markdown", "both"}:
            Path(record.markdown_path).unlink()
        if missing == "corrupt":
            Path(record.markdown_path).write_text("contenido incorrecto", encoding="utf-8")
        if missing == "original_corrupt":
            Path(record.original_path).write_bytes(b"original incorrecto")
        second = client.post("/api/v1/files", files={"file": ("nota.txt", body)}).json()
        assert second["created"] is False
        assert second["file_id"] == first["file_id"]
        assert second["status"] == "received"
        assert wait_for_file(client, first["file_id"])["status"] == "ready"
        assert client.get(f"/api/v1/files/{first['file_id']}/markdown").text == body.decode()
        count = client.app.state.db.query_one("SELECT COUNT(*) FROM tasks WHERE kind = 'ingestion'")[0]
        assert count == 2


def test_dashboard_resubmission_also_repairs_and_enqueues(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
        record = client.app.state.ingestion.get(first["file_id"])
        Path(record.markdown_path).unlink()
        client.get("/dashboard/files")
        token = client.cookies.get("ai_broker_dashboard_csrf")
        response = client.post(
            "/dashboard/actions/files/upload",
            data={"csrf_token": token},
            headers={"X-CSRF-Token": token},
            files={"file": ("nota.txt", b"texto")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert wait_for_file(client, first["file_id"])["status"] == "ready"


def test_backup_refuses_an_orphan_instead_of_claiming_recoverability(tmp_path):
    root = tmp_path / "source"
    with TestClient(create_app(_config(root))) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
        Path(client.app.state.ingestion.get(first["file_id"]).markdown_path).unlink()
    output = tmp_path / "backup.zip"
    with pytest.raises(ValueError, match="falta"):
        _backup(root, output)
    assert not output.exists()


def test_backup_and_restore_refuse_while_the_broker_is_running(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    with TestClient(create_app(_config(source))):
        with pytest.raises(RuntimeError, match="Detén"):
            _backup(source, tmp_path / "backup.zip")
    backup = tmp_path / "backup.zip"
    _backup(source, backup)
    with TestClient(create_app(_config(restored))):
        with pytest.raises(RuntimeError, match="Detén"):
            _restore(backup, restored, replace=True)


def test_os_lock_releases_and_refuses_a_second_owner(tmp_path):
    database = tmp_path / "broker.db"
    with StateLock(database):
        with pytest.raises(RuntimeError):
            with StateLock(database):
                pass
    with StateLock(database):
        pass


def test_incomplete_restore_prevents_startup_and_can_be_retried(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    with TestClient(create_app(_config(source))):
        pass
    backup = tmp_path / "backup.zip"
    _backup(source, backup)
    restored.mkdir()
    marker = StateLock(restored / "broker.db").restore_marker
    marker.write_text("interrumpido", encoding="utf-8")
    with pytest.raises(RuntimeError, match="interrumpida"):
        with TestClient(create_app(_config(restored))):
            pass
    _restore(backup, restored)
    assert not marker.exists()
    with TestClient(create_app(_config(restored))) as client:
        assert client.get("/health/live").status_code == 200


def test_invalid_backup_does_not_replace_existing_targets(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    with TestClient(create_app(_config(source))) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
    good, bad = tmp_path / "good.zip", tmp_path / "bad.zip"
    _backup(source, good)
    with zipfile.ZipFile(good) as original, zipfile.ZipFile(bad, "w") as damaged:
        for name in original.namelist():
            data = original.read(name)
            if name.endswith("converted.md"):
                data = b"alterado"
            damaged.writestr(name, data)
    restored.mkdir()
    database = restored / "broker.db"
    database.write_bytes(b"destino anterior")
    with pytest.raises(ValueError):
        _restore(bad, restored, replace=True)
    assert database.read_bytes() == b"destino anterior"


def test_missing_reference_in_manifest_is_rejected(tmp_path):
    source = tmp_path / "source"
    with TestClient(create_app(_config(source))) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
    good, bad = tmp_path / "good.zip", tmp_path / "bad.zip"
    _backup(source, good)
    with zipfile.ZipFile(good) as original, zipfile.ZipFile(bad, "w") as changed:
        for name in original.namelist():
            data = original.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["references"] = []
                data = json.dumps(manifest).encode()
            changed.writestr(name, data)
    with pytest.raises(ValueError, match="referencias"):
        verify_state_backup(bad)


def test_failed_restore_rolls_back_existing_files(tmp_path, monkeypatch):
    import app.maintenance as maintenance

    source, restored = tmp_path / "source", tmp_path / "restored"
    with TestClient(create_app(_config(source))) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
    backup = tmp_path / "backup.zip"
    _backup(source, backup)
    _restore(backup, restored)
    original = restored / "files" / first["file_id"] / "original.txt"
    original.write_bytes(b"version anterior")
    before_db = (restored / "broker.db").read_bytes()
    replace = maintenance.os.replace
    failed = False

    def fail_database_once(source_path, target_path):
        nonlocal failed
        if Path(target_path) == restored / "broker.db" and not failed:
            failed = True
            raise OSError("fallo de escritura simulado")
        return replace(source_path, target_path)

    monkeypatch.setattr(maintenance.os, "replace", fail_database_once)
    with pytest.raises(OSError, match="simulado"):
        _restore(backup, restored, replace=True)
    assert original.read_bytes() == b"version anterior"
    assert (restored / "broker.db").read_bytes() == before_db


def test_repair_preserves_image_identity_without_creating_markdown(tmp_path):
    from tests.test_image_attachments import PIXEL_PNG

    with make_client(tmp_path) as client:
        first = client.post("/api/v1/files", files={"file": ("foto.png", PIXEL_PNG)}).json()
        record = client.app.state.ingestion.get(first["file_id"])
        Path(record.original_path).unlink()
        repaired = client.post("/api/v1/files", files={"file": ("foto.png", PIXEL_PNG)}).json()
        assert repaired["file_id"] == first["file_id"] and repaired["status"] == "ready"
        assert repaired["created"] is False
        assert client.app.state.ingestion.get(first["file_id"]).markdown_path is None


def test_v1_backup_with_documents_is_refused_with_explanation(tmp_path):
    root = tmp_path / "source"
    config = _config(root)
    db = Database(root / "broker.db")
    db.init_schema()
    service = IngestionService(db, config)
    record, _ = service.store_upload("nota.txt", b"texto")
    asyncio.run(service.process(record.id))
    db.close()
    good, old = tmp_path / "new.zip", tmp_path / "old.zip"
    _backup(root, good)
    with zipfile.ZipFile(good) as original, zipfile.ZipFile(old, "w") as legacy:
        manifest = json.loads(original.read("manifest.json"))
        manifest["format"] = "ai-broker-backup-v1"
        manifest["files"] = [r for r in manifest["files"] if not r["path"].startswith("files/")]
        manifest.pop("references")
        for item in manifest["files"]:
            legacy.writestr(item["path"], original.read(item["path"]))
        legacy.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="antigua"):
        _restore(old, tmp_path / "restored")
    assert not (tmp_path / "restored" / "broker.db").exists()


def test_restore_requires_replace_and_old_empty_backups_remain_supported(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    source.mkdir()
    with sqlite3.connect(source / "broker.db") as connection:
        connection.execute("CREATE TABLE example (id INTEGER)")
    good, old = tmp_path / "new.zip", tmp_path / "old.zip"
    _backup(source, good)
    with zipfile.ZipFile(good) as original, zipfile.ZipFile(old, "w") as legacy:
        manifest = json.loads(original.read("manifest.json"))
        manifest["format"] = "ai-broker-backup-v1"
        legacy.writestr("broker.db", original.read("broker.db"))
        legacy.writestr("manifest.json", json.dumps(manifest))
    _restore(old, restored)
    with pytest.raises(FileExistsError):
        _restore(old, restored)


def test_an_old_orphan_that_failed_conversion_can_be_repaired_without_losing_its_id(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        wait_for_file(client, first["file_id"])
        record = client.app.state.ingestion.get(first["file_id"])
        Path(record.original_path).unlink()
        client.app.state.db.execute(
            "UPDATE ingested_files SET status = 'failed', privacy_policy_version = 0 WHERE id = ?",
            (first["file_id"],),
        )
        repaired = client.post("/api/v1/files", files={"file": ("nota.txt", b"texto")}).json()
        assert repaired["file_id"] == first["file_id"]
        assert repaired["privacy_policy_known"] is True and repaired["cloud_allowed"] is False
        assert wait_for_file(client, first["file_id"])["status"] == "ready"


def test_repair_of_transcoded_images_uses_the_uploaded_format_again(tmp_path, monkeypatch):
    import app.ingestion.engines as engines
    from tests.test_image_attachments import PIXEL_PNG

    monkeypatch.setattr(engines, "transcode_to_png", lambda data: PIXEL_PNG)
    with make_client(tmp_path) as client:
        original_bytes = b"II*\x00tiff de prueba"
        first = client.post("/api/v1/files", files={"file": ("foto.tiff", original_bytes)}).json()
        record = client.app.state.ingestion.get(first["file_id"])
        Path(record.original_path).unlink()
        repaired = client.post("/api/v1/files", files={"file": ("foto.tiff", original_bytes)}).json()
        assert repaired["file_id"] == first["file_id"] and repaired["status"] == "ready"
        assert Path(client.app.state.ingestion.get(first["file_id"]).original_path).read_bytes() == PIXEL_PNG


def test_restore_rejects_overlapping_stores_before_touching_targets(tmp_path):
    source = tmp_path / "source"
    with TestClient(create_app(_config(source))):
        pass
    backup = tmp_path / "backup.zip"
    _backup(source, backup)
    with pytest.raises(ValueError, match="separadas"):
        restore_state_backup(
            backup_path=backup,
            database_path=tmp_path / "restored" / "broker.db",
            artifacts_root=tmp_path / "restored" / "files",
            files_root=tmp_path / "restored" / "files",
        )
    assert not (tmp_path / "restored" / "broker.db").exists()
