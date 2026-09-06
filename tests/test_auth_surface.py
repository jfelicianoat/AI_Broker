"""Inventario explícito de qué responde el broker sin credencial admin.

Existe porque el reparto abierto/protegido solo vivía en la cabeza de quien
escribía cada endpoint: una ruta nueva declarada sin `request: Request` nacía
pública y ningún test se quejaba. Aquí toda ruta registrada tiene que estar
clasificada a mano, así que añadir una obliga a decidir de qué lado cae.

El caso real que lo motivó: una pantalla de conexión validaba su token contra
`/health` —abierto a propósito— y daba por bueno cualquier cadena inventada.
El broker no estaba roto, pero no ofrecía ningún endpoint contra el que
comprobar una credencial; de ahí `/api/v1/auth/check`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.admin_auth as admin_auth
from app.config import BrokerConfig, PersistenceConfig, ProcessingConfig
from app.main import create_app

ADMIN_TOKEN = "secreto-inventario"

# Rutas alcanzables sin credencial. Todas devuelven metadatos —estado,
# catálogo, forma del contrato—: ninguna expone prompts, resultados, gasto ni
# inventario de hardware. Mover algo a esta lista es una decisión de
# exposición, no un detalle de implementación.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    # Solo ids, estados y posiciones (véase el comentario en main.py).
    ("GET", "/api/v1/queue"),
    ("GET", "/api/v1/capabilities"),
    ("GET", "/api/v1/models"),
    ("GET", "/api/v1/models/availability"),
    ("GET", "/api/v1/models/context"),
}

# Rutas que con token configurado exigen credencial.
AUTHENTICATED_ROUTES = {
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
    ("PATCH", "/api/v1/queue"),
    ("POST", "/api/v1/dispatcher/tick"),
    ("POST", "/api/v1/dispatcher/ingestion/tick"),
    ("GET", "/api/v1/usage"),
    ("GET", "/api/v1/dashboard/summary"),
    ("GET", "/api/v1/dashboard/tasks"),
    ("GET", "/api/v1/dashboard/tasks/{task_id}"),
    ("GET", "/api/v1/dashboard/resources"),
    ("GET", "/api/v1/dashboard/residency"),
    ("GET", "/api/v1/auth/check"),
    # Documentación autogenerada. FastAPI la sirve abierta; aquí no. El
    # esquema enumera cada ruta, parámetro y modelo del broker, y eso es más
    # de lo que necesita saber quien no puede usarlo.
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
}

# Las dos únicas rutas del panel sin el guard: hay que poder ver el formulario
# de login y enviarlo sin estar autenticado.
DASHBOARD_PUBLIC_ROUTES = {
    ("GET", "/dashboard/login"),
    ("POST", "/dashboard/actions/login"),
}

DASHBOARD_GUARD = "_require_dashboard_access"

# Valores de relleno para los parámetros de ruta: da igual que no existan,
# porque la credencial se comprueba antes de buscarlos.
PATH_PARAM_SAMPLE = "no-existe"


def make_client(tmp_path: Path) -> TestClient:
    config = BrokerConfig(
        persistence=PersistenceConfig(database=str(tmp_path / "broker.db")),
        processing=ProcessingConfig(auto_dispatch=False, provider_mode="bootstrap"),
    )
    return TestClient(create_app(config))


@pytest.fixture(autouse=True)
def clean_keyring_cache(monkeypatch):
    """Aísla los tests del llavero real de la máquina y de la caché global.

    `resolve_admin_token` cachea el resultado del backend 30 segundos en una
    variable de módulo: sin limpiarla, un test contamina al siguiente.
    """
    monkeypatch.setattr("keyring.get_password", lambda *args, **kwargs: None)
    admin_auth._keyring_cache.clear()
    yield
    admin_auth._keyring_cache.clear()


def registered_routes(app) -> set[tuple[str, str]]:
    """(método, plantilla de ruta) de todo lo registrado, routers incluidos.

    Los routers incluidos no se aplanan en `app.routes`: quedan envueltos y
    hay que bajar por `original_router` para verlos.
    """
    found: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            methods = getattr(route, "methods", None)
            if not methods:
                continue
            for method in methods - {"HEAD", "OPTIONS"}:
                found.add((method, route.path))

    walk(app.routes)
    return found


def dashboard_routes_without_guard(app) -> set[tuple[str, str]]:
    unguarded: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            methods = getattr(route, "methods", None)
            if not methods or not route.path.startswith("/dashboard"):
                continue
            names = {
                getattr(dep.dependency, "__name__", None)
                for dep in getattr(route, "dependencies", [])
            }
            if DASHBOARD_GUARD in names:
                continue
            for method in methods - {"HEAD", "OPTIONS"}:
                unguarded.add((method, route.path))

    walk(app.routes)
    return unguarded


def test_every_route_is_classified(tmp_path: Path) -> None:
    """Ninguna ruta puede existir sin estar en una de las dos listas.

    Este es el test que impide que un endpoint nuevo nazca abierto en
    silencio: si no aparece clasificado, falla aquí antes de desplegarse.
    """
    with make_client(tmp_path) as client:
        registered = registered_routes(client.app)

    classified = PUBLIC_ROUTES | AUTHENTICATED_ROUTES | DASHBOARD_PUBLIC_ROUTES
    dashboard_guarded = {
        route for route in registered
        if route[1].startswith("/dashboard") and route not in DASHBOARD_PUBLIC_ROUTES
    }

    sin_clasificar = registered - classified - dashboard_guarded
    assert not sin_clasificar, (
        "Rutas sin clasificar: decide si exigen credencial y añádelas a "
        f"PUBLIC_ROUTES o AUTHENTICATED_ROUTES: {sorted(sin_clasificar)}"
    )
    obsoletas = classified - registered
    assert not obsoletas, f"Rutas clasificadas que ya no existen: {sorted(obsoletas)}"
    assert not (PUBLIC_ROUTES & AUTHENTICATED_ROUTES)


def test_dashboard_routes_are_guarded_by_construction(tmp_path: Path) -> None:
    """El panel se protege por router, no endpoint a endpoint.

    Registrar una vista nueva en el router `public` en vez de en `protected`
    la deja sin guard, y eso es lo que se detecta aquí.
    """
    with make_client(tmp_path) as client:
        unguarded = dashboard_routes_without_guard(client.app)

    assert unguarded == DASHBOARD_PUBLIC_ROUTES


def test_public_routes_never_demand_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", ADMIN_TOKEN)
    with make_client(tmp_path) as client:
        for method, path in sorted(PUBLIC_ROUTES):
            response = client.request(method, path)
            assert response.status_code not in (401, 403), (
                f"{method} {path} exige credencial pero está declarada pública"
            )


def test_authenticated_routes_never_serve_anonymous_requests(
    tmp_path: Path, monkeypatch,
) -> None:
    """Con token configurado, ninguna ruta protegida devuelve 2xx sin credencial.

    Se comprueba la propiedad de seguridad (nunca 2xx) y no un código exacto
    porque en POST/PATCH la validación del cuerpo corre antes que el handler:
    una petición anónima con cuerpo vacío recibe 422, no 403. Eso confunde al
    auditar, pero no filtra nada.
    """
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", ADMIN_TOKEN)
    with make_client(tmp_path) as client:
        for method, path in sorted(AUTHENTICATED_ROUTES):
            target = (
                path.replace("{task_id}", PATH_PARAM_SAMPLE)
                .replace("{artifact_id}", PATH_PARAM_SAMPLE)
                .replace("{file_id}", PATH_PARAM_SAMPLE)
                .replace("{group}", PATH_PARAM_SAMPLE)
            )
            for headers in ({}, {"X-Admin-Token": "token-inventado"}):
                response = client.request(method, target, headers=headers)
                assert not response.is_success, (
                    f"{method} {target} respondió {response.status_code} sin credencial válida"
                )


def test_protected_reads_answer_403_before_looking_anything_up(
    tmp_path: Path, monkeypatch,
) -> None:
    """Las lecturas protegidas responden 403, nunca 404.

    Un 404 significaría que el handler buscó el recurso antes de comprobar la
    credencial, y eso ya revela si un id existe.
    """
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", ADMIN_TOKEN)
    lecturas = [
        (method, path) for method, path in AUTHENTICATED_ROUTES
        if method in {"GET", "DELETE"}
    ]
    with make_client(tmp_path) as client:
        for method, path in sorted(lecturas):
            target = (
                path.replace("{task_id}", PATH_PARAM_SAMPLE)
                .replace("{artifact_id}", PATH_PARAM_SAMPLE)
                .replace("{file_id}", PATH_PARAM_SAMPLE)
                .replace("{group}", PATH_PARAM_SAMPLE)
            )
            response = client.request(method, target)
            assert response.status_code == 403, f"{method} {target} -> {response.status_code}"
            assert response.json()["detail"] == "ADMIN_AUTH_REQUIRED"


def test_auth_check_accepts_the_configured_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", ADMIN_TOKEN)
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/auth/check", headers={"X-Admin-Token": ADMIN_TOKEN})

        assert response.status_code == 200
        assert response.json() == {"authenticated": True, "auth_required": True}


def test_auth_check_rejects_an_invented_token(tmp_path: Path, monkeypatch) -> None:
    """El fallo original: una cadena inventada tiene que dar 403 en algún sitio."""
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", ADMIN_TOKEN)
    with make_client(tmp_path) as client:
        inventado = client.get("/api/v1/auth/check", headers={"X-Admin-Token": "me-lo-invento"})
        anonimo = client.get("/api/v1/auth/check")

        assert inventado.status_code == 403
        assert anonimo.status_code == 403
        assert inventado.json()["detail"] == "ADMIN_AUTH_REQUIRED"


def test_auth_check_says_when_the_broker_asks_for_no_credential(
    tmp_path: Path, monkeypatch,
) -> None:
    """Sin token configurado responde 200 pero lo declara.

    Es la distinción que le faltaba al cliente: "este broker no pide
    credencial" no es lo mismo que "tu token vale", y sin `auth_required` las
    dos se ven igual desde fuera.
    """
    monkeypatch.delenv("AI_BROKER_ADMIN_TOKEN", raising=False)
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/auth/check", headers={"X-Admin-Token": "da-igual"})

        assert response.status_code == 200
        assert response.json() == {"authenticated": True, "auth_required": False}


def test_auth_check_accepts_the_dashboard_session_cookie(tmp_path: Path, monkeypatch) -> None:
    """Vale la cookie del panel, no solo la cabecera: es la misma credencial."""
    monkeypatch.setenv("AI_BROKER_ADMIN_TOKEN", ADMIN_TOKEN)
    with make_client(tmp_path) as client:
        client.cookies.set(
            admin_auth.ADMIN_COOKIE_NAME, admin_auth.admin_cookie_value(ADMIN_TOKEN),
        )
        response = client.get("/api/v1/auth/check")

        assert response.status_code == 200
        assert response.json()["auth_required"] is True
