from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.config import LoggingConfig


SENSITIVE_PATTERN = re.compile(
    r"(authorization|api[_-]?key|token|secret|password|credential)=([^,\s]+)",
    re.IGNORECASE,
)


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        for key in (
            "method",
            "path",
            "status_code",
            "duration_ms",
            "client",
            "event",
            "task_id",
            "code",
            "retryable",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(config: LoggingConfig) -> Path:
    log_dir = Path(config.directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / config.filename
    level = getattr(logging, config.level.upper(), logging.INFO)

    formatter = JsonLineFormatter()
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_path,
            maxBytes=config.max_bytes,
            backupCount=config.backup_count,
            encoding="utf-8",
        )
    ]
    if config.console_enabled:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(level)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)

    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("ai_broker").info("logging.configured", extra={"event": "logging.configured"})
    return log_path


def _redact(value: str) -> str:
    return SENSITIVE_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", value)
