"""Tokens por cliente: credenciales con alcance limitado para las apps.

Hasta ahora todas las apps del ecosistema llamaban al broker con el token de
administración, que da control total: configuración, borrado de modelos,
reordenar la cola, panel. AI_OS_core lo repartía a cualquier app con token de
Core, así que el aislamiento entre apps de Core no llegaba al broker
(AOS-03 del plan del 13-nov-2026).

Un token de cliente solo abre las rutas de trabajo (crear y leer tareas,
ficheros, artefactos, grupos, el juez System-1) y solo sobre lo que ese
cliente creó. Lo emite y lo revoca el operador con el token de administración
(`/api/v1/admin/client-tokens`).

Se guarda el SHA-256 del token, nunca el token: tiene 256 bits de entropía,
así que un hash sin sal basta, y quien lea la base no puede hacerse pasar por
un cliente. El token solo se ve una vez, al crearlo.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from app.db import Database

#: Prefijo de todo token de cliente. Permite descartar sin consultar la base
#: lo que no puede ser uno (el token de administración, por ejemplo) y que un
#: escáner de secretos lo reconozca si alguien lo sube a un repositorio.
TOKEN_PREFIX = "abk_"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Cada cuánto se actualiza `last_used_at` como mucho: sin esto, cada sondeo de
# estado de un cliente sería una escritura en SQLite.
_TOUCH_INTERVAL = timedelta(seconds=60)


class ClientTokenError(ValueError):
    """Petición de gestión de tokens inválida (nombre mal formado o repetido)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ClientToken:
    id: str
    name: str
    token_prefix: str
    created_at: str
    revoked_at: str | None
    last_used_at: str | None


@dataclass(frozen=True)
class Principal:
    """Quién hace la petición: el operador o un cliente concreto."""

    kind: Literal["admin", "client"]
    client_id: str | None = None
    name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"


ADMIN = Principal("admin")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(row) -> ClientToken:
    return ClientToken(
        id=str(row["id"]), name=str(row["name"]), token_prefix=str(row["token_prefix"]),
        created_at=str(row["created_at"]), revoked_at=row["revoked_at"], last_used_at=row["last_used_at"],
    )


class ClientTokenStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, name: str) -> tuple[ClientToken, str]:
        """Emite un token nuevo. Devuelve (registro, token en claro)."""
        if not NAME_PATTERN.match(name):
            raise ClientTokenError(
                "CLIENT_TOKEN_NAME_INVALID", "El nombre admite 1-64 caracteres: letras, cifras, '.', '_' y '-'.",
            )
        active = self.db.query_one(
            "SELECT id FROM client_tokens WHERE name = ? AND revoked_at IS NULL", (name,),
        )
        if active is not None:
            # Dos tokens vivos con el mismo nombre harían imposible saber, en el
            # panel o en un log, qué app hizo qué. Para rotar: revocar y crear.
            raise ClientTokenError(
                "CLIENT_TOKEN_NAME_TAKEN", f"Ya hay un token activo llamado {name!r}: revócalo antes de crear otro.",
            )
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = f"cli_{uuid4().hex[:16]}"
        now = _now().isoformat()
        self.db.execute(
            "INSERT INTO client_tokens (id, name, token_hash, token_prefix, created_at) VALUES (?, ?, ?, ?, ?)",
            (token_id, name, _hash(token), token[: len(TOKEN_PREFIX) + 6], now),
        )
        return self.get(token_id), token

    def get(self, token_id: str) -> ClientToken:
        row = self.db.query_one("SELECT * FROM client_tokens WHERE id = ?", (token_id,))
        if row is None:
            raise KeyError(token_id)
        return _row(row)

    def list(self) -> list[ClientToken]:
        rows = self.db.query_all("SELECT * FROM client_tokens ORDER BY created_at DESC")
        return [_row(row) for row in rows]

    def revoke(self, token_id: str) -> ClientToken:
        """Revoca un token. Es definitivo: un token revocado no se reactiva."""
        current = self.get(token_id)
        if current.revoked_at is None:
            self.db.execute(
                "UPDATE client_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now().isoformat(), token_id),
            )
        return self.get(token_id)

    def authenticate(self, supplied: str | None) -> ClientToken | None:
        """El token vivo que corresponde a `supplied`, o None."""
        if not supplied or not supplied.startswith(TOKEN_PREFIX):
            return None
        row = self.db.query_one(
            "SELECT * FROM client_tokens WHERE token_hash = ? AND revoked_at IS NULL", (_hash(supplied),),
        )
        if row is None:
            return None
        token = _row(row)
        now = _now()
        last = datetime.fromisoformat(token.last_used_at) if token.last_used_at else None
        if last is None or now - last >= _TOUCH_INTERVAL:
            self.db.execute("UPDATE client_tokens SET last_used_at = ? WHERE id = ?", (now.isoformat(), token.id))
        return token
