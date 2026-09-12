"""Load task definitions from ``tasks/<name>/`` directories.

A task directory is the single source of truth for one task:

``instruction.md``
    The agent-facing instruction.
``tests/test.sh``
    The verifier. Its contents are passed to ``bash -c`` so the same script
    works in a local workspace and inside a container, where the host path
    would not exist.
``task.yaml``
    Everything else: limits, seed, adapter selection, scripted actions,
    provider overrides. Optional.
``Dockerfile``
    Optional environment baseline, recorded in ``metadata.dockerfile``.

Defining ``instruction`` or ``grader.command`` in ``task.yaml`` while the
corresponding file exists is rejected: two copies of the same fact drift apart.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schemas import TaskSpec

INSTRUCTION_FILE = "instruction.md"
VERIFIER_FILE = "tests/test.sh"
SPEC_FILE = "task.yaml"
DOCKERFILE = "Dockerfile"


class TaskDirectoryError(ValueError):
    """Raised when a task directory is incomplete or self-contradictory."""


def _read_spec(directory: Path) -> dict[str, Any]:
    spec_path = directory / SPEC_FILE
    if not spec_path.exists():
        return {}
    loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise TaskDirectoryError(f"{spec_path} must contain a mapping")
    if "tasks" in loaded:
        raise TaskDirectoryError(
            f"{spec_path} must define a single task, not a 'tasks' list; "
            f"use a run config for task collections"
        )
    return loaded


def load_task_dir(directory: Path) -> TaskSpec:
    """Build a validated :class:`TaskSpec` from one task directory."""
    directory = Path(directory)
    if not directory.is_dir():
        raise TaskDirectoryError(f"task directory not found: {directory}")

    spec = _read_spec(directory)
    spec.setdefault("id", directory.name)

    instruction_path = directory / INSTRUCTION_FILE
    if instruction_path.exists():
        if spec.get("instruction"):
            raise TaskDirectoryError(
                f"{directory}: instruction is defined in both {INSTRUCTION_FILE} and {SPEC_FILE}; "
                f"keep it only in {INSTRUCTION_FILE}"
            )
        spec["instruction"] = instruction_path.read_text(encoding="utf-8").strip()
    if not spec.get("instruction"):
        raise TaskDirectoryError(f"{directory}: missing {INSTRUCTION_FILE} and no instruction in {SPEC_FILE}")

    grader = dict(spec.get("grader") or {})
    verifier_path = directory / VERIFIER_FILE
    if verifier_path.exists():
        if grader.get("command"):
            raise TaskDirectoryError(
                f"{directory}: verifier is defined in both {VERIFIER_FILE} and {SPEC_FILE}; "
                f"keep it only in {VERIFIER_FILE}"
            )
        grader["command"] = ["bash", "-c", verifier_path.read_text(encoding="utf-8")]
    if grader:
        spec["grader"] = grader
    elif spec.get("grader_type", "command") == "command":
        raise TaskDirectoryError(f"{directory}: missing {VERIFIER_FILE} and no grader.command in {SPEC_FILE}")

    dockerfile = directory / DOCKERFILE
    if dockerfile.exists():
        metadata = dict(spec.get("metadata") or {})
        metadata.setdefault("dockerfile", str(dockerfile))
        spec["metadata"] = metadata

    metadata = dict(spec.get("metadata") or {})
    metadata.setdefault("task_dir", str(directory))
    spec["metadata"] = metadata
    return TaskSpec.model_validate(spec)


def discover_tasks(root: Path) -> list[TaskSpec]:
    """Load every task directory under ``root``, ordered by directory name."""
    root = Path(root)
    if not root.is_dir():
        raise TaskDirectoryError(f"tasks root not found: {root}")
    directories = sorted(
        child for child in root.iterdir()
        if child.is_dir() and ((child / SPEC_FILE).exists() or (child / INSTRUCTION_FILE).exists())
    )
    if not directories:
        raise TaskDirectoryError(f"no task directories found under {root}")
    return [load_task_dir(directory) for directory in directories]
