import json
from pathlib import Path

import pytest

from benchmark.adapters.agents.scripted import ScriptedAgent
from benchmark.adapters.environments.local import LocalEnvironment
from benchmark.adapters.graders.command import CommandGrader
from benchmark.runner import run_trial
from benchmark.schemas import TaskSpec, TraceEvent
from benchmark.trace import JsonlTraceRecorder, truncate_payload, truncate_text


def test_truncate_text_marks_what_was_cut() -> None:
    assert truncate_text("short", 10) == "short"
    truncated = truncate_text("x" * 30, 10)
    assert truncated.startswith("x" * 10)
    assert "truncated 20 chars" in truncated


def test_truncate_payload_preserves_structure() -> None:
    payload = {"stdout": "x" * 40, "returncode": 0, "nested": {"log": "y" * 40}, "items": ["z" * 40]}
    result = truncate_payload(payload, 10)
    assert result["returncode"] == 0
    assert result["nested"]["log"].startswith("y" * 10)
    assert result["items"][0].startswith("z" * 10)
    # the original payload is untouched
    assert payload["stdout"] == "x" * 40


def test_recorder_writes_truncated_events_consistently(tmp_path: Path) -> None:
    recorder = JsonlTraceRecorder(tmp_path / "trace.jsonl", max_payload_chars=10)
    recorder.record(TraceEvent(type="tool_result", step=0, payload={"stdout": "x" * 100, "returncode": 0}))

    line = json.loads((tmp_path / "trace.jsonl").read_text().strip())
    assert line["payload"]["returncode"] == 0
    assert line["payload"]["stdout"].startswith("x" * 10)
    # what the grader sees in memory matches what hit the disk
    assert recorder.events[0].payload["stdout"] == line["payload"]["stdout"]


def test_recorder_rejects_a_non_positive_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        JsonlTraceRecorder(tmp_path / "trace.jsonl", max_payload_chars=0)


def test_run_trial_bounds_the_persisted_trace(tmp_path: Path) -> None:
    """A chatty command must not write an unbounded JSONL line."""
    task = TaskSpec(
        id="test.noisy",
        instruction="be noisy",
        environment={"actions": [
            {"type": "command", "command": ["bash", "-lc", "printf '%0.sx' {1..400}"]},
            {"type": "finish"},
        ]},
        grader={"command": ["bash", "-lc", "true"]},
    )
    run_trial(
        task,
        LocalEnvironment(tmp_path),
        ScriptedAgent(task.environment["actions"]),
        CommandGrader(task.grader["command"], isolated=False),
        tmp_path / "traces",
        trial_id="trial-noisy",
        trace_max_chars=50,
    )
    events = [json.loads(line) for line in (tmp_path / "traces" / "trial-noisy.jsonl").read_text().splitlines()]
    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert tool_results
    assert len(tool_results[0]["payload"]["stdout"]) < 400
    assert "truncated" in tool_results[0]["payload"]["stdout"]
