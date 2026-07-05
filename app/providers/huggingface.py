from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import HuggingFaceLocalConfig, HuggingFaceLocalModelConfig
from app.providers.base import (
    ModelOutput,
    ProviderError,
    _estimation_text,
    request_with_context_capped_output,
)
from app.schemas import TaskCreateRequest


class HuggingFaceLocalProvider:
    def __init__(self, config: HuggingFaceLocalConfig) -> None:
        self.config = config
        self._loaded: dict[str, tuple[Any, Any]] = {}
        self._load_lock = asyncio.Lock()

    async def close(self) -> None:
        self._loaded.clear()

    def reload_config(self, config: HuggingFaceLocalConfig) -> None:
        if config != self.config:
            self._loaded.clear()
        self.config = config

    async def models(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []
        return [self._catalog_entry(item) for item in self.config.models]

    async def health(self) -> dict[str, Any]:
        started = asyncio.get_running_loop().time()
        if not self.config.enabled:
            return {"status": "degraded", "detail": "Hugging Face local deshabilitado", "latency_ms": 0.0}
        missing = [item.name for item in self.config.models if not self._model_path(item).exists()]
        try:
            self._import_runtime()
        except ProviderError as error:
            return {
                "status": "unavailable",
                "detail": f"{error.code}: {error}",
                "latency_ms": (asyncio.get_running_loop().time() - started) * 1000,
            }
        if missing:
            return {
                "status": "degraded",
                "detail": "Rutas de modelos no encontradas: " + ", ".join(missing[:5]),
                "latency_ms": (asyncio.get_running_loop().time() - started) * 1000,
            }
        return {
            "status": "healthy" if self.config.models else "degraded",
            "detail": f"{len(self.config.models)} modelos locales configurados",
            "latency_ms": (asyncio.get_running_loop().time() - started) * 1000,
        }

    def _catalog_entry(self, item: HuggingFaceLocalModelConfig) -> dict[str, Any]:
        path = self._model_path(item)
        compatible = item.compatibility
        compatibility_error = item.compatibility_error
        if not path.exists():
            compatible = "incompatible"
            compatibility_error = f"Ruta local no encontrada: {path}"
        return {
            "name": item.name,
            "provider": "huggingface_local",
            "deployment": "local",
            "status": "available" if path.exists() else "offline",
            "path": str(path),
            "context_window": item.context_window,
            "capabilities": list(item.capabilities),
            "family": "huggingface_local",
            "compatibility": compatible,
            "compatibility_checked_at": item.compatibility_checked_at,
            "compatibility_error": compatibility_error,
        }

    def _model_path(self, item: HuggingFaceLocalModelConfig) -> Path:
        raw = Path(item.path)
        if raw.is_absolute():
            return raw
        return Path(self.config.models_dir) / raw

    def _find_model_config(self, model: str) -> HuggingFaceLocalModelConfig | None:
        return next((item for item in self.config.models if item.name == model), None)

    @staticmethod
    def _import_runtime() -> tuple[Any, Any, Any]:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            return torch, AutoModelForCausalLM, AutoTokenizer
        except ImportError as error:
            raise ProviderError(
                "LOCAL_RUNTIME_UNAVAILABLE",
                "Faltan dependencias locales: instala transformers y torch para usar HuggingFaceLocalProvider",
                retryable=False,
            ) from error

    @staticmethod
    def _torch_dtype(torch: Any, dtype: str | None) -> Any | None:
        if not dtype:
            return None
        normalized = dtype.lower()
        mapping = {
            "auto": "auto",
            "float16": torch.float16,
            "fp16": torch.float16,
            "bfloat16": torch.bfloat16,
            "bf16": torch.bfloat16,
            "float32": torch.float32,
            "fp32": torch.float32,
        }
        if normalized not in mapping:
            raise ProviderError("INVALID_LOCAL_MODEL_CONFIG", f"dtype no soportado: {dtype}")
        return mapping[normalized]

    async def _load(self, item: HuggingFaceLocalModelConfig) -> tuple[Any, Any]:
        if item.name in self._loaded:
            return self._loaded[item.name]
        async with self._load_lock:
            if item.name in self._loaded:
                return self._loaded[item.name]
            torch, AutoModelForCausalLM, AutoTokenizer = self._import_runtime()
            path = self._model_path(item)
            if not path.exists():
                raise ProviderError("MODEL_UNAVAILABLE", f"Ruta local no encontrada para {item.name}: {path}")
            trust_remote_code = self.config.trust_remote_code if item.trust_remote_code is None else item.trust_remote_code
            dtype = self._torch_dtype(torch, item.dtype or self.config.default_dtype)
            device = item.device or self.config.default_device
            load_kwargs: dict[str, Any] = {"trust_remote_code": trust_remote_code}
            if dtype is not None:
                load_kwargs["torch_dtype"] = dtype
            if device == "auto":
                load_kwargs["device_map"] = "auto"
            model = await asyncio.to_thread(AutoModelForCausalLM.from_pretrained, str(path), **load_kwargs)
            tokenizer = await asyncio.to_thread(AutoTokenizer.from_pretrained, str(path), trust_remote_code=trust_remote_code)
            if device and device != "auto":
                model = await asyncio.to_thread(model.to, device)
            if getattr(tokenizer, "pad_token_id", None) is None and getattr(tokenizer, "eos_token", None) is not None:
                tokenizer.pad_token = tokenizer.eos_token
            self._loaded[item.name] = (model, tokenizer)
            return model, tokenizer

    async def generate(
        self, request: TaskCreateRequest, model: str, prompt: str, system: str | None = None
    ) -> ModelOutput:
        item = self._find_model_config(model)
        if item is None:
            raise ProviderError("MODEL_UNAVAILABLE", f"Modelo Hugging Face local no configurado: {model}")
        if "completion" not in {capability.lower() for capability in item.capabilities}:
            raise ProviderError("MODEL_CAPABILITY_MISMATCH", f"El modelo {model} no declara capacidad completion")
        inference_request = request_with_context_capped_output(
            request, item.context_window, _estimation_text(prompt, system)
        )
        started = datetime.now(timezone.utc)
        loaded_model, tokenizer = await self._load(item)
        try:
            generated_text, input_tokens, output_tokens = await asyncio.to_thread(
                self._generate_sync,
                loaded_model,
                tokenizer,
                prompt,
                inference_request.generation.temperature,
                inference_request.generation.max_output_tokens,
                system,
            )
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError("MODEL_ERROR", f"Error ejecutando {model}: {type(error).__name__}: {error}") from error
        if not generated_text.strip():
            raise ProviderError("INVALID_PROVIDER_RESPONSE", f"{model} no devolvio contenido")
        return ModelOutput(
            generated_text,
            input_tokens,
            output_tokens,
            0.0,
            (datetime.now(timezone.utc) - started).total_seconds() * 1000,
        )

    @staticmethod
    def _generate_sync(
        model: Any,
        tokenizer: Any,
        prompt: str,
        temperature: float,
        max_output_tokens: int,
        system: str | None = None,
    ) -> tuple[str, int, int]:
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        if hasattr(tokenizer, "apply_chat_template") and getattr(tokenizer, "chat_template", None):
            input_ids = tokenizer.apply_chat_template(
                messages,
                add_generation_prompt=True,
                return_tensors="pt",
            )
        else:
            flat_prompt = prompt if not system else f"{system}\n\n{prompt}"
            input_ids = tokenizer(flat_prompt, return_tensors="pt").input_ids
        device = getattr(model, "device", None)
        if device is not None and hasattr(input_ids, "to"):
            input_ids = input_ids.to(device)
        generated = model.generate(
            input_ids,
            max_new_tokens=max_output_tokens,
            do_sample=temperature > 0,
            temperature=max(temperature, 0.01),
            pad_token_id=getattr(tokenizer, "pad_token_id", None),
            eos_token_id=getattr(tokenizer, "eos_token_id", None),
        )
        output_ids = generated[0][input_ids.shape[-1]:]
        text = tokenizer.decode(output_ids, skip_special_tokens=True)
        return text, int(input_ids.shape[-1]), int(output_ids.shape[-1])
