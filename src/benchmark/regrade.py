"""Re-score a trial against its archived artifact snapshot.

Regrade does **not** re-run the agent and does not replay its actions. It
rebuilds the workspace from the artifact snapshot captured during the original
trial and runs the supplied grader against it, so a verifier can be corrected
without re-running the model.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .factories import build_environment
from .replay import load_events
from .schemas import TaskSpec


def _artifact_root(run_dir: Path, original: dict[str, Any], task: TaskSpec) -> Path:
    return (
        run_dir / "artifacts" / task.id
        / str(original.get("run_index", 0)) / str(original.get("attempt", 1))
    )


def regrade_trial(run_dir: Path, trial_id: str, grader: Any) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    original = next(item for item in manifest.get("trials", []) if item["trial_id"] == trial_id)
    task_data = next(item for item in manifest.get("tasks", []) if item["id"] == original["task_id"])
    task = TaskSpec.model_validate(task_data)
    events = load_events(run_dir / "traces" / f"{trial_id}.jsonl")
    artifact_root = _artifact_root(run_dir, original, task)
    # Scoring an empty workspace would silently produce a misleading result;
    # require the snapshot the trial actually persisted.
    if not artifact_root.exists():
        raise FileNotFoundError(
            f"no artifact snapshot for trial {trial_id}: {artifact_root} "
            f"(regrade re-scores the archived workspace)"
        )
    artifact_index = artifact_root / "index.json"
    workspace = Path(tempfile.mkdtemp(prefix="regrade-"))
    environment = None
    try:
        for source in artifact_root.rglob("*"):
            if source.is_file() and not source.is_symlink():
                relative = source.relative_to(artifact_root)
                destination = workspace / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        environment = build_environment(task, workspace)
        environment.reset(task)
        result = grader.evaluate(task, environment, events)
        return {
            "trial_id": trial_id,
            "mode": "artifact_snapshot",
            "artifacts_found": True,
            "artifact_index": str(artifact_index) if artifact_index.exists() else None,
            "grader_isolated": bool(getattr(grader, "isolated", False)),
            "original": original,
            "regrade": result.model_dump(mode="json"),
        }
    finally:
        if environment is not None:
            environment.cleanup()
        shutil.rmtree(workspace, ignore_errors=True)


def write_regrade(run_dir: Path, trial_id: str, grader: Any) -> Path:
    output = run_dir / "regrades" / f"{trial_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(regrade_trial(run_dir, trial_id, grader), indent=2), encoding="utf-8")
    return output
