"""Resolución y verificación del token de administración (dashboard y API)."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time

from fastapi import HTTPException, Request

from app.config import BrokerConfig

ADMIN_COOKIE_NAME = "ai_broker_dashboard_admin"
ADMIN_SESSION_SECONDS = 60 * 60 * 8
_KEYRING_CACHE_SECONDS = 30.0

# Hosts en los que la API solo es alcanzable desde la propia máquina; fuera de
# esta lista el broker exige token admin (véase create_app) y un fallo del
# backend de credenciales deniega el acceso en vez de desactivar la auth.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class AdminTokenLookupError(RuntimeError):
    """El backend de credenciales (keyring) falló: no se sabe si hay token.

    Distinto de "no hay token configurado" (None). Quien la reciba debe
    fallar cerrado, nunca tratar el error como autenticación desactivada.
    """


class _KeyringTokenCache:
    """Evita consultar el backend de credenciales del SO en cada mutación."""

    def __init__(self, ttl_seconds: float = _KEYRING_CACHE_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._value: str | None = None
        self._expires_at = 0.0
        self._loaded = False

    def get(self) -> tuple[bool, str | None]:
        with self._lock:
            if self._loaded and time.monotonic() < self._expires_at:
                return True, self._value
            return False, None

    def set(self, value: str | None) -> None:
        with self._lock:
            self._value = value
            self._expires_at = time.monotonic() + self._ttl
            self._loaded = True

    def clear(self) -> None:
        with self._lock:
            self._loaded = False
            self._value = None
            self._expires_at = 0.0


_keyring_cache = _KeyringTokenCache()


def resolve_admin_token(config: BrokerConfig) -> str | None:
    """Devuelve el token admin desde env (siempre fresco) o keyring (con caché TTL).

    None significa "no hay token configurado" (decisión deliberada, válida en
    loopback). Si el backend de credenciales falla se lanza
    AdminTokenLookupError: un keyring roto no equivale a "sin token".
    """
    if config.server.admin_token_env:
        value = os.environ.get(config.server.admin_token_env)
        if value:
            return value
    hit, cached = _keyring_cache.get()
    if hit:
        return cached
    try:
        import keyring

        token = keyring.get_password(
            config.server.admin_keyring_service,
            config.server.admin_keyring_username,
        ) or None
    except Exception as error:
        # El fallo no se cachea: el siguiente intento vuelve a consultar el
        # backend por si se recupera.
        raise AdminTokenLookupError(f"backend de credenciales no disponible: {error}") from error
    _keyring_cache.set(token)
    return token


def admin_cookie_value(token: str, timestamp: float | None = None) -> str:
    """Cookie de sesión `ts.hmac(token, ts)`: expira server-side y no expone el token."""
    issued_at = int(timestamp if timestamp is not None else time.time())
    signature = hmac.new(token.encode("utf-8"), str(issued_at).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{issued_at}.{signature}"


def _verify_admin_cookie(cookie: str, token: str) -> bool:
    issued_str, _, signature = cookie.partition(".")
    if not issued_str.isdigit() or not signature:
        return False
    issued_at = int(issued_str)
    now = time.time()
    if now - issued_at > ADMIN_SESSION_SECONDS or issued_at > now + 300:
        return False
    expected = hmac.new(token.encode("utf-8"), issued_str.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_admin_access(request: Request, config: BrokerConfig) -> None:
    """Exige credencial admin cuando hay token configurado (env o keyring).

    Si el backend de credenciales falla: en loopback (o con el opt-out LAN
    explícito) se degrada a "sin token" porque la API solo es alcanzable
    localmente; en cualquier otro host se responde 503 — denegar es preferible
    a exponer la API sin auth por un keyring roto.
    """
    import secrets

    try:
        expected = resolve_admin_token(config)
    except AdminTokenLookupError as error:
        if config.server.host in LOOPBACK_HOSTS or config.server.allow_unauthenticated_lan:
            expected = None
        else:
            raise HTTPException(status_code=503, detail="ADMIN_AUTH_BACKEND_UNAVAILABLE") from error
    if not expected:
        return
    header_token = request.headers.get("x-admin-token")
    if header_token and secrets.compare_digest(header_token, expected):
        return
    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    if cookie and _verify_admin_cookie(cookie, expected):
        return
    raise HTTPException(status_code=403, detail="ADMIN_AUTH_REQUIRED")


class LoginThrottle:
    """Backoff exponencial de intentos de login fallidos por origen."""

    def __init__(
        self,
        max_free_failures: int = 5,
        base_delay_seconds: float = 2.0,
        max_delay_seconds: float = 300.0,
    ) -> None:
        self._max_free = max_free_failures
        self._base = base_delay_seconds
        self._cap = max_delay_seconds
        self._lock = threading.Lock()
        self._state: dict[str, tuple[int, float]] = {}

    def blocked_for(self, key: str) -> float:
        with self._lock:
            _, blocked_until = self._state.get(key, (0, 0.0))
            return max(0.0, blocked_until - time.monotonic())

    def record_failure(self, key: str) -> None:
        with self._lock:
            failures = self._state.get(key, (0, 0.0))[0] + 1
            if failures < self._max_free:
                delay = 0.0
            else:
                delay = min(self._cap, self._base * (2 ** (failures - self._max_free)))
            self._state[key] = (failures, time.monotonic() + delay)

    def reset(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)
