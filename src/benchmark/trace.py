from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .schemas import TraceEvent

logger = logging.getLogger(__name__)

# Default per-string cap for persisted trace payloads. Environment stdout and
# tool results are otherwise unbounded: a single chatty command could write a
# multi-megabyte JSONL line. The in-memory observation caps the LLM side; this
# cap keeps the on-disk trajectory bounded too.
DEFAULT_MAX_PAYLOAD_CHARS = 20_000


def truncate_text(value: str, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    return f"{value[:limit]}\n...[truncated {len(value) - limit} chars]"


def truncate_payload(value: Any, limit: int) -> Any:
    """Recursively cap every string in a JSON-like payload.

    Structure (dict keys, list order) is preserved so replay and indexing keep
    working on a truncated trace.
    """
    if isinstance(value, str):
        return truncate_text(value, limit)
    if isinstance(value, dict):
        return {key: truncate_payload(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [truncate_payload(item, limit) for item in value]
    return value


class JsonlTraceRecorder:
    def __init__(self, path: Path, *, max_payload_chars: int = DEFAULT_MAX_PAYLOAD_CHARS) -> None:
        if max_payload_chars < 1:
            raise ValueError("max_payload_chars must be positive")
        self.path = path
        self.max_payload_chars = max_payload_chars
        self.events: list[TraceEvent] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: TraceEvent) -> None:
        payload = truncate_payload(event.payload, self.max_payload_chars)
        if payload != event.payload:
            logger.debug(
                "trace payload truncated",
                extra={"event": "trace_truncated", "step": event.step, "type": event.type},
            )
            event = event.model_copy(update={"payload": payload})
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
