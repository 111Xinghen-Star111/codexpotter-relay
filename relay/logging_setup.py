"""Structured file logging for the Relay Controller.

Logs are written to ``<relay home>/logs`` as line-oriented JSON so a crashed
run can still be audited afterwards. A human-readable mirror is printed to the
console by the CLI, never by library code.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .paths import log_dir


class JsonLineFormatter(logging.Formatter):
    """Render a log record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "relay_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    name: str,
    *,
    to_console: bool = False,
    level: int = logging.INFO,
) -> logging.Logger:
    """Attach a JSONL file handler plus an optional console handler.

    Calling this twice for the same ``name`` does not duplicate handlers, so the
    CLI and the controller can both ask for a logger safely.
    """
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / f"{name}.jsonl"

    logger = logging.getLogger(f"relay.{name}")
    logger.setLevel(level)
    logger.propagate = False

    already = {getattr(handler, "_relay_target", None) for handler in logger.handlers}
    if str(log_file) not in already:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonLineFormatter())
        file_handler._relay_target = str(log_file)  # type: ignore[attr-defined]
        logger.addHandler(file_handler)

    if to_console and "console" not in already:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        console._relay_target = "console"  # type: ignore[attr-defined]
        logger.addHandler(console)

    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log ``message`` together with structured ``fields``."""
    logger.log(level, message, extra={"relay_fields": fields})


def utc_timestamp() -> float:
    """Return the current wall-clock time as a float (seconds)."""
    return time.time()
