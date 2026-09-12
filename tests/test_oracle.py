"""`benchmark oracle` runs each task's reference solution through its verifier."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from benchmark import cli
from benchmark.oracle import run_oracles, write_oracles
from benchmark.tasks import load_task_dir

VERIFIER = "test -f artifact.txt"


def make_task_dir(root: Path, name: str, *, oracle: str | None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "instruction.md").write_text("Create artifact.txt.\n")
    (directory / "tests").mkdir()
    (directory / "tests" / "test.sh").write_text(VERIFIER + "\n")
    if oracle is not None:
        (directory / "oracle.sh").write_text(oracle + "\n")
    return directory


def test_oracle_that_satisfies_the_verifier_is_reported_as_passed(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, "passes", oracle="printf ok > artifact.txt\necho oracle-ran")
    task = load_task_dir(directory)
    # The fixtures deliberately omit environment_type, so TaskSpec defaults to
    # "docker"; a passing result proves the oracle forced a local environment.
    assert task.environment_type == "docker"

    [entry] = run_oracles([task], tmp_path / "work")

    assert entry["task_id"] == "passes"
    assert entry["oracle"] is not None
    assert Path(entry["oracle"]).name == "oracle.sh"
    assert entry["verifier_passed"] is True
    assert "oracle-ran" in entry["stdout"]
    assert entry["stderr"] == ""


def test_oracle_that_misses_the_artifact_fails_the_verifier(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, "fails", oracle="mkdir -p elsewhere")
    task = load_task_dir(directory)

    [entry] = run_oracles([task], tmp_path / "work")

    assert entry["oracle"] is not None
    assert entry["verifier_passed"] is False


def test_task_without_an_oracle_is_skipped_not_an_error(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, "no_oracle", oracle=None)
    task = load_task_dir(directory)

    [entry] = run_oracles([task], tmp_path / "work")

    assert entry["task_id"] == "no_oracle"
    assert entry["oracle"] is None
    assert entry["verifier_passed"] is None


def test_run_oracles_cleans_up_its_temp_workspaces(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, "passes", oracle="printf ok > artifact.txt")
    root = tmp_path / "work"

    run_oracles([load_task_dir(directory)], root)

    assert list(root.iterdir()) == []


def test_write_oracles_writes_json(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, "passes", oracle="printf ok > artifact.txt")
    results = run_oracles([load_task_dir(directory)], tmp_path / "work")

    output = write_oracles(results, tmp_path / "reports")

    assert output.name == "oracles.json"
    assert json.loads(output.read_text(encoding="utf-8"))[0]["verifier_passed"] is True


def test_cli_oracle_prints_the_report_and_exits_zero_when_all_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_task_dir(tmp_path, "passes", oracle="printf ok > artifact.txt")
    monkeypatch.setattr(sys, "argv", [
        "benchmark", "oracle", "--tasks-root", str(tmp_path), "--output", str(tmp_path / "reports"),
    ])

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["verifier_passed"] is True
    assert (tmp_path / "reports" / "oracles.json").exists()


def test_cli_oracle_exits_nonzero_when_a_task_fails_its_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_task_dir(tmp_path, "passes", oracle="printf ok > artifact.txt")
    make_task_dir(tmp_path, "fails", oracle="mkdir -p elsewhere")
    monkeypatch.setattr(sys, "argv", [
        "benchmark", "oracle", "--tasks-root", str(tmp_path), "--output", str(tmp_path / "reports"),
    ])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
