from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import execution_fingerprint as fingerprint
from app.providers.base import AgentTurn, ModelOutput, ToolCall, effective_generation
from app.schemas import InferenceKind, ModelReference, TaskCreateRequest


class BootstrapModelProvider:
    async def models(self) -> list[dict[str, Any]]:
        return [{"name": "bootstrap-single", "provider": "ollama", "deployment": "bootstrap", "status": "available",
                 "context_window": 1_000_000, "capabilities": ["completion", "embedding"],
                 "compatibility": "compatible", "compatibility_checked_at": None, "compatibility_error": None,
                 "features": {"vision": False, "json_mode": True, "tools": True},
                 # El proveedor fake no ejecuta nada: solo puede declarar quién
                 # dice ser. El resto de componentes queda no_disponible, que es
                 # exactamente lo que se sabe de él.
                 "execution_fingerprint": fingerprint.build({
                     "provider": fingerprint.component("ollama", fingerprint.DECLARED),
                     "deployment": fingerprint.component("bootstrap", fingerprint.DECLARED),
                     "model": fingerprint.component("bootstrap-single", fingerprint.DECLARED),
                 })}]

    async def agent_turn(
        self,
        request: TaskCreateRequest,
        model: ModelReference,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        allow_parallel: bool = False,
    ) -> AgentTurn:
        return await self.chat_tools(request, model, messages, tools)

    # Nombres de tools consideradas skills integradas por el bootstrap; el resto
    # se tratan como tools del cliente (passthrough) al simular el agente.
    _BUILTIN = {"web_search", "fetch_url", "calculator", "current_datetime"}

    async def chat_tools(
        self,
        request: TaskCreateRequest,
        model: ModelReference | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AgentTurn:
        """Agente determinista: en la primera ronda pide la primera tool
        disponible (integrada o del cliente); con su resultado ya en el
        historial, responde y termina. Simula así también el passthrough."""
        already_called = any(item.get("role") == "tool" for item in messages)
        started = datetime.now(timezone.utc)
        if not already_called and tools:
            name = tools[0]["function"]["name"]
            if name in self._BUILTIN:
                argument = "consulta bootstrap" if name == "web_search" else "https://example.com"
                key = "query" if name == "web_search" else "url"
                args: dict[str, Any] = {key: argument}
                args_json = f'{{"{key}": "{argument}"}}'
            else:
                args = {"input": "valor bootstrap"}
                args_json = '{"input": "valor bootstrap"}'
            return AgentTurn(
                content=None,
                tool_calls=(ToolCall(id="call_0", name=name, arguments=args),),
                tokens_input=8, tokens_output=4, cost_usd=0.0,
                latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
                raw_assistant_message={"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_0", "type": "function",
                     "function": {"name": name, "arguments": args_json}},
                ]},
                generation=self._generation(request),
            )
        text = f"Respuesta final del agente bootstrap para: {request.content.prompt}"
        return AgentTurn(
            content=text, tool_calls=(), tokens_input=6, tokens_output=max(1, len(text)//4),
            cost_usd=0.0, latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            raw_assistant_message={"role": "assistant", "content": text},
            generation=self._generation(request),
        )

    async def select(self, request: TaskCreateRequest, count: int, roles: list[str]) -> list[ModelReference]:
        target = request.model_requirements.target_model
        if target is not None:
            return [target.model_copy(update={"role": roles[index]}) for index in range(count)]
        # allowed_providers=None significa "sin restricción": el proveedor fake
        # se nombra a sí mismo en vez de leer una lista que puede no venir.
        declared = request.model_requirements.allowed_providers or ["bootstrap"]
        return [ModelReference(provider=declared[0], deployment="bootstrap",
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
            return ModelOutput(None, max(1, len(request.content.prompt)//4), 0, 0.0, 1.0, (0.25, 0.5, 0.75),
                               generation=effective_generation(request, applies=False))
        text = f"## Propuesta {ordinal}: {model.role or 'proposer'}\n\n{request.content.prompt}\n\nProveedor bootstrap."
        return self._output(text, request)
    async def synthesize(self, request: TaskCreateRequest, model: ModelReference, proposals: list[ModelOutput]) -> ModelOutput:
        text = "# Síntesis de Consenso Rápido\n\n" + "\n\n".join(item.content or "" for item in proposals)
        return self._output(text, request)
    @staticmethod
    def _generation(request: TaskCreateRequest) -> dict[str, Any]:
        # El proveedor fake no ejecuta ningún modelo, así que una semilla pedida
        # aquí no se aplica a nada: se declara `unsupported` en vez de fingir
        # que viajó a alguna parte.
        return effective_generation(request, seed_supported=False, top_p_supported=False)
    def _output(self, content: str, request: TaskCreateRequest) -> ModelOutput:
        prompt = request.content.prompt
        return ModelOutput(content, max(1, len(prompt)//4), max(1, len(content)//4), 0.0, 1.0,
                           generation=self._generation(request))
