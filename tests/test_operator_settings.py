"""Los tres ajustes que estaban declarados y no los leía nadie.

`server.cors_enabled`, `resources.max_loaded_local_models` y
`resources.scheduling_policy` existían en el YAML y en el panel desde hacía
meses sin ningún lector en el código: mandos desconectados, que es peor que no
tenerlos —quien los mueve cree haber cambiado algo—. Estos tests son la
condición para que no vuelvan a quedarse sueltos.
"""
from __future__ import annotations

import unittest

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import (
    BrokerConfig,
    ProcessingConfig,
    ResourceConfig,
    ServerConfig,
    effective_max_loaded_local_models,
)
from app.main import create_app
from app.providers import OllamaProvider
from app.providers.base import ProviderError
from app.resource_scheduler import ResourceScheduler, SchedulingMode
from app.schemas import SchedulingPolicy, TaskCreateRequest


def _moa_request(**execution) -> TaskCreateRequest:
    return TaskCreateRequest.model_validate({
        "idempotency_key": "settings:moa",
        "content": {"prompt": "consenso"},
        "execution": {
            "strategy": "mixture_of_agents",
            "preset": "slow",
            "max_proposers": 3,
            **execution,
        },
    })


class SchedulingPolicyDefaultTests(unittest.TestCase):
    """`resources.scheduling_policy` es el valor por defecto del operador, no
    una orden: lo que pida el cliente manda siempre."""

    def _scheduler(self, policy: str) -> ResourceScheduler:
        return ResourceScheduler(BrokerConfig(
            resources=ResourceConfig(scheduling_policy=policy, local_vram_budget_gb=64.0),
        ))

    def test_silence_of_the_client_uses_the_configured_policy(self) -> None:
        plan = self._scheduler("sequential").plan(_moa_request())
        self.assertIs(plan.mode, SchedulingMode.sequential)
        self.assertIn("sequential scheduling requested", plan.reasons)

    def test_an_explicit_client_policy_wins_over_the_configured_one(self) -> None:
        # El cliente pide adaptive teniendo el broker configurado en sequential:
        # es el caso que distingue "no dijo nada" de "dijo justo esto", y la
        # única razón por la que hace falta mirar `model_fields_set`.
        plan = self._scheduler("sequential").plan(_moa_request(scheduling="adaptive"))
        self.assertIsNot(plan.mode, SchedulingMode.sequential)

    def test_effective_scheduling_reports_what_governs_the_task(self) -> None:
        scheduler = self._scheduler("waves")
        self.assertEqual(
            scheduler.effective_scheduling(_moa_request()), SchedulingPolicy.waves,
        )
        self.assertEqual(
            scheduler.effective_scheduling(_moa_request(scheduling="sequential")),
            SchedulingPolicy.sequential,
        )

    def test_a_typo_in_the_policy_fails_at_load_time(self) -> None:
        # Un valor inválido tiene que romper al cargar la configuración, no
        # convertirse en silencio en "adaptive" a mitad de una planificación.
        with self.assertRaises(ValidationError):
            ResourceConfig(scheduling_policy="secuencial")


