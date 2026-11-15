"""Tokens por cliente: alcance limitado y aislamiento entre apps (AOS-03 de fondo)."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.admin_auth as admin_auth
from app.client_tokens import TOKEN_PREFIX, ClientTokenError, ClientTokenStore
from app.config import BrokerConfig, IngestionConfig, PersistenceConfig, ProcessingConfig
from app.db import Database
from app.main import create_app
from tests.test_auth_surface import AUTHENTICATED_ROUTES, PATH_PARAM_SAMPLE, registered_routes

ADMIN_TOKEN = "secreto-de-operador"
ADMIN = {"X-Admin-Token": ADMIN_TOKEN}

# Rutas de trabajo que un token de cliente puede usar. Todo lo demás de
# AUTHENTICATED_ROUTES es administración y le responde 403.
CLIENT_ROUTES = {
    ("POST", "/api/v1/system1/judge"),
    ("POST", "/api/v1/tasks"),
    ("GET", "/api/v1/tasks/{task_id}"),
    ("DELETE", "/api/v1/tasks/{task_id}"),
    ("GET", "/api/v1/tasks/{task_id}/invocations"),
    ("GET", "/api/v1/tasks/{task_id}/artifacts"),
    ("GET", "/api/v1/tasks/{task_id}/artifacts/{artifact_id}"),
    ("POST", "/api/v1/tasks/{task_id}/tool_results"),
    ("GET", "/api/v1/groups/{group}"),
    ("POST", "/api/v1/files"),
    ("GET", "/api/v1/files/{file_id}"),
    ("GET", "/api/v1/files/{file_id}/markdown"),
    ("GET", "/api/v1/auth/check"),
}


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *args, **kwargs: None)
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", ADMIN_TOKEN)
    admin_auth._keyring_cache.clear()
    yield
    admin_auth._keyring_cache.clear()


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        ingestion=IngestionConfig(enabled=True, storage_dir=str(tmp_path / "ingestion")),
    )
    return TestClient(create_app(config))


def issue(client: TestClient, name: str) -> dict[str, str]:
    response = client.post("/api/v1/admin/client-tokens", json={"name": name}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return {"X-Admin-Token": response.json()["token"]}


def new_task(client: TestClient, headers: dict[str, str], key: str, **extra) -> str:
    response = client.post(
        "/api/v1/tasks", json={"idempotency_key": key, "content": {"prompt": "Hola"}, **extra}, headers=headers,
    )
    assert response.status_code in (200, 202), response.text
    return str(response.json()["task_id"])


# ----------------------------------------------------------------- almacén


@pytest.fixture
def store(tmp_path: Path):
    db = Database(tmp_path / "b.db")
    db.init_schema()
    yield ClientTokenStore(db)
    db.close()


def test_store_keeps_only_a_hash(store: ClientTokenStore, tmp_path: Path) -> None:
    record, token = store.create("knowledge-orchestrator")
    assert token.startswith(TOKEN_PREFIX) and len(token) > 40
    with closing(sqlite3.connect(tmp_path / "b.db")) as raw_db:
        raw = raw_db.execute("SELECT * FROM client_tokens").fetchall()
    assert raw and all(token not in str(row) for row in raw)
    assert record.token_prefix == token[: len(record.token_prefix)]
    authenticated = store.authenticate(token)
    assert authenticated is not None and authenticated.id == record.id


def test_store_rejects_bad_and_duplicate_names_and_honours_revocation(store: ClientTokenStore) -> None:
    with pytest.raises(ClientTokenError) as bad:
        store.create("con espacios")
    assert bad.value.code == "CLIENT_TOKEN_NAME_INVALID"
    record, token = store.create("athena")
    with pytest.raises(ClientTokenError) as taken:
        store.create("athena")
    assert taken.value.code == "CLIENT_TOKEN_NAME_TAKEN"
    store.revoke(record.id)
    assert store.authenticate(token) is None
    # Revocado, el nombre queda libre para rotar.
    _, rotated = store.create("athena")
    assert store.authenticate(rotated) is not None
    assert store.authenticate("no-es-un-token") is None
    assert store.authenticate(TOKEN_PREFIX + "inventado") is None
    with pytest.raises(KeyError):
        store.revoke("cli_inexistente")


def test_last_used_is_recorded(store: ClientTokenStore) -> None:
    record, token = store.create("chatygpt")
    assert record.last_used_at is None
    store.authenticate(token)
    assert store.get(record.id).last_used_at is not None


# ------------------------------------------------------------- superficie


def test_client_routes_are_a_subset_of_the_authenticated_ones() -> None:
    from app.main import _CLIENT_API

    assert CLIENT_ROUTES <= AUTHENTICATED_ROUTES
    # La lista que aplica el broker y la que comprueba este test son la misma.
    assert _CLIENT_API == CLIENT_ROUTES


def test_a_malformed_body_on_an_admin_route_does_not_leak_fields_to_a_client(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        headers = issue(client, "app-a")
        leaked = client.patch("/api/v1/queue", headers=headers, json={"no": "vale"})
        assert leaked.status_code == 403 and "fields" not in leaked.text
        # En una ruta de cliente, el cliente sí ve el 422: es su contrato.
        own = client.post("/api/v1/tasks", headers=headers, json={"no": "vale"})
        assert own.status_code == 422 and own.json()["code"] == "CONTRACT_VALIDATION_FAILED"


def test_a_client_token_cannot_reach_any_admin_route(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        headers = issue(client, "app-a")
        admin_only = (registered_routes(client.app) & AUTHENTICATED_ROUTES) - CLIENT_ROUTES
        assert ("PATCH", "/api/v1/queue") in admin_only and ("GET", "/api/v1/usage") in admin_only
        for method, path in sorted(admin_only):
            target = path.replace("{task_id}", PATH_PARAM_SAMPLE).replace("{token_id}", PATH_PARAM_SAMPLE)
            response = client.request(method, target, headers=headers, json={})
            assert response.status_code == 403, (method, path, response.status_code)
        # Tampoco el panel: su cookie se firma con el token de administración.
        assert client.get("/dashboard", headers=headers, follow_redirects=False).status_code in (302, 303, 307, 403)


def test_auth_check_reports_the_scope(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        headers = issue(client, "app-a")
        assert client.get("/api/v1/auth/check", headers=headers).json() == {
            "authenticated": True, "auth_required": True, "scope": "client", "client_name": "app-a",
        }
        assert client.get("/api/v1/auth/check", headers=ADMIN).json()["scope"] == "admin"
        bearer = {"Authorization": "Bearer " + headers["X-Admin-Token"]}
        assert client.get("/api/v1/auth/check", headers=bearer).json()["scope"] == "client"
        assert client.get("/api/v1/auth/check", headers={"Authorization": "Basic x"}).status_code == 403
        assert client.get("/api/v1/capabilities").json()["client_tokens"] is True


def test_revoked_token_stops_working_immediately(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        headers = issue(client, "app-a")
        token_id = client.get("/api/v1/admin/client-tokens", headers=ADMIN).json()["items"][0]["id"]
        assert client.get("/api/v1/auth/check", headers=headers).status_code == 200
        revoked = client.delete(f"/api/v1/admin/client-tokens/{token_id}", headers=ADMIN)
        assert revoked.status_code == 200 and revoked.json()["active"] is False
        assert client.get("/api/v1/auth/check", headers=headers).status_code == 403
        assert client.delete("/api/v1/admin/client-tokens/cli_x", headers=ADMIN).status_code == 404


def test_listing_never_shows_the_token(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        secret = issue(client, "app-a")["X-Admin-Token"]
        listing = client.get("/api/v1/admin/client-tokens", headers=ADMIN)
        assert secret not in listing.text
        assert listing.json()["items"][0]["name"] == "app-a"
        dup = client.post("/api/v1/admin/client-tokens", json={"name": "app-a"}, headers=ADMIN)
        assert dup.status_code == 409 and dup.json()["detail"]["code"] == "CLIENT_TOKEN_NAME_TAKEN"


# ------------------------------------------------------------- aislamiento


def test_tasks_are_isolated_between_clients(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        a, b = issue(client, "app-a"), issue(client, "app-b")
        task = new_task(client, a, "informe-1")
        for path in (f"/api/v1/tasks/{task}", f"/api/v1/tasks/{task}/invocations", f"/api/v1/tasks/{task}/artifacts"):
            assert client.get(path, headers=a).status_code == 200, path
            denied = client.get(path, headers=b)
            assert denied.status_code == 404 and denied.json()["detail"] == "TASK_NOT_FOUND", path
        assert client.delete(f"/api/v1/tasks/{task}", headers=b).status_code == 404
        assert client.post(f"/api/v1/tasks/{task}/tool_results", headers=b,
                           json={"tool_results": []}).status_code in (404, 422)
        # El operador lo ve todo.
        assert client.get(f"/api/v1/tasks/{task}", headers=ADMIN).status_code == 200


def test_same_idempotency_key_in_two_clients_creates_two_tasks(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        a, b = issue(client, "app-a"), issue(client, "app-b")
        first = new_task(client, a, "clave-comun")
        assert new_task(client, a, "clave-comun") == first  # idempotente dentro del cliente
        other = new_task(client, b, "clave-comun")
        operator = new_task(client, ADMIN, "clave-comun")
        assert len({first, other, operator}) == 3


def test_groups_are_scoped_per_client(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        a, b = issue(client, "app-a"), issue(client, "app-b")
        new_task(client, a, "g-a-1", group="tanda")
        new_task(client, a, "g-a-2", group="tanda")
        new_task(client, b, "g-b-1", group="tanda")
        assert client.get("/api/v1/groups/tanda", headers=a).json()["total"] == 2
        assert client.get("/api/v1/groups/tanda", headers=b).json()["total"] == 1
        assert client.get("/api/v1/groups/tanda", headers=ADMIN).json()["total"] == 3
        assert client.get("/api/v1/groups/solo-de-a", headers=b).status_code == 404


def test_dependencies_only_see_the_same_owner(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        a, b = issue(client, "app-a"), issue(client, "app-b")
        foreign = new_task(client, a, "ajena", group="comun")
        waiting = new_task(client, b, "espera", depends_on=[foreign], depends_on_group="comun")
        repo = client.app.state.repository
        pending = repo.pending_dependencies(waiting, [foreign], "comun")
        # La tarea de otro cliente no existe para esta: no bloquea ni filtra su estado.
        assert pending == {"unknown_tasks": [foreign]}
        own = new_task(client, b, "propia", group="comun")
        assert repo.pending_dependencies(waiting, [own], "comun") == {
            "tasks": [own], "group": "comun", "group_pending": 1,
        }


def test_files_are_isolated_and_not_deduplicated_across_clients(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        a, b = issue(client, "app-a"), issue(client, "app-b")
        body = b"# Notas privadas\n\nTexto de la app A.\n"
        up_a = client.post("/api/v1/files", files={"file": ("notas.md", body, "text/markdown")}, headers=a)
        assert up_a.status_code == 202, up_a.text
        file_a = up_a.json()["file_id"]
        assert client.get(f"/api/v1/files/{file_a}", headers=a).status_code == 200
        assert client.get(f"/api/v1/files/{file_a}", headers=b).status_code == 404
        assert client.get(f"/api/v1/files/{file_a}/markdown", headers=b).status_code == 404
        # Mismo contenido subido por B: fichero propio, no el de A.
        up_b = client.post("/api/v1/files", files={"file": ("notas.md", body, "text/markdown")}, headers=b)
        assert up_b.json()["file_id"] != file_a and up_b.json()["created"] is True
        # Dentro del mismo cliente sí se deduplica, como siempre.
        again = client.post("/api/v1/files", files={"file": ("notas.md", body, "text/markdown")}, headers=a)
        assert again.json()["file_id"] == file_a and again.json()["created"] is False
        # Y B no puede adjuntar el fichero de A a una tarea.
        attach = client.post("/api/v1/tasks", headers=b, json={
            "idempotency_key": "robo", "content": {"prompt": "resume",
                                                   "attachments": [{"type": "broker_file",
                                                                    "metadata": {"file_id": file_a}}]},
        })
        assert attach.status_code == 404 and attach.json()["detail"] == "ATTACHED_FILE_NOT_FOUND"


def test_a_broker_without_admin_token_stays_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_BROKER_ADMIN_TOKEN")
    admin_auth._keyring_cache.clear()
    with make_client(tmp_path) as client:
        check = client.get("/api/v1/auth/check")
        assert check.status_code == 200 and check.json() == {
            "authenticated": True, "auth_required": False, "scope": "admin", "client_name": None,
        }


# ------------------------------------------------------------- consola


def test_console_script_creates_lists_and_revokes(tmp_path: Path, capsys) -> None:
    from scripts import client_tokens as cli

    with make_client(tmp_path) as client:
        client.headers.update(ADMIN)
        assert cli.main(["crear", "athena"], http=client) == 0
        created = capsys.readouterr().out
        token = next(word for word in created.split() if word.startswith(TOKEN_PREFIX))
        assert client.get("/api/v1/auth/check", headers={"X-Admin-Token": token}).json()["scope"] == "client"
        assert cli.main(["listar"], http=client) == 0
        listing = capsys.readouterr().out
        assert "athena" in listing and token not in listing and "activo" in listing
        token_id = listing.split()[0]
        assert cli.main(["revocar", token_id], http=client) == 0
        assert client.get("/api/v1/auth/check", headers={"X-Admin-Token": token}).status_code == 403
        assert cli.main(["crear", "con espacios"], http=client) == 1
