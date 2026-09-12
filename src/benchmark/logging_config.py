"""Opt-in logging configuration for the harness.

Importing the package never touches the root logger: an embedding application
keeps control of its own logging. ``configure_logging`` is called by the CLI
and only configures the ``benchmark`` logger namespace, so it stays safe to
call repeatedly and does not disturb third-party handlers.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

LOGGER_NAMESPACE = "benchmark"
DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Record attributes promoted into the structured payload when present.
_CONTEXT_FIELDS = ("event", "run_id", "trial_id", "task_id", "attempt")


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record for machine-readable log collection."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def coerce_level(level: str | int) -> int:
    """Translate a CLI-style level name into a :mod:`logging` level number."""
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    if not isinstance(resolved, int):
        raise ValueError(f"unknown log level: {level!r}")
    return resolved


def configure_logging(level: str | int = "WARNING", *, json_format: bool = False,
                      stream: TextIO | None = None) -> logging.Logger:
    """Configure the ``benchmark`` logger and return it.

    Only handlers owned by this namespace are replaced; handlers attached to
    the root logger (e.g. by a host application or pytest) are left alone.
    """
    logger = logging.getLogger(LOGGER_NAMESPACE)
    logger.setLevel(coerce_level(level))
    # Keep benchmark records from double-logging through a host root handler.
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if json_format else logging.Formatter(DEFAULT_FORMAT))
    logger.addHandler(handler)
    return logger