class MaxLoadedLocalModelsTests(unittest.IsolatedAsyncioTestCase):
    """El techo por conteo, además del de memoria."""

    def _config(self, slots) -> BrokerConfig:
        return BrokerConfig(
            processing=ProcessingConfig(unload_after_task=False),
            resources=ResourceConfig(
                local_vram_budget_gb=64.0,
                vram_safety_margin_gb=0.0,
                max_loaded_local_models=slots,
            ),
        )

    def test_auto_follows_the_parallel_inference_capacity(self) -> None:
        config = self._config("auto")
        # Cargar más modelos de los que se van a invocar a la vez solo ocupa
        # memoria que la siguiente tarea necesita.
        self.assertEqual(effective_max_loaded_local_models(config), 3)
        self.assertEqual(effective_max_loaded_local_models(self._config(1)), 1)

    async def test_a_full_slot_quota_defers_instead_of_loading_one_more(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                # Cabe de sobra en memoria: lo que se agota es el cupo.
                return httpx.Response(200, json={"models": [
                    {"name": "uno", "size_vram": 1_000_000},
                    {"name": "dos", "size_vram": 1_000_000},
                ]})
            return httpx.Response(404)

        provider = OllamaProvider(self._config(2), transport=httpx.MockTransport(handler))
        # Los dos cargados están sirviendo tareas: no son basura desalojable.
        provider.lifecycle._leases.update({"uno": 1, "dos": 1})
        with pytest.raises(ProviderError) as caught:
            async with provider.lifecycle.lease("tres", estimated_size=1_000_000):
                pass
        error = caught.value
        self.assertEqual(error.code, "LOCAL_MODEL_SLOTS_BUSY")
        self.assertTrue(error.retryable)
        block = error.memory_block
        self.assertEqual(block["reason"], "model_slots")
        self.assertEqual(block["model_slots"], 2)
        self.assertEqual(block["loaded_models"], 2)
        self.assertEqual(block["holders"], ["dos", "uno"])
        await provider.close()

    async def test_an_idle_model_is_evicted_before_giving_up_the_turn(self) -> None:
        state = {"loaded": ["ocioso", "ocupado"], "unloaded": []}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [
                    {"name": name, "size_vram": 1_000_000} for name in state["loaded"]
                ]})
            if request.url.path == "/api/generate":
                body = request.read().decode("utf-8")
                for name in list(state["loaded"]):
                    if f'"{name}"' in body:
                        state["loaded"].remove(name)
                        state["unloaded"].append(name)
                return httpx.Response(200, json={})
            return httpx.Response(404)

        provider = OllamaProvider(self._config(2), transport=httpx.MockTransport(handler))
        provider.lifecycle._leases["ocupado"] = 1
        # Hay sitio en cuanto se suelta el que no está sirviendo a nadie: el
        # cupo no puede convertirse en una espera con la máquina medio ociosa.
        async with provider.lifecycle.lease("nuevo", estimated_size=1_000_000):
            pass
        self.assertEqual(state["unloaded"], ["ocioso"])
        await provider.close()

    async def test_a_model_already_loaded_never_pays_the_quota(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [
                    {"name": "uno", "size_vram": 1_000_000},
                    {"name": "dos", "size_vram": 1_000_000},
                ]})
            return httpx.Response(404)

        provider = OllamaProvider(self._config(1), transport=httpx.MockTransport(handler))
        provider.lifecycle._leases.update({"uno": 1, "dos": 1})
        # Con el cupo desbordado, pedir uno que YA está cargado no carga nada:
        # hacerle esperar sería inventarse un bloqueo sin trabajo detrás.
        async with provider.lifecycle.lease("uno", estimated_size=1_000_000):
            pass
        await provider.close()


class CorsSettingTests(unittest.TestCase):
    """La casilla de CORS montaba exactamente nada hasta agosto de 2026."""

    def test_enabling_cors_without_origins_is_a_configuration_error(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            ServerConfig(cors_enabled=True)
        self.assertIn("cors_allow_origins", str(caught.exception))

    def test_the_wildcard_is_refused(self) -> None:
        # `*` obligaría a servir el API sin credenciales para no entregarle el
        # token de administración a cualquier web.
        with self.assertRaises(ValidationError):
            ServerConfig(cors_enabled=True, cors_allow_origins=["*"])

    def test_an_origin_with_a_path_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            ServerConfig(cors_enabled=True, cors_allow_origins=["https://app.local/panel"])

    def test_a_declared_origin_gets_the_cors_headers(self, tmp_path=None) -> None:
        config = BrokerConfig(server=ServerConfig(
            cors_enabled=True, cors_allow_origins=["https://app.local"],
        ))
        config.persistence.database = ":memory:"
        with TestClient(create_app(config)) as client:
            response = client.get("/health/live", headers={"Origin": "https://app.local"})
            self.assertEqual(response.headers.get("access-control-allow-origin"), "https://app.local")
            # Sin credenciales de navegador: la sesión del panel no viaja a otro origen.
            self.assertNotIn("access-control-allow-credentials", response.headers)
            otro = client.get("/health/live", headers={"Origin": "https://intruso.local"})
            self.assertIsNone(otro.headers.get("access-control-allow-origin"))

    def test_cors_is_off_by_default(self) -> None:
        config = BrokerConfig()
        config.persistence.database = ":memory:"
        with TestClient(create_app(config)) as client:
            response = client.get("/health/live", headers={"Origin": "https://app.local"})
            self.assertIsNone(response.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main()
