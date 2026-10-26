from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from logging.handlers import BaseRotatingHandler
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
            "stage",
            "role",
            "provider",
            "deployment",
            "model",
            "action",
            "errors",
            "use_case",
            "latency_ms",
            "confidence",
            "decision",
            "fallback_used",
            "reason_code",
            "shadow_mode",
            "previous_model",
            "proposed_model",
            "tokens_input",
            "tokens_output",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(config: LoggingConfig) -> Path:
    log_dir = Path(config.directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.level.upper(), logging.INFO)

    formatter = JsonLineFormatter()
    file_handler = DailyFileHandler(
        log_dir,
        config.filename,
        max_bytes=config.max_bytes,
        keep_forever=config.keep_forever,
        retention_days=config.retention_days,
    )
    log_path = file_handler.current_path
    handlers: list[logging.Handler] = [file_handler]
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


class DailyFileHandler(BaseRotatingHandler):
    """Un fichero por día con la fecha en el nombre, y poda por antigüedad.

    `ai-broker.log` se escribe como `ai-broker-2026-10-25.log`. Si un día pasa
    de `max_bytes`, sigue en `ai-broker-2026-10-25.1.log`, `.2`… sin perder
    nada: el tope parte el día, no lo recorta. La fecha es la local de la
    máquina (la del día del operador), aunque las líneas lleven hora UTC.

    La rotación anterior era por tamaño con 5 copias de 10 MB: con el sondeo de
    los clientes (casi todo `http.request`) eso eran unas 13 horas de log, y lo
    de la víspera ya no estaba al ir a buscarlo. Ahora lo que se conserva se
    mide en días (`retention_days`) o no se borra nunca (`keep_forever`).
    """

    def __init__(
        self,
        directory: Path,
        filename: str,
        *,
        max_bytes: int = 0,
        keep_forever: bool = False,
        retention_days: int = 30,
    ) -> None:
        self.directory = Path(directory)
        base = Path(filename)
        self.stem = base.stem
        self.suffix = base.suffix or ".log"
        self.legacy_name = base.name
        self.max_bytes = max_bytes
        self.keep_forever = keep_forever
        self.retention_days = retention_days
        self._pattern = re.compile(
            rf"^{re.escape(self.stem)}-(\d{{4}}-\d{{2}}-\d{{2}})(?:\.(\d+))?{re.escape(self.suffix)}$"
        )
        self.day = date.today()
        self.part = self._last_part(self.day)
        super().__init__(str(self._path_for(self.day, self.part)), "a", encoding="utf-8")
        self.prune()

    @property
    def current_path(self) -> Path:
        return Path(self.baseFilename)

    def _path_for(self, day: date, part: int) -> Path:
        index = f".{part}" if part else ""
        return self.directory / f"{self.stem}-{day.isoformat()}{index}{self.suffix}"

    def _last_part(self, day: date) -> int:
        """Al arrancar se sigue escribiendo en el último trozo del día."""
        parts = [0]
        for path in self.directory.glob(f"{self.stem}-{day.isoformat()}*{self.suffix}"):
            match = self._pattern.match(path.name)
            if match and match.group(1) == day.isoformat():
                parts.append(int(match.group(2) or 0))
        return max(parts)

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if datetime.fromtimestamp(record.created).date() != self.day:
            return True
        if self.max_bytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()
        if self.stream.tell() == 0:
            return False
        message = f"{self.format(record)}{self.terminator}"
        return self.stream.tell() + len(message.encode("utf-8")) >= self.max_bytes

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        today = date.today()
        if today != self.day:
            self.day, self.part = today, 0
        else:
            self.part += 1
        self.baseFilename = str(self._path_for(self.day, self.part).absolute())
        self.stream = self._open()
        self.prune()

    def prune(self) -> list[Path]:
        """Borra lo que tiene más de `retention_days` días; nada si es para siempre.

        Entran también los ficheros del esquema viejo (`ai-broker.log`,
        `ai-broker.log.1`…), juzgados por su fecha de modificación: si no, se
        quedarían en el directorio para siempre aunque la retención diga otra
        cosa. El fichero abierto nunca se toca.
        """
        if self.keep_forever:
            return []
        cutoff = date.today() - timedelta(days=self.retention_days)
        legacy = re.compile(rf"^{re.escape(self.legacy_name)}(?:\.\d+)?$")
        removed: list[Path] = []
        for path in self.directory.iterdir():
            if not path.is_file() or path.absolute() == self.current_path.absolute():
                continue
            match = self._pattern.match(path.name)
            try:
                if match:
                    day = date.fromisoformat(match.group(1))
                elif legacy.match(path.name):
                    day = datetime.fromtimestamp(path.stat().st_mtime).date()
                else:
                    continue
                if day < cutoff:
                    path.unlink()
                    removed.append(path)
            except (OSError, ValueError):
                # Un fichero que otro proceso tiene abierto (Windows no deja
                # borrarlo) se intenta otra vez en la siguiente rotación.
                continue
        return removed


def _redact(value: str) -> str:
    return SENSITIVE_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", value)
