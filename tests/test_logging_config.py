import json
import logging

import pytest

from benchmark.logging_config import JsonFormatter, coerce_level, configure_logging


def test_coerce_level_accepts_names_and_numbers() -> None:
    assert coerce_level("debug") == logging.DEBUG
    assert coerce_level("INFO") == logging.INFO
    assert coerce_level(logging.ERROR) == logging.ERROR
    with pytest.raises(ValueError):
        coerce_level("NOPE")


def test_configure_logging_is_scoped_and_idempotent() -> None:
    logger = configure_logging("DEBUG")
    assert logger.name == "benchmark"
    assert logger.level == logging.DEBUG
    assert logger.propagate is False

    logger = configure_logging("INFO")
    assert logger.level == logging.INFO
    # Re-configuring replaces our handler instead of stacking a second one.
    assert len(logger.handlers) == 1


def test_json_formatter_includes_record_context(capsys) -> None:
    logger = configure_logging("INFO", json_format=True)
    logger.info("run started", extra={"event": "run_start", "run_id": "r1"})

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["message"] == "run started"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "benchmark"
    assert payload["event"] == "run_start"
    assert payload["run_id"] == "r1"


def test_json_formatter_omits_absent_context() -> None:
    record = logging.LogRecord("benchmark", logging.INFO, __file__, 1, "hi", None, None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hi"
    assert "run_id" not in payload
