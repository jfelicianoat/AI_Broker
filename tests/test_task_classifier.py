"""Cobertura del clasificador heurístico de tipo de tarea (app.task_classifier)."""
import unittest
from uuid import uuid4

from app.schemas import TaskCreateRequest
from app.task_classifier import LONG_CONTEXT_TOKEN_THRESHOLD, classify_task_type


def _request(prompt: str) -> TaskCreateRequest:
    return TaskCreateRequest(
        idempotency_key=f"classify:{uuid4().hex}",
        content={"prompt": prompt},
        model_requirements={"allowed_providers": ["ollama"]},
    )


class ClassifyTaskTypeTests(unittest.TestCase):
    def test_short_conversational_prompt_is_prose(self) -> None:
        self.assertEqual(classify_task_type(_request("Hola, ¿qué tal estás hoy?")), "prose")

    def test_code_fence_is_code(self) -> None:
        prompt = "Revisa este fragmento:\n```python\nprint('hola')\n```"
        self.assertEqual(classify_task_type(_request(prompt)), "code")

    def test_code_keyword_is_code(self) -> None:
        self.assertEqual(
            classify_task_type(_request("Hazme un programa en Python y depúralo si falla")),
            "code",
        )

    def test_file_extension_is_code(self) -> None:
        self.assertEqual(classify_task_type(_request("Revisa main.py y dime qué falla")), "code")

    def test_long_prompt_without_code_signal_is_long_context(self) -> None:
        filler = "lorem ipsum dolor sit amet consectetur adipiscing elit. " * 400
        prompt = f"Resume esta transcripción:\n{filler}"
        self.assertGreaterEqual(len(prompt), LONG_CONTEXT_TOKEN_THRESHOLD)
        self.assertEqual(classify_task_type(_request(prompt)), "long_context")

    def test_code_signal_wins_over_long_context(self) -> None:
        filler = "lorem ipsum dolor sit amet consectetur adipiscing elit. " * 400
        prompt = f"```python\nimport os\n```\n{filler}"
        self.assertEqual(classify_task_type(_request(prompt)), "code")
