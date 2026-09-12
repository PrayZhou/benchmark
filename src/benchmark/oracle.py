"""Self-check a task set by running each task's reference solution.

The oracle is the harness analogue of Terminal-Bench's oracle agent: for every
task that ships an ``oracle.sh``, run it in a fresh workspace and then run the
task's own verifier against the resulting state. A task is self-consistent only
when its reference solution passes the verifier it ships with. A task without an
``oracle.sh`` is *skipped*, not failed, so a partial task set still reports
clearly and a missing reference solution never masquerades as a broken task.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .factories import build_environment, build_grader
from .schemas import TaskSpec

ORACLE_FILE = "oracle.sh"


def _oracle_path(task: TaskSpec) -> Path | None:
    """Locate ``oracle.sh`` through the task directory recorded at load time.

    The script is executed with the task workspace as cwd, so the located path
    must be absolute: a relative ``task_dir`` (as recorded when the tasks root
    itself was relative) would otherwise resolve against the wrong directory.
    """
    task_dir = task.metadata.get("task_dir")
    if not task_dir:
        return None
    candidate = (Path(str(task_dir)) / ORACLE_FILE).resolve()
    return candidate if candidate.is_file() else None


def _skipped(task: TaskSpec) -> dict[str, Any]:
    return {"task_id": task.id, "oracle": None, "verifier_passed": None, "stdout": "", "stderr": ""}


def _run_one(task: TaskSpec, root: Path) -> dict[str, Any]:
    oracle = _oracle_path(task)
    if oracle is None:
        return _skipped(task)

    # Task 1 made "docker" the TaskSpec default and LocalEnvironment opt-in. The
    # oracle is a host-side self-check that must run without Docker, so force the
    # local environment instead of going through the default (docker) path.
    local_task = task.model_copy(update={"environment_type": "local"})
    workspace = Path(tempfile.mkdtemp(prefix="oracle-", dir=str(root)))
    environment = None
    try:
        environment = build_environment(local_task, workspace)
        environment.reset(local_task)
        # The oracle runs with the workspace as cwd, exactly as the agent would,
        # so a reference solution cannot rely on the task directory itself.
        completed = environment.run_command(["bash", str(oracle)])  # type: ignore[attr-defined]
        grader = build_grader(local_task)
        result = grader.evaluate(local_task, environment, [])
        entry: dict[str, Any] = {
            "task_id": task.id,
            "oracle": str(oracle),
            "verifier_passed": result.success,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if not result.success:
            entry["verifier_evidence"] = result.evidence
        return entry
    finally:
        if environment is not None:
            environment.cleanup()
        shutil.rmtree(workspace, ignore_errors=True)


def run_oracles(tasks: list[TaskSpec], root: Path) -> list[dict[str, Any]]:
    """Run every task's oracle against its own verifier in a fresh workspace.

    ``root`` holds the per-task temporary workspaces; each is removed before the
    next task starts. Returns one report entry per task.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return [_run_one(task, root) for task in tasks]


def write_oracles(results: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "oracles.json"
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return output
