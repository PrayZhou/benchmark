"""Re-execute a recorded trial's actions and report divergences.

Replay is a determinism check: it re-runs the *actions* the agent took, not the
agent itself, and compares each result with the recorded trace. It never
re-scores the outcome — that is ``regrade``'s job.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .factories import build_environment
from .schemas import TaskSpec, TraceEvent

# The trace recorder appends this marker when a payload was cut. A truncated
# stdout cannot be compared byte-for-byte, so replay falls back to exit status.
TRUNCATION_MARKER = "...[truncated "


def load_events(trace_path: Path) -> list[TraceEvent]:
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    return [TraceEvent.model_validate_json(line) for line in lines if line.strip()]


def _load_trial_context(run_dir: Path, trial_id: str) -> tuple[dict[str, Any], TaskSpec]:
    run_dir = run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    original = next(item for item in manifest.get("trials", []) if item["trial_id"] == trial_id)
    task_data = next(item for item in manifest.get("tasks", []) if item["id"] == original["task_id"])
    return original, TaskSpec.model_validate(task_data)


def _stdout_comparable(recorded: Any, actual: Any) -> bool:
    return (
        isinstance(recorded, str) and TRUNCATION_MARKER not in recorded and isinstance(actual, str)
    )


def _matches(expected: Any, actual: Any) -> bool:
    """Compare a replayed result with the recorded one.

    ``ok`` and ``returncode`` are always comparable; ``stdout`` only when the
    recorded value survived trace truncation intact.
    """
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return expected == actual
    if expected.get("ok") != actual.get("ok"):
        return False
    if "returncode" in expected and expected.get("returncode") != actual.get("returncode"):
        return False
    recorded_stdout = expected.get("stdout")
    if _stdout_comparable(recorded_stdout, actual.get("stdout")) and recorded_stdout != actual.get("stdout"):
        return False
    return True


def replay_trial(run_dir: Path, trial_id: str, *, execute: bool = True,
                 timeout: float | None = None) -> dict[str, Any]:
    """Replay one trial.

    ``execute=False`` returns the recorded actions under ``mode: "list"`` — a
    listing, never presented as a replay.
    """
    run_dir = run_dir.resolve()
    trace = run_dir / "traces" / f"{trial_id}.jsonl"
    if not trace.exists():
        raise FileNotFoundError(f"trace not found: {trace}")
    original, task = _load_trial_context(run_dir, trial_id)
    events = load_events(trace)
    actions = [event for event in events if event.type == "action"]
    recorded = {event.step: event.payload for event in events if event.type == "tool_result"}

    if not execute:
        return {
            "trial_id": trial_id,
            "mode": "list",
            "executed": False,
            "action_count": len(actions),
            "actions": [event.payload for event in actions],
        }

    workspace = Path(tempfile.mkdtemp(prefix="replay-"))
    environment = None
    steps: list[dict[str, Any]] = []
    try:
        environment = build_environment(task, workspace)
        environment.reset(task)
        for event in actions:
            actual = environment.execute(event.payload, timeout=timeout)
            expected = recorded.get(event.step)
            steps.append({
                "step": event.step,
                "action": event.payload,
                "expected": expected,
                "actual": actual,
                "match": _matches(expected, actual),
            })
    finally:
        if environment is not None:
            environment.cleanup()
        shutil.rmtree(workspace, ignore_errors=True)

    divergences = [step for step in steps if not step["match"]]
    return {
        "trial_id": trial_id,
        "mode": "replay",
        "executed": True,
        "action_count": len(actions),
        "steps": steps,
        "divergences": divergences,
        "source_trace": original.get("trace_path"),
    }


def write_replay(run_dir: Path, trial_id: str, *, execute: bool = True,
                 timeout: float | None = None) -> Path:
    output = run_dir / "replays" / f"{trial_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = replay_trial(run_dir, trial_id, execute=execute, timeout=timeout)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output
