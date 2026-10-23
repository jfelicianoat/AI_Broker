"""El tipo de tarea que ve el proveedor es el mismo con que se registra la
invocación: de él depende a qué segmento se aplica la marca de razonamiento
desbocado (app.providers.ollama)."""
from __future__ import annotations

from pathlib import Path

from app.schemas import TaskCreateRequest
from app.task_classifier import invocation_task_type
from tests.test_model_quarantine import BrokenProposerProvider, _moa_request, _run

CODE_PROMPT = "Revisa este código:\n```python\ndef f(x):\n    return x / 0\n```"


class TaskTypeSpy(BrokenProposerProvider):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    async def propose(self, request, model, ordinal):
        # Como un trozo de map-reduce: una sub-petición que, clasificada
        # suelta, saldría de otro tipo que la tarea.
        chunk = request.model_copy(update={
            "content": request.content.model_copy(update={"prompt": "Resume este fragmento"}),
        })
        self.seen.append(invocation_task_type(chunk))
        return await super().propose(request, model, ordinal)


def test_provider_sees_the_task_type_the_invocation_is_recorded_with(tmp_path: Path, monkeypatch) -> None:
    import tests.test_model_quarantine as harness

    monkeypatch.setattr(harness, "_moa_request", lambda: _moa_request(content={"prompt": CODE_PROMPT}))
    provider = TaskTypeSpy()
    state, db = _run(tmp_path, provider)
    assert state["status"] == "completed"
    assert provider.seen and set(provider.seen) == {"code"}
    recorded = {row["task_type"] for row in db.query_all("SELECT task_type FROM model_invocations")}
    assert recorded == {"code"}
    # Al acabar la tarea el ámbito se cierra: lo siguiente se clasifica solo.
    prose = TaskCreateRequest.model_validate({"idempotency_key": "x", "content": {"prompt": "Haz unos apuntes"}})
    assert invocation_task_type(prose) == "prose"
