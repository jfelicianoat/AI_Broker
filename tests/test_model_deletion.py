"""Borrado de un modelo local desde el panel de Modelos.

Es la única acción irreversible de la interfaz, así que lo que más se prueba
aquí es lo que NO debe borrar: sin confirmación, con trabajo en curso, en un
proveedor sin API de borrado, o un modelo que no está en este disco.
"""
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig, ProvidersConfig
from app.main import create_app
from app.providers import OllamaProvider, ProviderError

_TAGS = {
    "models": [
        {"name": "grande:70b", "size": 42_000_000_000, "context_length": 8192,
         "capabilities": ["completion"]},
        {"name": "remoto:1b", "size": 0, "context_length": 8192,
         "capabilities": ["completion"], "remote_host": "ollama.com"},
    ]
}


def _ollama(handler) -> OllamaProvider:
    return OllamaProvider(BrokerConfig(), transport=httpx.MockTransport(handler))


def _handler(state: dict):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json=_TAGS)
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/generate":
            return httpx.Response(200, json={})
        if path == "/api/delete":
            state["deleted"] = request.method
            state["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return handle


def test_deleting_a_local_model_asks_ollama_and_reports_freed_space() -> None:
    """El borrado físico lo hace el runtime, no el broker: varios modelos
    comparten blobs y solo Ollama sabe cuáles quedan sin referenciar."""
    import asyncio

    state: dict = {}
    provider = _ollama(_handler(state))
    freed = asyncio.run(provider.delete_model("grande:70b"))
    asyncio.run(provider.close())

    assert state["deleted"] == "DELETE"
    assert "grande:70b" in state["body"]
    assert freed == 42_000_000_000


def test_a_cloud_model_is_not_deletable() -> None:
    """No ocupa disco en esta máquina: borrarlo no liberaría nada y sí quitaría
    el acceso."""
    import asyncio

    state: dict = {}
    provider = _ollama(_handler(state))
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.delete_model("remoto:1b"))
    asyncio.run(provider.close())

    assert error.value.code == "MODEL_NOT_LOCAL"
    assert "deleted" not in state


def test_an_unknown_model_is_not_deletable() -> None:
    import asyncio

    state: dict = {}
    provider = _ollama(_handler(state))
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.delete_model("no-existe:1b"))
    asyncio.run(provider.close())

    assert error.value.code == "MODEL_UNAVAILABLE"
    assert "deleted" not in state


def test_other_providers_are_refused_instead_of_deleting_files_by_hand() -> None:
    """LM Studio y compañía no tienen API de borrado. Adivinar rutas y borrar
    ficheros en el disco de alguien es exactamente lo que no debe hacer un
    servicio."""
    import asyncio

    from app.providers.routing import RoutedModelProvider

    routed = RoutedModelProvider(BrokerConfig())
    with pytest.raises(ProviderError) as error:
        asyncio.run(routed.delete_local_model("lmstudio", "algo"))
    asyncio.run(routed.close())
    assert error.value.code == "MODEL_DELETE_UNSUPPORTED"


# ------------------------------------------------------------------- el panel

def _client(tmp_path: Path, deleter=None) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        providers=ProvidersConfig(),
    )
    app = create_app(config, config_path=tmp_path / "broker_config.yaml")
    if deleter is not None:
        app.state.provider.delete_local_model = deleter
    return TestClient(app)


def _post_delete(client: TestClient, data: dict) -> httpx.Response:
    client.get("/dashboard/models")
    token = client.cookies.get("ai_broker_dashboard_csrf")
    return client.post(
        "/dashboard/actions/models/delete",
        headers={"X-CSRF-Token": token},
        data={"csrf_token": token, **data},
        follow_redirects=False,
    )


def test_the_first_click_only_asks(tmp_path: Path) -> None:
    """Sin `confirm` no se borra nada: se devuelve la pregunta, con el tamaño
    que se va a perder y el aviso de que no se puede deshacer."""
    calls: list[tuple[str, str]] = []

    async def deleter(provider_id: str, model: str) -> int:
        calls.append((provider_id, model))
        return 0

    with _client(tmp_path, deleter=deleter) as client:
        response = _post_delete(client, {"provider": "ollama", "model": "grande:70b"})

    assert response.status_code == 200
    assert "¿Borrar grande:70b del disco?" in response.text
    assert "No se puede deshacer" in response.text
    assert not calls, "el primer clic no puede borrar"


def test_the_confirmation_must_name_the_same_model(tmp_path: Path) -> None:
    """El campo `confirm` lleva el nombre: un formulario reenviado contra otro
    modelo no cuela."""
    calls: list[tuple[str, str]] = []

    async def deleter(provider_id: str, model: str) -> int:
        calls.append((provider_id, model))
        return 1

    with _client(tmp_path, deleter=deleter) as client:
        response = _post_delete(client, {
            "provider": "ollama", "model": "grande:70b", "confirm": "otro:1b",
        })

    assert response.status_code == 200  # vuelve a preguntar
    assert not calls


