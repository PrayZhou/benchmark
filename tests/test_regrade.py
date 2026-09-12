"""Regrade re-scores the archived artifact snapshot, explicitly."""
import json
import shutil
import sys
from pathlib import Path

import pytest

from benchmark.adapters.graders.command import CommandGrader
from benchmark.orchestrator import run_tasks
from benchmark.regrade import regrade_trial
from benchmark.schemas import TaskSpec


def _scripted_run(root: Path) -> tuple[Path, str]:
    task = TaskSpec(
        id="regrade.marker",
        instruction="create a marker",
        environment_type="local",
        environment={"actions": [
            {"type": "command", "command": ["bash", "-lc", "printf READY > marker.txt"]},
            {"type": "finish"},
        ]},
        grader={"command": ["bash", "-lc", "test -f marker.txt"]},
    )
    output = root / "out"
    results = run_tasks([task], root, output)
    return output, results[0].trial_id


def test_regrade_scores_the_artifact_snapshot(tmp_path: Path) -> None:
    output, trial_id = _scripted_run(tmp_path)

    payload = regrade_trial(output, trial_id,
                            CommandGrader(["bash", "-lc", "test \"$(cat marker.txt)\" = READY"], isolated=False))

    assert payload["mode"] == "artifact_snapshot"
    assert payload["artifacts_found"] is True
    assert payload["artifact_index"] is not None
    assert payload["grader_isolated"] is False
    assert payload["regrade"]["success"] is True


def test_regrade_refuses_to_score_a_missing_snapshot(tmp_path: Path) -> None:
    output, trial_id = _scripted_run(tmp_path)
    shutil.rmtree(output / "artifacts")

    with pytest.raises(FileNotFoundError, match="no artifact snapshot"):
        regrade_trial(output, trial_id, CommandGrader(["bash", "-lc", "true"], isolated=False))


def test_cli_regrade_keeps_leading_dash_verifier_args(tmp_path: Path, monkeypatch, capsys) -> None:
    """``--command bash -lc ...`` must not have ``-lc`` parsed as an option."""
    from benchmark import cli

    output, trial_id = _scripted_run(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "benchmark", "regrade", str(output), trial_id,
        "--command", "bash", "-lc", "test -f marker.txt",
    ])
    cli.main()

    written = Path(capsys.readouterr().out.strip())
    payload = json.loads(written.read_text())
    assert payload["mode"] == "artifact_snapshot"
    assert payload["regrade"]["success"] is True
