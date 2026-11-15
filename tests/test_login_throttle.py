"""AB-05: LoginThrottle olvida los orígenes cuyo bloqueo terminó hace más de una hora."""
import pytest

import app.admin_auth as admin_auth
from app.admin_auth import LoginThrottle


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(admin_auth.time, "monotonic", lambda: now[0])
    return now


def test_expired_origins_are_purged(clock: list[float]) -> None:
    throttle = LoginThrottle()
    throttle.record_failure("a")
    clock[0] += 3700
    throttle.record_failure("b")
    assert "a" not in throttle._state
    assert "b" in throttle._state


def test_recent_and_blocked_origins_are_kept(clock: list[float]) -> None:
    throttle = LoginThrottle(max_free_failures=2, base_delay_seconds=300, max_delay_seconds=300)
    throttle.record_failure("bloqueado")
    throttle.record_failure("bloqueado")  # segundo fallo: bloqueado 300 s
    throttle.record_failure("reciente")  # primer fallo: sin bloqueo
    clock[0] += 3800  # el bloqueo acabó hace 3500 s: aún dentro de la hora
    throttle.record_failure("otro")
    assert throttle._state["bloqueado"][0] == 2
    assert "reciente" not in throttle._state  # acabó hace 3800 s


def test_backoff_still_grows_for_a_persistent_origin(clock: list[float]) -> None:
    throttle = LoginThrottle(max_free_failures=2, base_delay_seconds=2)
    for _ in range(4):
        throttle.record_failure("x")
    assert throttle.blocked_for("x") == pytest.approx(8.0)
    throttle.reset("x")
    assert throttle.blocked_for("x") == 0.0
