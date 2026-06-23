from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    sha256: str
    size_bytes: int


class ArtifactStore:
    def __init__(self, root: str | Path = "state/tasks") -> None:
        self.root = Path(root)

    def write_markdown(self, task_id: str, relative_path: str, content: str) -> ArtifactRecord:
        return self.write_text(task_id, relative_path, content)

    def write_text(self, task_id: str, relative_path: str, content: str) -> ArtifactRecord:
        task_root = self.root / task_id
        target = task_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name

        os.replace(temp_name, target)
        data = target.read_bytes()
        return ArtifactRecord(
            path=str(target),
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
