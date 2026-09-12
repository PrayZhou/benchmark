import json
import sqlite3
import sys
from pathlib import Path

import pytest

from benchmark.index import RunIndex
from benchmark.manifest import RunManifest
from benchmark.reporting import write_report
from benchmark.schemas import Finding, RunConfig, TaskSpec, TrialResult

# The schema as it shipped before findings were indexed. Kept verbatim so the
# migration can be exercised against a real v1 database.
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL,
    created_at TEXT,
    harness_version TEXT,
    git_commit TEXT,
    python_version TEXT,
    platform TEXT,
    dataset TEXT,
    task_count INTEGER NOT NULL DEFAULT 0,
    trial_count INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    successful INTEGER NOT NULL DEFAULT 0,
    success_rate REAL NOT NULL DEFAULT 0,
    average_score REAL NOT NULL DEFAULT 0,
    average_duration_seconds REAL NOT NULL DEFAULT 0,
    total_attempts INTEGER NOT NULL DEFAULT 0,
    retried_groups INTEGER NOT NULL DEFAULT 0,
    failures_by_category TEXT NOT NULL DEFAULT '{}',
    config TEXT NOT NULL DEFAULT '{}',
    manifest_path TEXT,
    report_path TEXT
);
CREATE TABLE IF NOT EXISTS trials (
    trial_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    agent_id TEXT,
    seed INTEGER,
    run_index INTEGER,
    attempt INTEGER,
    parent_trial_id TEXT,
    status TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    failure_category TEXT,
    provider TEXT,
    model TEXT,
    termination_reason TEXT,
    trace_path TEXT,
    PRIMARY KEY (run_id, trial_id)
);
CREATE INDEX IF NOT EXISTS idx_trials_run_id ON trials(run_id);
CREATE INDEX IF NOT EXISTS idx_trials_task_id ON trials(task_id);
"""


def _finding(kind: str = "policy_violation", *, severity: str = "high", step: int | None = 1) -> Finding:
    return Finding(kind=kind, severity=severity, source="grader", evidence=f"evidence for {kind}", step=step)


def _trial(trial_id: str, *, task_id: str = "t", success: bool = True, attempt: int = 1,
           parent: str | None = None, score: float = 1.0,
           violations: list[Finding] | None = None,
           uncertainties: list[Finding] | None = None) -> TrialResult:
    return TrialResult(
        task_id=task_id, trial_id=trial_id, agent_id="scripted", seed=0,
        status="completed" if success else "error", success=success, score=score,
        attempt=attempt, parent_trial_id=parent, duration_seconds=1.0,
        failure_category=None if success else "test_failure",
        violations=violations or [], uncertainties=uncertainties or [],
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


def test_index_records_violations_and_uncertainties(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path / "run1", "run-1", [
        _trial("t1", violations=[_finding("edited_tests")], uncertainties=[_finding("judge_unsure", severity="low")]),
        _trial("t2", success=False, score=0.0,
               violations=[_finding("secret_exfil"), _finding("destructive_cmd")]),
    ])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_run(run_dir)

    runs = index.list_runs()
    assert runs[0]["violation_count"] >= 1
    assert runs[0]["violation_count"] == 3
    assert runs[0]["uncertain_trials"] == 1

    with sqlite3.connect(tmp_path / "index.sqlite3") as connection:
        trials = {
            row[0]: row
            for row in connection.execute(
                "SELECT trial_id, violations, uncertainties, violation_count FROM trials WHERE run_id = 'run-1'"
            )
        }
    assert trials["t1"][3] == 1
    assert trials["t2"][3] == 2
    assert [item["kind"] for item in json.loads(trials["t1"][1])] == ["edited_tests"]
    assert [item["kind"] for item in json.loads(trials["t2"][2])] == []


def test_reindexing_keeps_violation_counts_idempotent(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path / "run1", "run-1", [_trial("t1", violations=[_finding()])])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_run(run_dir)
    index.index_run(run_dir)

    assert index.list_runs()[0]["violation_count"] == 1
    with sqlite3.connect(tmp_path / "index.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM trials WHERE run_id = 'run-1'").fetchone()[0]
        total = connection.execute("SELECT SUM(violation_count) FROM trials WHERE run_id = 'run-1'").fetchone()[0]
    assert count == 1
    assert total == 1


def test_retry_does_not_launder_a_violation(tmp_path: Path) -> None:
    """Attempt 1 violated; attempt 2 passed cleanly. The run still has a violation."""
    run_dir = _make_run(tmp_path / "run1", "run-1", [
        _trial("t1", attempt=1, parent="g1", success=False, score=0.0,
               violations=[_finding("edited_tests")], uncertainties=[_finding("judge_unsure", severity="low")]),
        _trial("t2", attempt=2, parent="g1", success=True),
    ])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_run(run_dir)

    runs = index.list_runs()
    assert runs[0]["violation_count"] >= 1
    assert runs[0]["uncertain_trials"] >= 1
    with sqlite3.connect(tmp_path / "index.sqlite3") as connection:
        total = connection.execute(
            "SELECT SUM(violation_count), SUM(CASE WHEN uncertainties != '[]' THEN 1 ELSE 0 END) "
            "FROM trials WHERE run_id = 'run-1'"
        ).fetchone()
    assert runs[0]["violation_count"] == total[0]
    assert runs[0]["uncertain_trials"] == total[1]

    payload = index.compare(violations_only=True)
    assert [run["run_id"] for run in payload["runs"]] == ["run-1"]
    # The outcome metrics keep decided-attempt semantics: the clean attempt 2 wins.
    assert payload["runs"][0]["successful"] == 1


def _make_v1_database(path: Path, user_version: int = 1) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(_V1_SCHEMA)
        connection.execute(f"PRAGMA user_version = {user_version}")


def test_open_sets_schema_version_to_2(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite3"
    RunIndex(db)
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.parametrize("user_version", [0, 1])
def test_opening_a_legacy_database_migrates_and_indexes(tmp_path: Path, user_version: int) -> None:
    # Version 1 is what the migration writes; version 0 is what the original
    # schema shipped, since it never wrote PRAGMA user_version at all.
    db = tmp_path / "index.sqlite3"
    _make_v1_database(db, user_version)
    run_dir = _make_run(tmp_path / "run1", "run-1", [_trial("t1", violations=[_finding()])])

    index = RunIndex(db)
    index.index_run(run_dir)

    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        trial_columns = {row[1] for row in connection.execute("PRAGMA table_info(trials)")}
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    assert {"violations", "uncertainties", "violation_count"} <= trial_columns
    assert {"violation_count", "uncertain_trials"} <= run_columns
    assert index.list_runs()[0]["violation_count"] == 1


def test_migration_tolerates_a_partially_migrated_database(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite3"
    _make_v1_database(db)
    # A previous migration attempt added one column but never bumped the version.
    with sqlite3.connect(db) as connection:
        connection.execute("ALTER TABLE runs ADD COLUMN violation_count INTEGER NOT NULL DEFAULT 0")

    index = RunIndex(db)
    run_dir = _make_run(tmp_path / "run1", "run-1", [_trial("t1", violations=[_finding()])])
    index.index_run(run_dir)

    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert index.list_runs()[0]["violation_count"] == 1


def test_compare_violations_only_excludes_clean_runs(tmp_path: Path) -> None:
    _make_run(tmp_path / "clean", "run-clean", [_trial("c1")])
    _make_run(tmp_path / "dirty", "run-dirty", [_trial("d1", violations=[_finding()])])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_runs([tmp_path / "clean", tmp_path / "dirty"])

    payload = index.compare(violations_only=True)

    assert [run["run_id"] for run in payload["runs"]] == ["run-dirty"]
    assert payload["runs"][0]["violation_count"] == 1


def test_compare_violations_only_composes_with_explicit_run_ids(tmp_path: Path) -> None:
    _make_run(tmp_path / "clean", "run-clean", [_trial("c1")])
    _make_run(tmp_path / "dirty", "run-dirty", [_trial("d1", violations=[_finding()])])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_runs([tmp_path / "clean", tmp_path / "dirty"])

    payload = index.compare(["run-dirty", "run-clean"], violations_only=True)

    assert [run["run_id"] for run in payload["runs"]] == ["run-dirty"]


def test_compare_without_violations_only_keeps_every_run(tmp_path: Path) -> None:
    _make_run(tmp_path / "clean", "run-clean", [_trial("c1")])
    _make_run(tmp_path / "dirty", "run-dirty", [_trial("d1", violations=[_finding()])])
    index = RunIndex(tmp_path / "index.sqlite3")
    index.index_runs([tmp_path / "clean", tmp_path / "dirty"])

    payload = index.compare()

    assert [run["run_id"] for run in payload["runs"]] == ["run-clean", "run-dirty"]
    assert payload["runs"][0]["uncertain_trials"] == 0


def test_cli_compare_violations_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    from benchmark import cli

    _make_run(tmp_path / "clean", "run-clean", [_trial("c1")])
    _make_run(tmp_path / "dirty", "run-dirty", [_trial("d1", violations=[_finding()])])
    db = tmp_path / "index.sqlite3"
    RunIndex(db).index_runs([tmp_path / "clean", tmp_path / "dirty"])

    monkeypatch.setattr(sys, "argv", ["benchmark", "compare", "--db", str(db), "--violations"])
    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert [run["run_id"] for run in payload["runs"]] == ["run-dirty"]