def test_the_second_click_deletes(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    async def deleter(provider_id: str, model: str) -> int:
        calls.append((provider_id, model))
        return 42_000_000_000

    with _client(tmp_path, deleter=deleter) as client:
        response = _post_delete(client, {
            "provider": "ollama", "model": "grande:70b", "confirm": "grande:70b",
        })

    assert response.status_code == 303
    assert calls == [("ollama", "grande:70b")]
    assert "model_delete=ok" in response.headers["location"]


def test_it_refuses_while_the_machine_is_working(tmp_path: Path) -> None:
    """El modelo que se va a borrar puede ser justo el que está respondiendo."""
    calls: list[str] = []

    async def deleter(provider_id: str, model: str) -> int:
        calls.append(model)
        return 1

    with _client(tmp_path, deleter=deleter) as client:
        created = client.post("/api/v1/tasks", json={
            "idempotency_key": "borrado:ocupado",
            "content": {"prompt": "trabajo en curso"},
        })
        assert created.status_code == 202
        response = _post_delete(client, {
            "provider": "ollama", "model": "grande:70b", "confirm": "grande:70b",
        })

    assert response.status_code == 303
    assert "model_delete=error" in response.headers["location"]
    assert not calls, "no se borra con trabajo en cola"


def test_the_confirmation_says_what_will_break(tmp_path: Path) -> None:
    """La pregunta no solo dice cuánto libera: dice qué deja de funcionar. Aquí
    el modelo es el único embedding local y además el que describe figuras."""
    from app.config import IngestionConfig, IngestionImagesConfig

    catalog = [
        {"name": "visor:7b", "provider": "ollama", "deployment": "local",
         "status": "available", "size_bytes": 5_000_000_000, "context_window": 8192,
         "capabilities": ["completion", "embedding"], "compatibility": "compatible"},
        {"name": "otro:8b", "provider": "ollama", "deployment": "local",
         "status": "available", "size_bytes": 1, "context_window": 8192,
         "capabilities": ["completion"], "compatibility": "compatible"},
    ]
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
        ingestion=IngestionConfig(images=IngestionImagesConfig(enabled=True, model="visor:7b")),
    )
    app = create_app(config, config_path=tmp_path / "broker_config.yaml")

    async def fake_models():
        return catalog

    app.state.provider.models = fake_models
    with TestClient(app) as client:
        response = _post_delete(client, {"provider": "ollama", "model": "visor:7b"})

    assert response.status_code == 200
    assert "Esto además romperá" in response.text
    assert "ingestion.images.model" in response.text
    assert "ÚNICO modelo local" in response.text


def test_the_confirmation_is_quiet_when_nothing_breaks(tmp_path: Path) -> None:
    """Sin consecuencias no se añade ruido: un aviso que sale siempre enseña a
    ignorarlo, y aquí eso sería caro."""
    catalog = [
        {"name": "chat:8b", "provider": "ollama", "deployment": "local",
         "status": "available", "size_bytes": 1, "context_window": 8192,
         "capabilities": ["completion"], "compatibility": "compatible"},
        {"name": "otro:8b", "provider": "ollama", "deployment": "local",
         "status": "available", "size_bytes": 1, "context_window": 8192,
         "capabilities": ["completion"], "compatibility": "compatible"},
    ]
    with _client(tmp_path) as client:
        async def fake_models():
            return catalog

        client.app.state.provider.models = fake_models
        response = _post_delete(client, {"provider": "ollama", "model": "chat:8b"})

    assert response.status_code == 200
    assert "¿Borrar chat:8b del disco?" in response.text
    assert "Esto además romperá" not in response.text


def test_only_local_ollama_models_show_the_button(tmp_path: Path) -> None:
    """El botón aparece donde el borrado es posible y honesto: Ollama local.
    Un modelo de Ollama Cloud no ocupa este disco, y LM Studio no tiene API de
    borrado."""
    catalog = [
        {"name": "grande:70b", "provider": "ollama", "deployment": "local",
         "status": "available", "size_bytes": 42_000_000_000, "context_window": 8192,
         "capabilities": ["completion"], "compatibility": "compatible"},
        {"name": "remoto:1b", "provider": "ollama", "deployment": "cloud",
         "status": "available", "size_bytes": 0, "context_window": 8192,
         "capabilities": ["completion"], "compatibility": "compatible"},
        {"name": "local-model", "provider": "lmstudio", "deployment": "local",
         "status": "available", "size_bytes": 1, "context_window": 8192,
         "capabilities": ["completion"], "compatibility": "compatible"},
    ]

    with _client(tmp_path) as client:
        async def fake_models():
            return catalog

        client.app.state.provider.models = fake_models
        page = client.get("/dashboard/models")

    assert page.status_code == 200
    body = page.text
    assert 'hx-vals=\'{"provider": "ollama", "model": "grande:70b"}\'' in body
    assert "remoto:1b" in body and '"model": "remoto:1b"' not in body
    assert "local-model" in body and '"model": "local-model"' not in body


def test_the_action_requires_the_csrf_token(tmp_path: Path) -> None:
    calls: list[str] = []

    async def deleter(provider_id: str, model: str) -> int:
        calls.append(model)
        return 1

    with _client(tmp_path, deleter=deleter) as client:
        client.get("/dashboard/models")
        response = client.post(
            "/dashboard/actions/models/delete",
            data={"provider": "ollama", "model": "grande:70b", "confirm": "grande:70b"},
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert not calls
