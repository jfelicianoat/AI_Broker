"""Descarga de modelos locales por inactividad.

Cubre las tres piezas: la decisión (IdleUnloadTracker), la descarga real
(OllamaLifecycleManager.unload_idle) y el bucle que las une.
"""
import asyncio
import json
import unittest

import httpx

from app.config import BrokerConfig, ProcessingConfig
from app.maintenance import IdleUnloadTracker, idle_poll_seconds, idle_unload_loop
from app.providers.ollama import OllamaProvider


class _Clock:
    """Reloj de mentira: el plazo se prueba entero sin esperar un segundo."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class IdleUnloadTrackerTests(unittest.TestCase):
    def test_does_not_unload_before_the_deadline(self) -> None:
        clock = _Clock()
        tracker = IdleUnloadTracker(now=clock)
        clock.advance(59)
        self.assertFalse(tracker.should_unload(busy=False, idle_seconds=60))

    def test_unloads_once_the_deadline_is_reached(self) -> None:
        clock = _Clock()
        tracker = IdleUnloadTracker(now=clock)
        clock.advance(60)
        self.assertTrue(tracker.should_unload(busy=False, idle_seconds=60))

    def test_work_in_flight_restarts_the_clock(self) -> None:
        clock = _Clock()
        tracker = IdleUnloadTracker(now=clock)
        clock.advance(59)
        # Entra trabajo justo antes de cumplirse el plazo: el reloj vuelve a
        # cero. Sin esto, la primera pausa larga descargaría en mitad de una
        # racha de tareas.
        self.assertFalse(tracker.should_unload(busy=True, idle_seconds=60))
        clock.advance(59)
        self.assertFalse(tracker.should_unload(busy=False, idle_seconds=60))
        clock.advance(1)
        self.assertTrue(tracker.should_unload(busy=False, idle_seconds=60))

    def test_does_not_repeat_until_new_work_arrives(self) -> None:
        clock = _Clock()
        tracker = IdleUnloadTracker(now=clock)
        clock.advance(60)
        self.assertTrue(tracker.should_unload(busy=False, idle_seconds=60))
        tracker.mark_reclaimed()
        clock.advance(600)
        # La memoria ya está devuelta: seguir preguntándole al runtime por una
        # lista que sabemos vacía sería un sondeo eterno sin objeto.
        self.assertFalse(tracker.should_unload(busy=False, idle_seconds=60))
        self.assertFalse(tracker.should_unload(busy=True, idle_seconds=60))
        clock.advance(60)
        # Tras pasar trabajo vuelve a estar armado.
        self.assertTrue(tracker.should_unload(busy=False, idle_seconds=60))

    def test_disabled_deadline_never_unloads_but_keeps_the_clock_honest(self) -> None:
        clock = _Clock()
        tracker = IdleUnloadTracker(now=clock)
        clock.advance(600)
        self.assertFalse(tracker.should_unload(busy=False, idle_seconds=0))
        # Se activa tras una hora ocupada: el reloj sabe que la máquina NO
        # lleva parada, así que no descarga de golpe al guardar el ajuste.
        self.assertFalse(tracker.should_unload(busy=True, idle_seconds=0))
        self.assertFalse(tracker.should_unload(busy=False, idle_seconds=60))

    def test_poll_interval_follows_the_deadline_within_bounds(self) -> None:
        self.assertEqual(idle_poll_seconds(0), 30.0)
        self.assertEqual(idle_poll_seconds(3600), 30.0)
        self.assertEqual(idle_poll_seconds(120), 30.0)
        self.assertEqual(idle_poll_seconds(40), 10.0)
        # Suelo de un segundo: un plazo corto no puede volverse espera activa.
        self.assertEqual(idle_poll_seconds(2), 1.0)


def _ollama_with_loaded(loaded: dict[str, int], *, refuses: str | None = None) -> OllamaProvider:
    """Ollama de mentira que descarga de verdad: `keep_alive: 0` saca el modelo
    de /api/ps, que es lo que espera el bucle de confirmación de `unload`."""
    resident = dict(loaded)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={
                "models": [{"name": name, "size_vram": size} for name, size in resident.items()],
            })
        if request.url.path == "/api/generate":
            payload = json.loads(request.content or b"{}")
            model = str(payload.get("model") or "")
            if model == refuses:
                return httpx.Response(500, json={"error": "el runtime se niega"})
            resident.pop(model, None)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return OllamaProvider(
        BrokerConfig(processing=ProcessingConfig(unload_after_task=False)),
        transport=httpx.MockTransport(handler),
    )


class UnloadIdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_unloads_every_model_nobody_is_using(self) -> None:
        provider = _ollama_with_loaded({"qwen": 8_000_000_000, "llama": 4_000_000_000})
        unloaded = await provider.lifecycle.unload_idle()
        self.assertEqual(sorted(unloaded), ["llama", "qwen"])
        self.assertEqual(await provider.lifecycle.running(), [])
        await provider.close()

    async def test_a_live_lease_aborts_the_whole_round(self) -> None:
        provider = _ollama_with_loaded({"qwen": 8_000_000_000, "llama": 4_000_000_000})
        # Un lease vivo significa que la máquina está trabajando: no se descarga
        # ni siquiera el modelo que no interviene, porque la inactividad no es
        # propiedad de cada modelo sino del broker entero.
        provider.lifecycle._leases["qwen"] = 1
        self.assertEqual(await provider.lifecycle.unload_idle(), [])
        self.assertEqual(len(await provider.lifecycle.running()), 2)
        await provider.close()

    async def test_a_model_that_refuses_does_not_hold_the_rest_hostage(self) -> None:
        provider = _ollama_with_loaded(
            {"terco": 8_000_000_000, "llama": 4_000_000_000}, refuses="terco",
        )
        self.assertEqual(await provider.lifecycle.unload_idle(), ["llama"])
        # El que falla sigue dentro; liberar uno de dos sigue siendo liberar.
        self.assertEqual([item["name"] for item in await provider.lifecycle.running()], ["terco"])
        await provider.close()


class _FakeRepository:
    """Repositorio mínimo: dice si hay trabajo y hace correr el reloj.

    Cada consulta adelanta el reloj de mentira, que es lo que ocurre de verdad
    entre dos sondeos, y así el bucle se prueba sin esperas reales."""

    def __init__(self, *, busy: bool, clock: _Clock, step: float, stop_after: int) -> None:
        self.busy = busy
        self._clock = clock
        self._step = step
        self._stop_after = stop_after
        self.stop: asyncio.Event | None = None
        self.polls = 0

    def has_unfinished_task(self) -> bool:
        self.polls += 1
        self._clock.advance(self._step)
        if self.polls >= self._stop_after and self.stop is not None:
            self.stop.set()
        return self.busy


class _FakeLifecycle:
    def __init__(self, *, unloaded: list[str] | None = None, explodes: bool = False) -> None:
        self._unloaded = unloaded or []
        self._explodes = explodes
        self.calls = 0

    async def unload_idle(self) -> list[str]:
        self.calls += 1
        if self._explodes:
            raise RuntimeError("ollama no responde")
        return self._unloaded


class IdleUnloadLoopTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, config, repository, lifecycle, clock) -> None:
        stop = asyncio.Event()
        repository.stop = stop
        await asyncio.wait_for(
            idle_unload_loop(repository, lifecycle, config, stop, now=clock), timeout=10,
        )

    async def test_unloads_when_the_broker_has_been_idle_long_enough(self) -> None:
        clock = _Clock()
        config = BrokerConfig(processing=ProcessingConfig(
            unload_after_task=False, idle_unload_seconds=60,
        ))
        lifecycle = _FakeLifecycle(unloaded=["qwen"])
        repository = _FakeRepository(busy=False, clock=clock, step=120, stop_after=1)
        await self._run(config, repository, lifecycle, clock)
        self.assertEqual(lifecycle.calls, 1)

    async def test_pending_or_running_work_keeps_the_models_loaded(self) -> None:
        clock = _Clock()
        config = BrokerConfig(processing=ProcessingConfig(
            unload_after_task=False, idle_unload_seconds=60,
        ))
        lifecycle = _FakeLifecycle()
        repository = _FakeRepository(busy=True, clock=clock, step=120, stop_after=1)
        await self._run(config, repository, lifecycle, clock)
        self.assertEqual(lifecycle.calls, 0)

    async def test_unload_after_task_cancels_the_deadline(self) -> None:
        clock = _Clock()
        # Con descarga al terminar cada tarea no queda nada cargado que
        # reclamar: el plazo configurado no debe traducirse en sondeos.
        config = BrokerConfig(processing=ProcessingConfig(
            unload_after_task=True, idle_unload_seconds=60,
        ))
        lifecycle = _FakeLifecycle()
        repository = _FakeRepository(busy=False, clock=clock, step=120, stop_after=1)
        await self._run(config, repository, lifecycle, clock)
        self.assertEqual(lifecycle.calls, 0)

    async def test_does_not_unload_twice_without_new_work(self) -> None:
        clock = _Clock()
        config = BrokerConfig(processing=ProcessingConfig(
            unload_after_task=False, idle_unload_seconds=4,
        ))
        lifecycle = _FakeLifecycle(unloaded=["qwen"])
        repository = _FakeRepository(busy=False, clock=clock, step=120, stop_after=2)
        await self._run(config, repository, lifecycle, clock)
        self.assertEqual(repository.polls, 2)
        self.assertEqual(lifecycle.calls, 1)

    async def test_a_runtime_failure_does_not_kill_the_loop(self) -> None:
        clock = _Clock()
        config = BrokerConfig(processing=ProcessingConfig(
            unload_after_task=False, idle_unload_seconds=60,
        ))
        lifecycle = _FakeLifecycle(explodes=True)
        repository = _FakeRepository(busy=False, clock=clock, step=120, stop_after=1)
        with self.assertLogs("ai_broker.maintenance", level="WARNING"):
            # Sin este blindaje, un Ollama caído dejaría la memoria sin recuperar
            # hasta el siguiente reinicio y nadie lo notaría.
            await self._run(config, repository, lifecycle, clock)
        self.assertEqual(lifecycle.calls, 1)


if __name__ == "__main__":
    unittest.main()
