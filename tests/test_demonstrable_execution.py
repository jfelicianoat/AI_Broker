"""Contrato 2.10: que un cliente pueda DEMOSTRAR lo que ocurrió, no deducirlo.

El caso que lo motiva es un worker autónomo que ejecuta tarjetas sin nadie
delante y tiene que rendir cuentas después. Cuatro cosas no eran demostrables:

- Qué invocaciones eran suyas y cuáles trabajo propio del broker: `role` y
  `status` viajaban como texto libre, así que separarlas exigía una lista negra
  de nombres a mano que cualquier rol nuevo rompía en silencio.
- Que solo el modelo aprobado viera el contenido: una tarea con modelo exacto y
  `fallback_allowed: false` se sondeaba igual en otro modelo, bajo su task_id.
- Que `prompt_compression: "off"` se respetara: se podía probar lo que se pidió
  y nunca lo que se cumplió.
- Cuál de los ficheros de `/artifacts` es el entregable.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig, ShadowProbeConfig
from app.db import Database
from app.main import create_app
from app.repository import TaskRepository
from app.schemas import (
    AUXILIARY_INVOCATION_ROLES,
    INVOCATION_ROLES,
    ModelReference,
    invocation_is_contractual,
)


def make_client(tmp_path: Path, **config_extra) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        **config_extra,
    )
    return TestClient(create_app(config))


def _run(client: TestClient, **payload) -> dict:
    created = client.post("/api/v1/tasks", json={
        "idempotency_key": payload.pop("idempotency_key", "demo:1"),
        "content": {"prompt": payload.pop("prompt", "Hola")},
        **payload,
    }).json()
    client.post("/api/v1/dispatcher/tick")
    return created


# --------------------------------------------------------------------------
# Petición 1 — Vocabulario cerrado y marca de trabajo contractual
# --------------------------------------------------------------------------


def test_role_and_status_are_enumerated_in_the_openapi(tmp_path: Path) -> None:
    """Sin `enum` no hay forma de validar el valor contra el contrato: era la
    razón por la que un cliente acababa manteniendo su propia lista."""
    with make_client(tmp_path) as client:
        schema = client.get("/openapi.json").json()

    item = schema["components"]["schemas"]["TaskInvocationItem"]["properties"]
    assert set(item["role"]["enum"]) == INVOCATION_ROLES
    assert set(item["status"]["enum"]) == {"started", "completed", "failed", "ambiguous", "unknown"}
    assert item["contractual"]["type"] == "boolean"


def test_every_role_the_broker_writes_is_part_of_the_enum() -> None:
    """El enum se queda corto en cuanto alguien añade un rol y no lo declara:
    entonces la telemetría lo reporta como "unknown" y el cliente pierde
    exactamente la información que este contrato le prometió."""
    from app.coordinator import ROLE_SYSTEM_PROMPTS
    from app.ingestion.service import VISION_ROLE
    from app.shadow_probe import SHADOW_ROLE

    escritos = {
        "single", "agent", "arbiter", "chunk_map", "chunk_reduce", "confidence_judge",
        VISION_ROLE, SHADOW_ROLE, *ROLE_SYSTEM_PROMPTS,
    }
    self_reported = INVOCATION_ROLES - {"unknown"}
    assert escritos == self_reported


def test_the_shadow_probe_is_the_only_non_contractual_role() -> None:
    assert AUXILIARY_INVOCATION_ROLES == {"shadow_probe"}
    assert invocation_is_contractual("single")
    assert invocation_is_contractual("confidence_judge")
    assert not invocation_is_contractual("shadow_probe")


def test_an_unknown_future_role_is_reported_without_breaking_telemetry(tmp_path: Path) -> None:
    """Una fila escrita por una versión con más vocabulario (una vuelta atrás
    sobre la misma BD) no puede tumbar la telemetría con un 500."""
    db = Database(tmp_path / "futuro.db")
    db.init_schema()
    db.execute(
        "INSERT INTO tasks (id, request_json, status, created_at, updated_at) "
        "VALUES ('t', '{}', 'completed', '2026-01-01', '2026-01-01')"
    )
    db.execute(
        "INSERT INTO model_invocations (id, task_id, role, provider, deployment, model, "
        "task_type, status, tokens_input, tokens_output, cost_usd, created_at, updated_at) "
        "VALUES ('inv_1', 't', 'rol_del_futuro', 'ollama', 'local', 'm', 'prose', "
        "'estado_del_futuro', 1, 1, 0, '2026-01-01', '2026-01-01')"
    )

    item = TaskRepository(db).list_task_invocations("t").items[0]
    db.close()

    assert item.role == "unknown"
    assert item.status == "unknown"
    # Se declara contractual: sobrefacturar es visible, ocultar trabajo que
    # procesó el contenido del cliente no lo es.
    assert item.contractual is True
    # El rol crudo sigue disponible donde no está enumerado.
    assert item.model.role == "rol_del_futuro"


def test_a_completed_task_reports_its_invocation_as_contractual(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = _run(client, idempotency_key="demo:contractual")
        items = client.get(f"/api/v1/tasks/{created['task_id']}/invocations").json()["items"]

    assert [item["role"] for item in items] == ["single"]
    assert all(item["contractual"] for item in items)


# --------------------------------------------------------------------------
# Petición 2 — Solo el modelo aprobado ve el contenido
# --------------------------------------------------------------------------


def test_capabilities_declare_whether_auxiliary_invocations_exist(tmp_path: Path) -> None:
    """El cliente con tarjetas estrictas necesita saberlo ANTES de encolar, no
    al leer la telemetría de una tarea que ya se ejecutó."""
    with make_client(tmp_path) as client:
        apagado = client.get("/api/v1/capabilities").json()
    with make_client(
        tmp_path / "otro", shadow_probe=ShadowProbeConfig(enabled=True),
    ) as client:
        encendido = client.get("/api/v1/capabilities").json()

    assert apagado["auxiliary_invocations"] is False
    assert encendido["auxiliary_invocations"] is True
    # El opt-out existe siempre: "el broker las hace" y "puedo impedirlo" son
    # dos preguntas distintas.
    assert apagado["auxiliary_invocations_optout"] is True
    assert encendido["auxiliary_invocations_optout"] is True


def test_the_opt_out_travels_in_the_request_without_breaking_the_default(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        creada = client.post("/api/v1/tasks", json={
            "idempotency_key": "demo:optout",
            "content": {"prompt": "Hola"},
            "auxiliary_invocations": False,
        })
        por_defecto = client.post("/api/v1/tasks", json={
            "idempotency_key": "demo:default", "content": {"prompt": "Hola"},
        })

    assert creada.status_code == 202
    assert por_defecto.status_code == 202


# --------------------------------------------------------------------------
# Petición 3 — El eco de la compresión efectiva
# --------------------------------------------------------------------------


def test_each_invocation_echoes_the_compression_it_applied(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = _run(client, idempotency_key="demo:compresion", prompt_compression="off")
        item = client.get(
            f"/api/v1/tasks/{created['task_id']}/invocations"
        ).json()["items"][0]

    assert item["prompt_compression"] == {"requested": "off", "effective": "off"}


def test_a_task_that_says_nothing_echoes_broker_default(tmp_path: Path) -> None:
    """`requested` distingue "no pedí nada" de "pedí y no se me aplicó"."""
    with make_client(tmp_path) as client:
        created = _run(client, idempotency_key="demo:sin-pedir")
        item = client.get(
            f"/api/v1/tasks/{created['task_id']}/invocations"
        ).json()["items"][0]

    assert item["prompt_compression"]["requested"] == "broker_default"


def test_capabilities_announce_the_echo(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        body = client.get("/api/v1/capabilities").json()

    assert body["contract_version"] == "2.10"
    assert body["prompt_compression_echo"] is True
    assert body["invocation_contract"] is True
    assert body["canonical_artifacts"] is True


# --------------------------------------------------------------------------
# Petición 4 — Cuál de los artefactos es el entregable
# --------------------------------------------------------------------------


def test_the_deliverable_artifact_is_marked_final(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = _run(client, idempotency_key="demo:artefacto")
        items = client.get(f"/api/v1/tasks/{created['task_id']}/artifacts").json()["items"]
        estado = client.get(f"/api/v1/tasks/{created['task_id']}").json()

    finales = [item for item in items if item["final"]]
    assert len(finales) == 1
    assert finales[0]["artifact_type"] == "single_output"
    assert finales[0]["sha256"]
    # Y es el mismo texto que la comodidad sin contrato de `result`.
    with make_client(tmp_path) as client:
        descarga = client.get(finales[0]["download_url"])
    assert descarga.text == estado["result"]["assistant_content"]


def test_an_image_is_listed_but_is_not_the_deliverable(tmp_path: Path) -> None:
    """`image_output` acompaña a la respuesta; no la sustituye. Sin la marca,
    un cliente que cerrase con "el primer artefacto" cerraría con el PNG."""
    from app.schemas import FINAL_ARTIFACT_TYPES

    assert "image_output" not in FINAL_ARTIFACT_TYPES
    assert FINAL_ARTIFACT_TYPES == {
        "single_output", "agent_output", "synthesis_output", "embedding_output",
    }


def test_every_final_artifact_type_is_one_the_coordinator_writes() -> None:
    """La lista se queda obsoleta en silencio si alguien añade una estrategia y
    no la declara: entonces su entregable sale sin `final` y el cliente cierra
    la tarjeta sin nada."""
    fuente = Path("app/coordinator.py").read_text(encoding="utf-8")
    from app.schemas import FINAL_ARTIFACT_TYPES

    for artifact_type in FINAL_ARTIFACT_TYPES:
        assert f'"{artifact_type}"' in fuente, artifact_type


# --------------------------------------------------------------------------
# Petición 5 — De dónde saca la credencial un proceso co-ubicado
# --------------------------------------------------------------------------


def test_the_session_token_is_published_where_local_clients_read_it(monkeypatch) -> None:
    """El runner arranca con la máquina (Wake-on-LAN): no ve la consola del
    broker ni hereda su entorno, y el token no puede cruzar la red."""
    from app.admin_auth import publish_session_admin_token
    from app.config import ServerConfig

    escrito: dict[str, str] = {}

    class _Keyring:
        @staticmethod
        def set_password(service: str, username: str, password: str) -> None:
            escrito[f"{service}/{username}"] = password

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Keyring)
    config = BrokerConfig(server=ServerConfig(publish_session_token="keyring"))

    username = publish_session_admin_token(config, "token-de-esta-sesion")

    assert username == "session_admin_token"
    assert escrito == {"ai-broker/session_admin_token": "token-de-esta-sesion"}


def test_publishing_never_touches_the_operator_entry(monkeypatch) -> None:
    """La entrada que el broker LEE como fallback es del operador, para fijar un
    token estable. Pisarla convertiría el token efímero en permanente."""
    from app.admin_auth import publish_session_admin_token
    from app.config import ServerConfig

    escrito: dict[str, str] = {}

    class _Keyring:
        @staticmethod
        def set_password(service: str, username: str, password: str) -> None:
            escrito[f"{service}/{username}"] = password

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Keyring)
    config = BrokerConfig(server=ServerConfig(publish_session_token="keyring"))

    publish_session_admin_token(config, "efimero")

    assert config.server.admin_keyring_username == "dashboard_admin_token"
    assert "ai-broker/dashboard_admin_token" not in escrito


def test_publishing_is_off_unless_the_operator_asks_for_it(monkeypatch) -> None:
    from app.admin_auth import publish_session_admin_token

    class _Keyring:
        @staticmethod
        def set_password(service: str, username: str, password: str) -> None:
            raise AssertionError("no debería escribir nada")

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Keyring)

    assert publish_session_admin_token(BrokerConfig(), "efimero") is None


def test_a_broken_keyring_does_not_stop_the_broker(monkeypatch, tmp_path: Path) -> None:
    """El broker sirve igual y quien está delante tiene el token en consola; lo
    que no puede es pasar callando, porque el síntoma en el otro extremo son
    403 sin explicación."""
    import logging

    from app.config import ServerConfig
    from app.startup import publish_admin_credential_for_local_clients

    class _Keyring:
        @staticmethod
        def get_password(service: str, username: str) -> str | None:
            return None

        @staticmethod
        def set_password(service: str, username: str, password: str) -> None:
            raise RuntimeError("almacen de credenciales no disponible")

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Keyring)
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "token-en-el-entorno")
    logger = logging.getLogger("test.publish")
    config = BrokerConfig(server=ServerConfig(publish_session_token="keyring"))

    from unittest.mock import patch

    with patch.object(logger, "warning") as warning:
        publish_admin_credential_for_local_clients(config, logger)

    warning.assert_called_once()
    assert warning.call_args.args[0] == "admin.session_token_publish_failed"


def test_the_published_token_is_the_one_the_broker_will_accept(monkeypatch) -> None:
    """Publicar un token distinto del que verifica `verify_admin_access` sería
    peor que no publicar ninguno: el cliente creería tener credencial."""
    import logging

    from app.admin_auth import resolve_admin_token
    from app.config import ServerConfig

    escrito: dict[str, str] = {}

    class _Keyring:
        @staticmethod
        def get_password(service: str, username: str) -> str | None:
            return None

        @staticmethod
        def set_password(service: str, username: str, password: str) -> None:
            escrito[username] = password

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Keyring)
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", "el-de-esta-sesion")
    from app.startup import publish_admin_credential_for_local_clients

    config = BrokerConfig(server=ServerConfig(publish_session_token="keyring"))
    publish_admin_credential_for_local_clients(config, logging.getLogger("test.publish"))

    assert escrito["session_admin_token"] == resolve_admin_token(config)


def test_a_model_reference_still_accepts_any_role() -> None:
    """`ModelReference.role` no está enumerado a propósito: lo rellena el
    enrutado con nombres de rol de consenso y ahí sí conviene ver lo que hay."""
    assert ModelReference(
        provider="ollama", deployment="local", model="m", role="lo-que-sea",
    ).role == "lo-que-sea"
