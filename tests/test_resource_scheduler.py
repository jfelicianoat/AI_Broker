from __future__ import annotations

import unittest

import httpx

from app.config import BrokerConfig, ResourceConfig
from app.providers import OllamaProvider, RoutedModelProvider
from app.resource_scheduler import (
    ModelFootprint,
    ResourcePlanningError,
    ResourceScheduler,
    SchedulingMode,
)
from app.schemas import ModelReference, TaskCreateRequest

GB = 1024**3
# Tamaños reales de la máquina donde se detectó el fallo (task_573ee98f...):
# el plan prometió las tres en paralelo porque contaba invocaciones, y el
# tercer proposer se estrelló contra los 65.5 GB que ya tenían los otros dos.
QWEN_CODER = int(17.3 * GB)
QWEN_CODER_NEXT = int(48.2 * GB)
NORTH_MINI_BF16 = int(56.8 * GB)


def unified_config(**overrides) -> BrokerConfig:
    resources = ResourceConfig(
        local_vram_budget_gb=64.0,
        unified_memory_budget_gb=112.0,
        vram_safety_margin_gb=2.0,
        **overrides,
    )
    return BrokerConfig(resources=resources)


def moa_request(**execution) -> TaskCreateRequest:
    payload = {
        "idempotency_key": "resources:moa",
        "content": {"prompt": "consenso"},
        "execution": {
            "strategy": "mixture_of_agents",
            "preset": "slow",
            "max_proposers": 3,
            **execution,
        },
    }
    return TaskCreateRequest.model_validate(payload)


def footprints(*sizes: tuple[str, int]) -> list[ModelFootprint]:
    return [
        ModelFootprint(label=f"proposer_{index}", model=model, size_bytes=size)
        for index, (model, size) in enumerate(sizes, start=1)
    ]


class MemoryAwarePlanningTests(unittest.TestCase):
    def test_oversized_proposer_moves_to_a_later_wave_instead_of_dying(self) -> None:
        plan = ResourceScheduler(unified_config()).plan(
            moa_request(),
            footprints(
                ("qwen3-coder:30b", QWEN_CODER),
                ("qwen3-coder-next:latest", QWEN_CODER_NEXT),
                ("north-mini-code-1.0:bf16", NORTH_MINI_BF16),
            ),
        )
        self.assertEqual(plan.mode, SchedulingMode.waves)
        self.assertEqual(plan.waves, [["proposer_1", "proposer_2"], ["proposer_3"]])
        self.assertEqual(plan.invocations_total, 3)
        self.assertEqual(plan.peak_vram_reserved_gb, 65.5)

    def test_models_that_fit_together_still_run_in_one_wave(self) -> None:
        plan = ResourceScheduler(unified_config()).plan(
            moa_request(),
            footprints(
                ("qwen3-coder:30b", QWEN_CODER),
                ("north-mini-code-1.0:q8_0", int(30.2 * GB)),
                ("gemma4:12b", int(9.0 * GB)),
            ),
        )
        self.assertEqual(plan.mode, SchedulingMode.parallel)
        self.assertEqual(plan.waves, [["proposer_1", "proposer_2", "proposer_3"]])

    def test_cloud_proposers_do_not_consume_the_local_budget(self) -> None:
        plan = ResourceScheduler(unified_config()).plan(
            moa_request(),
            footprints(
                ("north-mini-code-1.0:bf16", NORTH_MINI_BF16),
                ("deepseek-chat", 0),
                ("qwen3-coder-next:latest", QWEN_CODER_NEXT),
            ),
        )
        # Los 105 GB de los dos locales caben con el cloud en medio, que no
        # toca el pool de la máquina.
        self.assertEqual(plan.mode, SchedulingMode.parallel)
        self.assertEqual(len(plan.waves), 1)

    def test_the_same_model_twice_is_only_paid_once(self) -> None:
        plan = ResourceScheduler(unified_config()).plan(
            moa_request(),
            footprints(
                ("qwen3-coder-next:latest", QWEN_CODER_NEXT),
                ("qwen3-coder-next:latest", QWEN_CODER_NEXT),
                ("qwen3-coder:30b", QWEN_CODER),
            ),
        )
        self.assertEqual(plan.mode, SchedulingMode.parallel)
        self.assertEqual(plan.peak_vram_reserved_gb, 65.5)

    def test_a_model_that_never_fits_is_named_in_the_plan(self) -> None:
        # Mayor que el pool entero: escalonar no lo arregla, así que el plan
        # tiene que decirlo en vez de dejar un proposer saltado sin explicación.
        plan = ResourceScheduler(unified_config()).plan(
            moa_request(),
            footprints(
                ("qwen3-coder:30b", QWEN_CODER),
                ("gigante:bf16", int(120.0 * GB)),
            ),
        )
        self.assertEqual(plan.waves, [["proposer_1"], ["proposer_2"]])
        self.assertTrue(any("gigante:bf16" in reason for reason in plan.reasons))

    def test_strict_parallel_fails_loudly_when_memory_is_the_limit(self) -> None:
        scheduler = ResourceScheduler(unified_config())
        with self.assertRaisesRegex(ResourcePlanningError, "122.3 GB of local memory"):
            scheduler.plan(
                moa_request(scheduling="parallel"),
                footprints(
                    ("qwen3-coder:30b", QWEN_CODER),
                    ("qwen3-coder-next:latest", QWEN_CODER_NEXT),
                    ("north-mini-code-1.0:bf16", NORTH_MINI_BF16),
                ),
            )

    def test_waves_disabled_falls_back_to_one_at_a_time(self) -> None:
        plan = ResourceScheduler(unified_config(allow_execution_waves=False)).plan(
            moa_request(),
            footprints(
                ("qwen3-coder:30b", QWEN_CODER),
                ("qwen3-coder-next:latest", QWEN_CODER_NEXT),
                ("north-mini-code-1.0:bf16", NORTH_MINI_BF16),
            ),
        )
        self.assertEqual(plan.mode, SchedulingMode.sequential)
        self.assertEqual(plan.waves, [["proposer_1"], ["proposer_2"], ["proposer_3"]])

    def test_without_footprints_the_old_count_based_plan_is_kept(self) -> None:
        plan = ResourceScheduler(unified_config()).plan(moa_request())
        self.assertEqual(plan.mode, SchedulingMode.parallel)
        self.assertEqual(plan.peak_vram_reserved_gb, 0)


class LocalFootprintTests(unittest.IsolatedAsyncioTestCase):
    async def test_sizes_come_from_the_catalog_and_cloud_weighs_nothing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [
                    {"name": "big", "size": NORTH_MINI_BF16, "capabilities": ["completion"],
                     "details": {"context_length": 32768}},
                    {"name": "remote", "size": 0, "capabilities": ["completion"],
                     "remote_host": "ollama.com", "details": {"context_length": 32768}},
                ]})
            return httpx.Response(200, json={"models": []})

        config = unified_config()
        provider = RoutedModelProvider(
            config, ollama=OllamaProvider(config, transport=httpx.MockTransport(handler))
        )
        sizes = await provider.local_footprints([
            ModelReference(provider="ollama", deployment="local", model="big"),
            ModelReference(provider="ollama", deployment="cloud", model="remote"),
            ModelReference(provider="ollama", deployment="local", model="ausente"),
        ])
        await provider.close()
        self.assertEqual(sizes, [NORTH_MINI_BF16, 0, 0])


if __name__ == "__main__":
    unittest.main()
