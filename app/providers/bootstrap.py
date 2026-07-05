from __future__ import annotations

from typing import Any

from app.providers.base import ModelOutput
from app.schemas import InferenceKind, ModelReference, TaskCreateRequest


class BootstrapModelProvider:
    async def models(self) -> list[dict[str, Any]]:
        return [{"name": "bootstrap-single", "provider": "ollama", "deployment": "bootstrap", "status": "available",
                 "context_window": 1_000_000, "capabilities": ["completion", "embedding"],
                 "compatibility": "compatible", "compatibility_checked_at": None, "compatibility_error": None}]

    async def select(self, request: TaskCreateRequest, count: int, roles: list[str]) -> list[ModelReference]:
        target = request.model_requirements.target_model
        if target is not None:
            return [target.model_copy(update={"role": roles[index]}) for index in range(count)]
        return [ModelReference(provider=request.model_requirements.allowed_providers[0], deployment="bootstrap",
                               model=request.model_requirements.preferred_model or f"bootstrap-{i+1}", role=roles[i])
                for i in range(count)]

    async def close(self) -> None: return None
    async def health(self) -> dict[str, dict[str, Any]]:
        return {"bootstrap": {"status": "healthy", "detail": "Proveedor determinista de pruebas", "latency_ms": 0.0}}
    async def resource_snapshot(self) -> dict[str, Any]:
        return {
            "provider": "bootstrap",
            "used_vram_bytes": 0,
            "reserved_vram_bytes": 0,
            "loaded_models": [],
        }
    async def propose(self, request: TaskCreateRequest, model: ModelReference, ordinal: int) -> ModelOutput:
        if request.inference_kind == InferenceKind.embedding:
            return ModelOutput(None, max(1, len(request.content.prompt)//4), 0, 0.0, 1.0, (0.25, 0.5, 0.75))
        text = f"## Propuesta {ordinal}: {model.role or 'proposer'}\n\n{request.content.prompt}\n\nProveedor bootstrap."
        return self._output(text, request.content.prompt)
    async def synthesize(self, request: TaskCreateRequest, model: ModelReference, proposals: list[ModelOutput]) -> ModelOutput:
        text = "# Síntesis de Consenso Rápido\n\n" + "\n\n".join(item.content or "" for item in proposals)
        return self._output(text, request.content.prompt)
    @staticmethod
    def _output(content: str, prompt: str) -> ModelOutput:
        return ModelOutput(content, max(1, len(prompt)//4), max(1, len(content)//4), 0.0, 1.0)
