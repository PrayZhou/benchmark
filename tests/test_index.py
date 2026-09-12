import json
import sqlite3
import sys
from pathlib import Path

import pytest

from benchmark.index import RunIndex
from benchmark.manifest import RunManifest
from benchmark.reporting import write_report
from benchmark.schemas import RunConfig, TaskSpec, TrialResult


def _trial(trial_id: str, *, task_id: str = "t", success: bool = True, attempt: int = 1,
           parent: str | None = None, score: float = 1.0) -> TrialResult:
    return TrialResult(
        task_id=task_id, trial_id=trial_id, agent_id="scripted", seed=0,
        status="completed" if success else "error", success=success, score=score,
        attempt=attempt, parent_trial_id=parent, duration_seconds=1.0,
        failure_category=None if success else "test_failure",
    )


def _make_run(root: Path, run_id: str, results: list[TrialResult], *, dataset: str | None = None) -> Path:
    task_id = results[0].task_id if results else "t"
    manifest = RunManifest(run_id, [TaskSpec(id=task_id, instruction="x")], root=root,
                           harness_version="0.1.0", run_config=RunConfig(runs_per_task=1, dataset=dataset))
    for result in results:
        manifest.add_trial(result)
    manifest.write()
    write_report(results, root, dataset=dataset)
    return root


def test_index_records_runs_and_trials(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path / "run1", "run-1", [_trial("t1"), _trial("t2", success=False, score=0.0)])
    index = RunIndex(tmp_path / "index.sqlite3")
    summary = index.index_run(run_dir)
    assert summary["run_id"] == "run-1"
    assert summary["success_rate"] == 0.5

    runs = index.list_runs()
    assert len(runs) == 1
    assert runs[0]["total"] == 2
    assert runs[0]["successful"] == 1
    assert runs[0]["failures_by_category"] == {"test_failure": 1}
    assert runs[0]["manifest_path"].endswith("manifest.json")


def test_indexing_is_idempotent_and_does_not_duplicate_trials(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path / "run1", "run-1", [_trial("t1")])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_run(run_dir)
    index.index_run(run_dir)

    assert len(index.list_runs()) == 1
    with sqlite3.connect(tmp_path / "index.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM trials WHERE run_id = 'run-1'").fetchone()[0]
    assert count == 1


def test_index_allows_the_same_trial_id_in_different_runs(tmp_path: Path) -> None:
    """Trial ids are deterministic per (task, seed, attempt); identity is (run_id, trial_id)."""
    _make_run(tmp_path / "run1", "run-1", [_trial("shared", task_id="alpha")])
    _make_run(tmp_path / "run2", "run-2", [_trial("shared", task_id="alpha", success=False, score=0.0)])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_runs([tmp_path / "run1", tmp_path / "run2"])

    runs = {run["run_id"]: run for run in index.list_runs()}
    assert runs["run-1"]["successful"] == 1
    assert runs["run-2"]["successful"] == 0


def test_index_compare_pivots_per_task(tmp_path: Path) -> None:
    _make_run(tmp_path / "run1", "run-1", [_trial("a1", task_id="alpha", parent="g1")])
    _make_run(tmp_path / "run2", "run-2", [
        _trial("b1", task_id="alpha", success=False, parent="g2", score=0.0),
        _trial("b2", task_id="beta", parent="g3"),
    ])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_runs([tmp_path / "run1", tmp_path / "run2"])

    payload = index.compare(["run-1", "run-2"])
    assert [run["run_id"] for run in payload["runs"]] == ["run-1", "run-2"]
    assert payload["tasks"]["alpha"]["run-1"]["success_rate"] == 1.0
    assert payload["tasks"]["alpha"]["run-2"]["success_rate"] == 0.0
    assert payload["tasks"]["beta"]["run-2"]["attempts"] == 1


def test_index_compare_can_omit_task_matrix(tmp_path: Path) -> None:
    _make_run(tmp_path / "run1", "run-1", [_trial("t1")])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_run(tmp_path / "run1")
    assert "tasks" not in index.compare(include_tasks=False)


def test_report_records_the_dataset_label(tmp_path: Path) -> None:
    _make_run(tmp_path / "run1", "run-1", [_trial("t1")], dataset="mmlu")
    payload = json.loads((tmp_path / "run1" / "report.json").read_text())
    assert payload["dataset"] == "mmlu"


def test_compare_filters_runs_by_dataset(tmp_path: Path) -> None:
    _make_run(tmp_path / "run1", "run-mmlu", [_trial("t1")], dataset="mmlu")
    _make_run(tmp_path / "run2", "run-gsm8k", [_trial("t2")], dataset="gsm8k")
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_runs([tmp_path / "run1", tmp_path / "run2"])

    payload = index.compare(dataset="mmlu")

    assert [run["run_id"] for run in payload["runs"]] == ["run-mmlu"]
    assert payload["runs"][0]["dataset"] == "mmlu"


def test_cli_compare_filters_by_dataset(tmp_path: Path, monkeypatch, capsys) -> None:
    from benchmark import cli

    _make_run(tmp_path / "run1", "run-mmlu", [_trial("t1")], dataset="mmlu")
    _make_run(tmp_path / "run2", "run-gsm8k", [_trial("t2")], dataset="gsm8k")
    db = tmp_path / "index.sqlite3"
    RunIndex(db).index_runs([tmp_path / "run1", tmp_path / "run2"])

    monkeypatch.setattr(sys, "argv", ["benchmark", "compare", "--db", str(db), "--dataset", "mmlu"])
    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert [run["run_id"] for run in payload["runs"]] == ["run-mmlu"]


def test_index_compare_rejects_unknown_run(tmp_path: Path) -> None:
    index = RunIndex(tmp_path / "index.sqlite3")
    with pytest.raises(KeyError):
        index.compare(["missing"])


def test_index_requires_a_manifest(tmp_path: Path) -> None:
    index = RunIndex(tmp_path / "index.sqlite3")
    with pytest.raises(FileNotFoundError):
        index.index_run(tmp_path / "empty")


def test_cli_index_and_compare_round_trip(tmp_path: Path, monkeypatch, capsys) -> None:
    from benchmark import cli

    _make_run(tmp_path / "run1", "run-1", [_trial("t1")])
    db = tmp_path / "index.sqlite3"

    monkeypatch.setattr(sys, "argv", ["benchmark", "index", str(tmp_path / "run1"), "--db", str(db)])
    cli.main()
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["indexed"][0]["run_id"] == "run-1"

    monkeypatch.setattr(sys, "argv", ["benchmark", "compare", "--db", str(db)])
    cli.main()
    compared = json.loads(capsys.readouterr().out)
    assert compared["runs"][0]["run_id"] == "run-1"
    assert compared["tasks"]["t"]["run-1"]["success_rate"] == 1.0
