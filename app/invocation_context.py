"""Qué invocación está sirviendo el proveedor en este contexto asíncrono.

El coordinador abre la fila de la invocación (`start_invocation`) y justo
después llama al proveedor. Los adapters no reciben el identificador por
parámetro —la firma de propose/synthesize es la misma para todos—, pero el
provider `larm` lo necesita: la clave idempotente de la lease es
`task_id:invocation_id` (contrato LARM 06), y es la que permite reconciliar
tras un reinicio sin pedir una segunda lease para la misma llamada.

Un ContextVar sigue al trabajo y no al hilo: cada rama paralela de un mixture
corre en su propia tarea asyncio, con su copia del contexto, así que dos
proponentes simultáneos no se pisan la invocación."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class InvocationContext:
    task_id: str
    invocation_id: str


_current: ContextVar[InvocationContext | None] = ContextVar("broker_invocation", default=None)


def bind(task_id: str, invocation_id: str) -> None:
    _current.set(InvocationContext(task_id=task_id, invocation_id=invocation_id))


def current() -> InvocationContext | None:
    return _current.get()


def reset() -> None:
    """Olvida la invocación ligada a este contexto (tests y llamadas sueltas)."""
    _current.set(None)
