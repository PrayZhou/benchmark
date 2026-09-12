"""Cross-run SQLite index.

A single run directory is self-describing, but comparing runs means reading
each ``manifest.json`` by hand. This module ingests run directories into one
SQLite database (``runs`` + ``trials`` tables) so runs can be listed and
compared without loading every report into memory.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .reporting import summarize
from .schemas import TrialResult

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
DEFAULT_INDEX_PATH = Path("reports") / "index.sqlite3"

_SCHEMA = """
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
    -- Trial ids derive deterministically from (task, seed, attempt), so the
    -- same id legitimately appears in more than one run. Identity is the
    -- (run_id, trial_id) pair, not the trial id alone.
    PRIMARY KEY (run_id, trial_id)
);
CREATE INDEX IF NOT EXISTS idx_trials_run_id ON trials(run_id);
CREATE INDEX IF NOT EXISTS idx_trials_task_id ON trials(task_id);
"""

_RUN_COLUMNS = (
    "run_id", "indexed_at", "created_at", "harness_version", "git_commit", "python_version",
    "platform", "dataset", "task_count", "trial_count", "total", "successful", "success_rate",
    "average_score", "average_duration_seconds", "total_attempts", "retried_groups",
    "failures_by_category", "config", "manifest_path", "report_path",
)

_TRIAL_COLUMNS = (
    "trial_id", "run_id", "task_id", "agent_id", "seed", "run_index", "attempt",
    "parent_trial_id", "status", "success", "score", "steps", "tool_calls", "duration_seconds",
    "input_tokens", "output_tokens", "failure_category", "provider", "model",
    "termination_reason", "trace_path",
)

_RUN_FIELDS = tuple(
    "trial_id run_id task_id agent_id seed run_index attempt parent_trial_id status success score "
    "steps tool_calls duration_seconds input_tokens output_tokens failure_category provider model "
    "termination_reason trace_path".split()
)


class RunIndex:
    """SQLite-backed index over run directories."""

    def __init__(self, path: Path = DEFAULT_INDEX_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def index_run(self, run_dir: Path) -> dict[str, Any]:
        """Ingest (or re-ingest) one run directory. Idempotent per ``run_id``."""
        run_dir = Path(run_dir)
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"manifest not found: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        trials = [TrialResult.model_validate(item) for item in manifest.get("trials", [])]
        summary = summarize(trials)
        run_id = manifest.get("run_id") or run_dir.name
        report_path = run_dir / "report.json"
        run_config = manifest.get("run_config") or {}
        run_row = {
            "run_id": run_id,
            "indexed_at": datetime.now(UTC).isoformat(),
            "created_at": manifest.get("created_at"),
            "harness_version": manifest.get("harness_version"),
            "git_commit": manifest.get("git_commit"),
            "python_version": manifest.get("python_version"),
            "platform": manifest.get("platform"),
            "dataset": run_config.get("dataset"),
            "task_count": len(manifest.get("task_ids", []) or []),
            "trial_count": len(trials),
            "total": summary.total,
            "successful": summary.successful,
            "success_rate": summary.success_rate,
            "average_score": summary.average_score,
            "average_duration_seconds": summary.average_duration_seconds,
            "total_attempts": summary.total_attempts,
            "retried_groups": summary.retried_groups,
            "failures_by_category": json.dumps(summary.failures_by_category),
            "config": json.dumps(run_config),
            "manifest_path": str(manifest_path),
            "report_path": str(report_path) if report_path.exists() else None,
        }
        trial_rows = [self._trial_row(result, run_id) for result in trials]
        with closing(self._connect()) as connection, connection:
            connection.execute(self._upsert_run_sql(), self._run_values(run_row))
            connection.execute("DELETE FROM trials WHERE run_id = ?", (run_id,))
            connection.executemany(self._insert_trials_sql(), trial_rows)
        logger.info(
            "indexed run %s", run_id,
            extra={"event": "run_indexed", "run_id": run_id, "trials": len(trials),
                   "index_path": str(self.path)},
        )
        return {
            "run_id": run_id,
            "path": str(run_dir),
            "trials": len(trials),
            "total": summary.total,
            "success_rate": summary.success_rate,
        }

    def index_runs(self, run_dirs: Iterable[Path]) -> list[dict[str, Any]]:
        return [self.index_run(run_dir) for run_dir in run_dirs]

    def list_runs(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY COALESCE(created_at, run_id), run_id"
            ).fetchall()
        return [self._run_view(row) for row in rows]

    def compare(self, run_ids: Sequence[str] | None = None, *, include_tasks: bool = True,
                dataset: str | None = None) -> dict[str, Any]:
        """Summarise runs side by side, with an optional per-task matrix.

        ``dataset`` filters to runs carrying that label when no explicit
        ``run_ids`` are given.
        """
        with closing(self._connect()) as connection:
            runs = self._select_runs(connection, run_ids, dataset=dataset)
            selected = [run["run_id"] for run in runs]
            payload: dict[str, Any] = {"runs": [self._run_view(run) for run in runs]}
            if include_tasks and selected:
                payload["tasks"] = self._task_breakdown(connection, selected)
            return payload

    def _select_runs(self, connection: sqlite3.Connection, run_ids: Sequence[str] | None,
                     *, dataset: str | None = None) -> list[sqlite3.Row]:
        order = "ORDER BY COALESCE(created_at, run_id), run_id"
        if not run_ids:
            if dataset is None:
                return connection.execute(f"SELECT * FROM runs {order}").fetchall()
            return connection.execute(
                f"SELECT * FROM runs WHERE dataset = ? {order}", (dataset,)
            ).fetchall()
        placeholders = ",".join("?" * len(run_ids))
        rows = connection.execute(
            f"SELECT * FROM runs WHERE run_id IN ({placeholders})", tuple(run_ids)
        ).fetchall()
        found = {row["run_id"] for row in rows}
        missing = [run_id for run_id in run_ids if run_id not in found]
        if missing:
            raise KeyError(f"unknown run_id(s): {', '.join(missing)}")
        # Preserve caller order.
        by_id = {row["run_id"]: row for row in rows}
        return [by_id[run_id] for run_id in run_ids]

    def _task_breakdown(self, connection: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, Any]:
        placeholders = ",".join("?" * len(run_ids))
        rows = connection.execute(
            f"""
            SELECT task_id, run_id,
                   COUNT(*) AS attempts,
                   SUM(success) AS successful,
                   AVG(score) AS average_score,
                   AVG(duration_seconds) AS average_duration_seconds
            FROM trials
            WHERE run_id IN ({placeholders})
            GROUP BY task_id, run_id
            ORDER BY task_id, run_id
            """,
            tuple(run_ids),
        ).fetchall()
        tasks: dict[str, dict[str, Any]] = {}
        for row in rows:
            attempts = row["attempts"] or 0
            successful = row["successful"] or 0
            tasks.setdefault(row["task_id"], {})[row["run_id"]] = {
                "attempts": attempts,
                "successful": successful,
                "success_rate": successful / attempts if attempts else 0.0,
                "average_score": row["average_score"] or 0.0,
                "average_duration_seconds": row["average_duration_seconds"] or 0.0,
            }
        return tasks

    @staticmethod
    def _run_view(row: sqlite3.Row) -> dict[str, Any]:
        view = dict(row)
        view["failures_by_category"] = json.loads(view.get("failures_by_category") or "{}")
        view["config"] = json.loads(view.get("config") or "{}")
        return view

    @staticmethod
    def _trial_row(result: TrialResult, run_id: str) -> tuple[Any, ...]:
        data = result.model_dump(mode="json")
        data["run_id"] = run_id
        data["success"] = int(result.success)
        return tuple(data.get(field) for field in _RUN_FIELDS)

    @staticmethod
    def _run_values(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[column] for column in _RUN_COLUMNS)

    @staticmethod
    def _upsert_run_sql() -> str:
        columns = ", ".join(_RUN_COLUMNS)
        placeholders = ", ".join("?" for _ in _RUN_COLUMNS)
        updates = ", ".join(f"{column}=excluded.{column}" for column in _RUN_COLUMNS if column != "run_id")
        return f"INSERT INTO runs ({columns}) VALUES ({placeholders}) ON CONFLICT(run_id) DO UPDATE SET {updates}"

    @staticmethod
    def _insert_trials_sql() -> str:
        columns = ", ".join(_TRIAL_COLUMNS)
        placeholders = ", ".join("?" for _ in _TRIAL_COLUMNS)
        return f"INSERT INTO trials ({columns}) VALUES ({placeholders})"
