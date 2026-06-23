from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.schemas import ModelReference, TaskCreateRequest


@dataclass(frozen=True)
class ModelOutput:
    content: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    latency_ms: float


class BootstrapModelProvider:
    """Deterministic provider used until real Ollama/DeepSeek adapters are wired."""

    async def propose(self, request: TaskCreateRequest, model: ModelReference, ordinal: int) -> ModelOutput:
        prompt = request.content.prompt.strip()
        role = model.role or f"proposer_{ordinal}"
        answer = (
            f"## Propuesta {ordinal}: {role}\n\n"
            f"Esta propuesta analiza la petición original de forma independiente.\n\n"
            f"**Petición:** {prompt}\n\n"
            "### Afirmaciones\n\n"
            "- Se conserva el contenido original como fuente única para esta etapa.\n"
            "- La respuesta está generada por el proveedor bootstrap para validar el workflow.\n\n"
            "### Incertidumbres\n\n"
            "- Falta conectar el adaptador real de Ollama o DeepSeek para inferencia semántica.\n"
        )
        return self._output(answer, prompt)

    async def synthesize(
        self,
        request: TaskCreateRequest,
        arbiter: ModelReference,
        proposals: list[ModelOutput],
    ) -> ModelOutput:
        joined = "\n\n".join(output.content for output in proposals)
        answer = (
            "# Síntesis de Consenso Rápido\n\n"
            "El workflow `mixture_of_agents/fast` completó propuestas independientes y una síntesis directa.\n\n"
            "## Respuesta final\n\n"
            f"{request.content.prompt.strip()}\n\n"
            "## Señales combinadas\n\n"
            f"Se integraron {len(proposals)} propuestas. Esta síntesis usa el proveedor bootstrap "
            "hasta que se conecten los adaptadores LLM reales.\n\n"
            "## Propuestas consideradas\n\n"
            f"{joined}\n"
        )
        return self._output(answer, request.content.prompt)

    def _output(self, content: str, prompt: str) -> ModelOutput:
        now = datetime.now(timezone.utc)
        tokens_input = max(1, len(prompt) // 4)
        tokens_output = max(1, len(content) // 4)
        latency_ms = max(1.0, (datetime.now(timezone.utc) - now).total_seconds() * 1000)
        return ModelOutput(
            content=content,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=0.0,
            latency_ms=latency_ms,
        )

