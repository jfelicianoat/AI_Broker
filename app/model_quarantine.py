"""Cuarentena de modelos que han dejado de producir salida usable.

Un modelo puede estar en el catálogo, cargar sin protestar y aun así no servir:
pesos corruptos, una cuantización que el runtime no ejecuta bien, una plantilla
rota. El síntoma es siempre el mismo —invocaciones que terminan en un error
definitivo— y el coste, alto: cada intento paga la carga del modelo entera
antes de fallar.

Aquí se decide, con la evidencia que ya deja `model_invocations`, qué modelos
salen de la rotación. La regla es deliberadamente conservadora en las dos
direcciones: hacen falta varios fallos seguidos para apartar a uno, y la
cuarentena caduca sola, para que un modelo reinstalado vuelva sin que nadie
tenga que acordarse de rehabilitarlo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import ModelQuarantineConfig
from app.db import Database

# Identidad sin task_type: un modelo que devuelve basura la devuelve para todo,
# a diferencia de la calidad, que sí depende del tipo de tarea.
QuarantineKey = tuple[str, str, str]


@dataclass(frozen=True)
class QuarantineEntry:
    """Por qué un modelo está apartado. El motivo viaja hasta el catálogo: un
    modelo que desaparece de la selección sin explicación es un misterio que se
    paga depurando."""

    failures: int
    last_code: str
    last_failure_at: str

    @property
    def reason(self) -> str:
        return (
            f"{self.failures} fallos seguidos sin ningún acierto "
            f"(último: {self.last_code} el {self.last_failure_at[:19].replace('T', ' ')})"
        )


def load_quarantine(
    db: Database, settings: ModelQuarantineConfig
) -> dict[QuarantineKey, QuarantineEntry]:
    """Modelos apartados ahora mismo, con el motivo de cada uno.

    Cuenta hacia atrás desde la invocación más reciente: un éxito, por antiguo
    que sea dentro de la ventana, corta la racha y devuelve al modelo a la
    rotación. Solo cuentan los códigos declarados como definitivos; quedarse
    sin memoria o un proveedor caído son circunstancias, no defectos.

    Tampoco cuenta el tráfico excluido del aprendizaje
    (TaskCreateRequest.exclude_from_model_learning): apartar un modelo del
    catálogo por fallar donde alguien estaba INTENTANDO que fallara sería
    castigarlo por hacer justo lo esperable. Ni condena ni absuelve: esas
    invocaciones no rompen una racha ni la alimentan.
    """
    if not settings.enabled:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=settings.window_hours)).isoformat()
    rows = db.query_all(
        """
        SELECT provider, deployment, model, status, error_code, created_at
        FROM model_invocations
        WHERE created_at >= ? AND status IN ('completed', 'failed')
          AND excluded_from_learning = 0
        ORDER BY created_at DESC, id DESC
        """,
        (cutoff,),
    )
    definitive = {code.upper() for code in settings.definitive_codes}
    streaks: dict[QuarantineKey, list] = {}
    settled: set[QuarantineKey] = set()
    for row in rows:
        key: QuarantineKey = (
            str(row["provider"]).lower(),
            str(row["deployment"]).lower(),
            str(row["model"]).lower(),
        )
        if key in settled:
            continue
        if row["status"] == "completed":
            # Racha rota: el modelo ha demostrado que sigue respondiendo.
            settled.add(key)
            streaks.pop(key, None)
            continue
        code = str(row["error_code"] or "").upper()
        if code not in definitive:
            # Un fallo circunstancial no absuelve ni condena: no rompe la racha
            # de fallos definitivos ni cuenta para ella.
            continue
        streaks.setdefault(key, []).append((code, str(row["created_at"])))

    return {
        key: QuarantineEntry(
            failures=len(failures), last_code=failures[0][0], last_failure_at=failures[0][1]
        )
        for key, failures in streaks.items()
        if len(failures) >= settings.consecutive_failures
    }


def quarantine_key(entry: dict) -> QuarantineKey:
    """Clave de una fila del catálogo, con el mismo criterio de minúsculas."""
    return (
        str(entry.get("provider") or "").lower(),
        str(entry.get("deployment") or "").lower(),
        str(entry.get("name") or "").lower(),
    )


# Roles del consenso agrupados por el trabajo que hacen. Los cinco nombres de
# proponente son la misma tarea con distinta instrucción; el árbitro lee las
# propuestas y sintetiza, que es otra cosa y falla de otra manera.
PROPOSER_ROLES = frozenset({
    "proposer", "generalist", "specialist", "skeptic", "analyst", "reviewer", "refiner",
})
ARBITER_ROLES = frozenset({"arbiter"})

# (provider, deployment, model, grupo de rol)
RoleKey = tuple[str, str, str, str]


def role_group(role: str | None) -> str | None:
    """`proposer`, `arbiter` o None para los roles que no son de consenso."""
    value = str(role or "").lower()
    if value in PROPOSER_ROLES:
        return "proposer"
    if value in ARBITER_ROLES:
        return "arbiter"
    return None


@dataclass(frozen=True)
class RoleUnfitness:
    """Por qué un modelo no se elige para un rol de consenso."""

    attempts: int
    failures: int
    last_code: str

    @property
    def failure_rate(self) -> float:
        return self.failures / self.attempts if self.attempts else 0.0

    @property
    def reason(self) -> str:
        return (
            f"{self.failures} de {self.attempts} invocaciones en este rol acabaron en un fallo "
            f"definitivo (último: {self.last_code})"
        )


def load_role_unfitness(
    db: Database, settings: ModelQuarantineConfig
) -> dict[RoleKey, RoleUnfitness]:
    """Modelos que no sirven para un rol de consenso aunque sirvan para lo demás.

    Existe porque la cuarentena mira al modelo entero y aquí el defecto es del
    rol. lfm2:24b acierta 384 de 395 veces en `single` y, como proponente,
    devuelve su propio prompt una de cada tres; nemotron:latest acierta casi
    siempre en `single` y como árbitro hace eco 16 de 21 veces. Los aciertos en
    `single` rompían la racha de la cuarentena, y el ranking —tiempo esperado
    por tipo de tarea, sin rol— los seguía poniendo primeros porque son rápidos.

    Cuenta solo los códigos definitivos de la cuarentena: un timeout o falta de
    memoria no dicen nada del modelo en ese rol. Tampoco cuenta el tráfico
    excluido del aprendizaje, por la misma razón que allí."""
    if not settings.enabled or settings.role_max_failure_rate >= 1.0:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=settings.role_window_hours)).isoformat()
    roles = sorted(PROPOSER_ROLES | ARBITER_ROLES)
    marks = ",".join("?" for _ in roles)
    rows = db.query_all(
        f"""
        SELECT provider, deployment, model, role, status, error_code
        FROM model_invocations
        WHERE created_at >= ? AND status IN ('completed', 'failed')
          AND excluded_from_learning = 0 AND role IN ({marks})
        ORDER BY created_at ASC, id ASC
        """,
        (cutoff, *roles),
    )
    definitive = {code.upper() for code in settings.definitive_codes}
    tallies: dict[RoleKey, list] = {}
    for row in rows:
        group = role_group(row["role"])
        if group is None:
            continue
        code = str(row["error_code"] or "").upper()
        if row["status"] == "failed" and code not in definitive:
            continue
        key: RoleKey = (
            str(row["provider"]).lower(), str(row["deployment"]).lower(),
            str(row["model"]).lower(), group,
        )
        tally = tallies.setdefault(key, [0, 0, ""])
        tally[0] += 1
        if row["status"] == "failed":
            tally[1] += 1
            tally[2] = code
    return {
        key: RoleUnfitness(attempts=attempts, failures=failures, last_code=last_code)
        for key, (attempts, failures, last_code) in tallies.items()
        if attempts >= settings.role_min_attempts
        and failures / attempts >= settings.role_max_failure_rate
    }


# (provider, deployment, model, task_type)
ArbiterRecordKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class ArbiterRecord:
    """Cómo le ha ido a un modelo como árbitro en un tipo de tarea."""

    attempts: int
    failures: int

    @property
    def mostly_failing(self) -> bool:
        """Más fallos definitivos que aciertos. Es un criterio de ORDEN, no de
        exclusión: basta un eco sin ningún acierto para dejar pasar delante a
        quien no tiene ese historial, sin vetar a nadie."""
        return self.failures > self.attempts - self.failures


def load_arbiter_record(db: Database, settings: ModelQuarantineConfig) -> dict[ArbiterRecordKey, ArbiterRecord]:
    """Historial de cada modelo como árbitro, por tipo de tarea.

    El árbitro se elige por tamaño declarado (`strongest_available`), y el
    tamaño no avisa de que ornith:35b devuelve su propio prompt al sintetizar
    apuntes de una transcripción larga. Por tipo de tarea porque el mismo
    modelo puede arbitrar bien respuestas cortas y hacer eco con 13 000 tokens
    de propuestas. Mismos filtros que load_role_unfitness: solo códigos
    definitivos, sin tráfico excluido del aprendizaje, misma ventana."""
    if not settings.enabled:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=settings.role_window_hours)).isoformat()
    roles = sorted(ARBITER_ROLES)
    marks = ",".join("?" for _ in roles)
    rows = db.query_all(
        f"""
        SELECT provider, deployment, model, task_type, status, error_code
        FROM model_invocations
        WHERE created_at >= ? AND status IN ('completed', 'failed')
          AND excluded_from_learning = 0 AND task_type IS NOT NULL AND role IN ({marks})
        """,
        (cutoff, *roles),
    )
    definitive = {code.upper() for code in settings.definitive_codes}
    tallies: dict[ArbiterRecordKey, list[int]] = {}
    for row in rows:
        code = str(row["error_code"] or "").upper()
        if row["status"] == "failed" and code not in definitive:
            continue
        key: ArbiterRecordKey = (
            str(row["provider"]).lower(), str(row["deployment"]).lower(),
            str(row["model"]).lower(), str(row["task_type"]).lower(),
        )
        tally = tallies.setdefault(key, [0, 0])
        tally[0] += 1
        if row["status"] == "failed":
            tally[1] += 1
    return {key: ArbiterRecord(attempts, failures) for key, (attempts, failures) in tallies.items()}
