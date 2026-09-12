"""Replay re-executes recorded actions and reports divergences."""
import json
import sys
from pathlib import Path

import pytest

from benchmark.orchestrator import run_tasks
from benchmark.replay import replay_trial
from benchmark.schemas import RunConfig, TaskSpec


def _scripted_run(root: Path, *, trace_max_chars: int = 20_000) -> tuple[Path, str]:
    task = TaskSpec(
        id="replay.marker",
        instruction="create a marker",
        environment_type="local",
        environment={"actions": [
            {"type": "command", "command": ["bash", "-lc", "printf READY > marker.txt"]},
            {"type": "command", "command": ["bash", "-lc", "test \"$(cat marker.txt)\" = READY"]},
            {"type": "finish"},
        ]},
        grader={"command": ["bash", "-lc", "test -f marker.txt"]},
    )
    output = root / "out"
    results = run_tasks([task], root, output, RunConfig(trace_max_chars=trace_max_chars))
    return output, results[0].trial_id


def test_replay_re_executes_every_action_without_divergence(tmp_path: Path) -> None:
    output, trial_id = _scripted_run(tmp_path)

    payload = replay_trial(output, trial_id)

    assert payload["mode"] == "replay"
    assert payload["executed"] is True
    assert payload["action_count"] == 3
    assert [step["step"] for step in payload["steps"]] == [0, 1, 2]
    assert payload["divergences"] == []


def test_replay_reports_a_diverging_action(tmp_path: Path) -> None:
    output, trial_id = _scripted_run(tmp_path)
    trace = output / "traces" / f"{trial_id}.jsonl"
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    for event in events:
        if event["type"] == "action" and event["step"] == 1:
            event["payload"] = {"type": "command", "command": ["bash", "-lc", "exit 3"]}
    trace.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    payload = replay_trial(output, trial_id)

    assert len(payload["divergences"]) == 1
    divergence = payload["divergences"][0]
    assert divergence["step"] == 1
    assert divergence["expected"]["returncode"] == 0
    assert divergence["actual"]["returncode"] == 3


def test_truncated_stdout_is_not_a_divergence(tmp_path: Path) -> None:
    """Trace truncation is lossy by design; replay must not flag it as drift."""
    task = TaskSpec(
        id="replay.noisy",
        instruction="be noisy",
        environment_type="local",
        environment={"actions": [
            {"type": "command", "command": ["bash", "-lc", "printf '%0.sx' {1..200}"]},
            {"type": "finish"},
        ]},
        grader={"command": ["bash", "-lc", "true"]},
    )
    output = tmp_path / "out"
    results = run_tasks([task], tmp_path, output, RunConfig(trace_max_chars=30))

    payload = replay_trial(output, results[0].trial_id)

    recorded = payload["steps"][0]["expected"]["stdout"]
    assert "truncated" in recorded
    assert payload["divergences"] == []


def test_no_execute_lists_actions_instead_of_replaying(tmp_path: Path) -> None:
    output, trial_id = _scripted_run(tmp_path)

    payload = replay_trial(output, trial_id, execute=False)

    assert payload["mode"] == "list"
    assert payload["executed"] is False
    assert payload["action_count"] == 3
    assert payload["actions"][0]["type"] == "command"
    assert "steps" not in payload


def test_replay_requires_a_trace(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="trace not found"):
        replay_trial(tmp_path / "run", "missing")


def test_cli_replay_no_execute_writes_a_listing(tmp_path: Path, monkeypatch, capsys) -> None:
    from benchmark import cli

    output, trial_id = _scripted_run(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["benchmark", "replay", str(output), trial_id, "--no-execute"])
    cli.main()

    written = Path(capsys.readouterr().out.strip())
    payload = json.loads(written.read_text())
    assert payload["mode"] == "list"
