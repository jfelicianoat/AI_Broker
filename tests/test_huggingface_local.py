"""Cobertura del proveedor Hugging Face local sin depender de torch/transformers.

El runtime real se sustituye por fakes deterministas: los tests cubren el
catálogo, la salud, la carga de modelos y la generación (incluida la
cancelación) con el mismo comportamiento en cualquier máquina.
"""
import threading
import unittest
from types import SimpleNamespace

from app.config import HuggingFaceLocalConfig, HuggingFaceLocalModelConfig
from app.providers import HuggingFaceLocalProvider, ProviderError
from app.schemas import TaskCreateRequest


class _Tensor(list):
    """Lista con .shape y slicing tipado, suficiente para _generate_sync."""

    @property
    def shape(self):
        return (len(self),)

    def __getitem__(self, key):
        result = super().__getitem__(key)
        return _Tensor(result) if isinstance(key, slice) else result


class FakeTokenizer:
    pad_token_id = 0
    eos_token_id = 1
    chat_template = None

    def __init__(self, decoded: str = "respuesta local"):
        self.decoded = decoded

    def __call__(self, text, return_tensors=None):
        return SimpleNamespace(input_ids=_Tensor(range(len(text.split()))))

    def decode(self, ids, skip_special_tokens=True):
        return self.decoded


class FakeModel:
    def __init__(self, extra_tokens=(7, 8, 9)):
        self.extra_tokens = list(extra_tokens)

    def generate(self, input_ids, **kwargs):
        return [_Tensor(list(input_ids) + self.extra_tokens)]

    def to(self, device):
        return self


def _request() -> TaskCreateRequest:
    return TaskCreateRequest(idempotency_key="hf:test", content={"prompt": "hola mundo"})


def _config(tmp_path, *, enabled=True, model_path=None, capabilities=None) -> HuggingFaceLocalConfig:
    return HuggingFaceLocalConfig(
        enabled=enabled,
        models_dir=str(tmp_path),
        models=[
            HuggingFaceLocalModelConfig(
                name="local-hf",
                path=str(model_path if model_path is not None else tmp_path / "local-hf"),
                capabilities=capabilities or ["completion"],
            )
        ],
    )


class HuggingFaceCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_provider_reports_empty_catalog_and_degraded_health(self) -> None:
        provider = HuggingFaceLocalProvider(HuggingFaceLocalConfig(enabled=False))
        self.assertEqual(await provider.models(), [])
        health = await provider.health()
        self.assertEqual(health["status"], "degraded")

    async def test_missing_model_path_marks_entry_incompatible(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            provider = HuggingFaceLocalProvider(_config(Path(tmp), model_path=Path(tmp) / "no-existe"))
            entry = (await provider.models())[0]
            self.assertEqual(entry["status"], "offline")
            self.assertEqual(entry["compatibility"], "incompatible")
            self.assertIn("Ruta local no encontrada", entry["compatibility_error"])

    async def test_existing_model_path_is_available(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            model_dir = Path(tmp) / "local-hf"
            model_dir.mkdir()
            provider = HuggingFaceLocalProvider(_config(Path(tmp), model_path=model_dir))
            entry = (await provider.models())[0]
            self.assertEqual(entry["status"], "available")
            self.assertEqual(entry["deployment"], "local")


class HuggingFaceHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_unavailable_when_runtime_missing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            provider = HuggingFaceLocalProvider(_config(Path(tmp)))

            def broken_runtime():
                raise ProviderError("LOCAL_RUNTIME_UNAVAILABLE", "sin torch", retryable=False)

            provider._import_runtime = broken_runtime
            health = await provider.health()
            self.assertEqual(health["status"], "unavailable")
            self.assertIn("LOCAL_RUNTIME_UNAVAILABLE", health["detail"])

    async def test_health_degraded_when_paths_missing_and_healthy_when_present(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            provider = HuggingFaceLocalProvider(_config(Path(tmp), model_path=Path(tmp) / "no-existe"))
            provider._import_runtime = lambda: (None, None, None)
            degraded = await provider.health()
            self.assertEqual(degraded["status"], "degraded")
            self.assertIn("Rutas de modelos no encontradas", degraded["detail"])

            model_dir = Path(tmp) / "local-hf"
            model_dir.mkdir()
            provider = HuggingFaceLocalProvider(_config(Path(tmp), model_path=model_dir))
            provider._import_runtime = lambda: (None, None, None)
            healthy = await provider.health()
            self.assertEqual(healthy["status"], "healthy")


class HuggingFaceDtypeTests(unittest.TestCase):
    def test_dtype_mapping_and_rejection(self) -> None:
        fake_torch = SimpleNamespace(float16="f16", bfloat16="bf16", float32="f32")
        self.assertIsNone(HuggingFaceLocalProvider._torch_dtype(fake_torch, None))
        self.assertEqual(HuggingFaceLocalProvider._torch_dtype(fake_torch, "fp16"), "f16")
        self.assertEqual(HuggingFaceLocalProvider._torch_dtype(fake_torch, "BF16"), "bf16")
        self.assertEqual(HuggingFaceLocalProvider._torch_dtype(fake_torch, "auto"), "auto")
        with self.assertRaises(ProviderError) as raised:
            HuggingFaceLocalProvider._torch_dtype(fake_torch, "int4")
        self.assertEqual(raised.exception.code, "INVALID_LOCAL_MODEL_CONFIG")


class HuggingFaceGenerateTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_rejects_unknown_model_and_missing_capability(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            provider = HuggingFaceLocalProvider(_config(Path(tmp), capabilities=["embedding"]))
            with self.assertRaises(ProviderError) as unknown:
                await provider.generate(_request(), "otro-modelo", "hola")
            self.assertEqual(unknown.exception.code, "MODEL_UNAVAILABLE")

            with self.assertRaises(ProviderError) as mismatch:
                await provider.generate(_request(), "local-hf", "hola")
            self.assertEqual(mismatch.exception.code, "MODEL_CAPABILITY_MISMATCH")

    async def test_load_fails_when_model_path_missing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            provider = HuggingFaceLocalProvider(_config(Path(tmp), model_path=Path(tmp) / "no-existe"))
            provider._import_runtime = lambda: (SimpleNamespace(), None, None)
            with self.assertRaises(ProviderError) as raised:
                await provider.generate(_request(), "local-hf", "hola")
            self.assertEqual(raised.exception.code, "MODEL_UNAVAILABLE")

    async def test_load_uses_fake_runtime_and_generates(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            model_dir = Path(tmp) / "local-hf"
            model_dir.mkdir()
            config = _config(Path(tmp), model_path=model_dir)
            config.models[0].dtype = "fp16"
            config.models[0].device = "cpu"
            provider = HuggingFaceLocalProvider(config)

            fake_torch = SimpleNamespace(float16="f16", bfloat16="bf16", float32="f32")
            auto_model = SimpleNamespace(from_pretrained=lambda path, **kwargs: FakeModel())
            auto_tokenizer = SimpleNamespace(from_pretrained=lambda path, **kwargs: FakeTokenizer())
            provider._import_runtime = lambda: (fake_torch, auto_model, auto_tokenizer)

            output = await provider.generate(_request(), "local-hf", "hola mundo")
            self.assertEqual(output.content, "respuesta local")
            self.assertGreater(output.tokens_output, 0)
            self.assertEqual(output.cost_usd, 0.0)
            # Segunda llamada: el modelo cargado se reutiliza (caché _loaded).
            again = await provider.generate(_request(), "local-hf", "hola mundo")
            self.assertEqual(again.content, "respuesta local")

    async def test_generate_maps_empty_output_and_runtime_errors(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            model_dir = Path(tmp) / "local-hf"
            model_dir.mkdir()
            provider = HuggingFaceLocalProvider(_config(Path(tmp), model_path=model_dir))
            provider._loaded["local-hf"] = (FakeModel(), FakeTokenizer(decoded="   "))
            with self.assertRaises(ProviderError) as empty:
                await provider.generate(_request(), "local-hf", "hola")
            self.assertEqual(empty.exception.code, "INVALID_PROVIDER_RESPONSE")

            class ExplodingModel(FakeModel):
                def generate(self, input_ids, **kwargs):
                    raise RuntimeError("CUDA out of memory")

            provider._loaded["local-hf"] = (ExplodingModel(), FakeTokenizer())
            with self.assertRaises(ProviderError) as broken:
                await provider.generate(_request(), "local-hf", "hola")
            self.assertEqual(broken.exception.code, "MODEL_ERROR")
            self.assertIn("CUDA out of memory", str(broken.exception))

    def test_generate_sync_raises_when_cancelled(self) -> None:
        stop_event = threading.Event()
        stop_event.set()
        with self.assertRaises(ProviderError) as raised:
            HuggingFaceLocalProvider._generate_sync(
                FakeModel(), FakeTokenizer(), "hola", 0.2, 16, None, stop_event
            )
        self.assertEqual(raised.exception.code, "TASK_CANCELLED")

    async def test_reload_config_clears_loaded_models_on_change(self) -> None:
        provider = HuggingFaceLocalProvider(HuggingFaceLocalConfig(enabled=True))
        provider._loaded["viejo"] = (FakeModel(), FakeTokenizer())
        provider.reload_config(HuggingFaceLocalConfig(enabled=True, models_dir="otro"))
        self.assertEqual(provider._loaded, {})
        await provider.close()
