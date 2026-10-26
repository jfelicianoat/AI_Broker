"""Logs diarios con la fecha en el nombre y poda por antigüedad."""
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import LoggingConfig
from app.logging_config import DailyFileHandler


def _record(message: str, when: datetime | None = None) -> logging.LogRecord:
    record = logging.LogRecord("ai_broker", logging.INFO, __file__, 1, message, None, None)
    if when is not None:
        record.created = when.timestamp()
    return record


def _touch(path: Path, *, days_ago: int = 0) -> Path:
    path.write_text("{}\n", encoding="utf-8")
    if days_ago:
        stamp = time.time() - days_ago * 86400
        os.utime(path, (stamp, stamp))
    return path


def test_file_name_carries_the_local_date(tmp_path: Path) -> None:
    handler = DailyFileHandler(tmp_path, "ai-broker.log")
    try:
        handler.emit(_record("hola"))
    finally:
        handler.close()

    assert handler.current_path.name == f"ai-broker-{date.today().isoformat()}.log"
    assert "hola" in handler.current_path.read_text(encoding="utf-8")


def test_size_cap_splits_the_day_instead_of_dropping_lines(tmp_path: Path) -> None:
    handler = DailyFileHandler(tmp_path, "ai-broker.log", max_bytes=200)
    try:
        for index in range(20):
            handler.emit(_record(f"linea-{index:02d}-" + "x" * 40))
    finally:
        handler.close()

    today = date.today().isoformat()
    parts = sorted(tmp_path.glob(f"ai-broker-{today}*.log"))
    assert len(parts) > 1
    assert (tmp_path / f"ai-broker-{today}.1.log").exists()
    combined = "".join(path.read_text(encoding="utf-8") for path in parts)
    assert all(f"linea-{index:02d}-" in combined for index in range(20))


def test_restart_appends_to_the_last_part_of_the_day(tmp_path: Path) -> None:
    today = date.today().isoformat()
    _touch(tmp_path / f"ai-broker-{today}.log")
    _touch(tmp_path / f"ai-broker-{today}.3.log")

    handler = DailyFileHandler(tmp_path, "ai-broker.log", max_bytes=10_000)
    handler.close()

    assert handler.current_path.name == f"ai-broker-{today}.3.log"


def test_day_change_opens_a_new_dated_file(tmp_path: Path) -> None:
    handler = DailyFileHandler(tmp_path, "ai-broker.log")
    try:
        # Simula que el handler se abrió ayer: el siguiente registro es de hoy.
        handler.day = date.today() - timedelta(days=1)
        handler.emit(_record("de hoy"))
    finally:
        handler.close()

    assert handler.day == date.today()
    assert handler.current_path.name == f"ai-broker-{date.today().isoformat()}.log"


def test_prune_removes_files_older_than_retention(tmp_path: Path) -> None:
    today = date.today()
    old = _touch(tmp_path / f"ai-broker-{(today - timedelta(days=31)).isoformat()}.log")
    old_part = _touch(tmp_path / f"ai-broker-{(today - timedelta(days=40)).isoformat()}.2.log")
    kept = _touch(tmp_path / f"ai-broker-{(today - timedelta(days=30)).isoformat()}.log")
    legacy_old = _touch(tmp_path / "ai-broker.log.4", days_ago=45)
    legacy_recent = _touch(tmp_path / "ai-broker.log.1")
    foreign = _touch(tmp_path / "restart-console.log", days_ago=90)

    handler = DailyFileHandler(tmp_path, "ai-broker.log", retention_days=30)
    handler.close()

    assert not old.exists()
    assert not old_part.exists()
    assert not legacy_old.exists()
    assert kept.exists()
    assert legacy_recent.exists()
    # Solo se tocan los ficheros de este log, nunca otros del directorio.
    assert foreign.exists()


def test_keep_forever_never_deletes(tmp_path: Path) -> None:
    ancient = _touch(tmp_path / "ai-broker-2020-01-01.log")
    legacy = _touch(tmp_path / "ai-broker.log.5", days_ago=400)

    handler = DailyFileHandler(tmp_path, "ai-broker.log", keep_forever=True, retention_days=1)
    handler.close()

    assert ancient.exists()
    assert legacy.exists()


def test_config_defaults_keep_a_month() -> None:
    config = LoggingConfig()
    assert config.keep_forever is False
    assert config.retention_days == 30


def test_old_yaml_with_backup_count_still_loads() -> None:
    config = LoggingConfig.model_validate({"max_bytes": 1024, "backup_count": 5})
    assert config.max_bytes == 1024
    assert not hasattr(config, "backup_count")
