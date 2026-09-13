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
``workspace/``
    Optional fixture tree copied into the environment root at ``reset()``, so a
    task can ship starting state (a buggy module, a ``Makefile``, an input log).
``Dockerfile``
    Optional environment baseline, recorded in ``metadata.dockerfile``.

Defining ``instruction`` or ``grader.command`` in ``task.yaml`` while the
corresponding file exists is rejected: two copies of the same fact drift apart.
``grader_type: composite`` is the structured exception: the behavioral
sub-graders stay in ``task.yaml`` and the ``tests/test.sh`` verifier is injected
as the command sub-grader, so a policy gate and the correctness check run
together without duplicating the verifier.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from .schemas import TaskSpec

INSTRUCTION_FILE = "instruction.md"
VERIFIER_FILE = "tests/test.sh"
SPEC_FILE = "task.yaml"
DOCKERFILE = "Dockerfile"
WORKSPACE_DIR = "workspace"


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

    grader_type = spec.get("grader_type", "command")
    grader = dict(spec.get("grader") or {})
    verifier_path = directory / VERIFIER_FILE
    if verifier_path.exists():
        verifier_command = ["bash", "-c", verifier_path.read_text(encoding="utf-8")]
        if grader_type == "composite":
            # A composite grader keeps its behavioral sub-graders in task.yaml
            # and gets the correctness verifier injected here, so tests/test.sh
            # stays the single source of truth for the verifier.
            configured = grader.get("graders")
            if configured is not None and not isinstance(configured, list):
                raise TaskDirectoryError(f"{directory}: grader.graders must be a list")
            graders = list(configured or [])
            if any(isinstance(item, dict) and item.get("type") == "command" for item in graders):
                raise TaskDirectoryError(
                    f"{directory}: verifier is defined in both {VERIFIER_FILE} and a composite "
                    f"command sub-grader in {SPEC_FILE}; keep it only in {VERIFIER_FILE}"
                )
            graders.append({"type": "command", "command": verifier_command})
            grader["graders"] = graders
        else:
            if grader.get("command"):
                raise TaskDirectoryError(
                    f"{directory}: verifier is defined in both {VERIFIER_FILE} and {SPEC_FILE}; "
                    f"keep it only in {VERIFIER_FILE}"
                )
            grader["command"] = verifier_command
    if grader_type == "composite" and not grader.get("graders"):
        raise TaskDirectoryError(f"{directory}: composite grader needs at least one sub-grader")
    if grader:
        spec["grader"] = grader
    elif grader_type == "command":
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


def stage_workspace(task: TaskSpec, root: Path) -> None:
    """Copy the task's optional ``workspace/`` fixture tree into ``root``.

    A task can ship starting state by placing files in ``<task_dir>/workspace/``.
    Callers invoke this at ``reset()`` so the local and Docker adapters present
    the same starting workspace. A missing ``task_dir`` metadata entry or a
    missing ``workspace/`` directory is a no-op. Symlinks are skipped rather
    than followed or recreated, so a fixture cannot reach outside its own tree.
    """
    root = Path(root)
    task_dir = task.metadata.get("task_dir")
    if not task_dir:
        return
    source = Path(str(task_dir)) / WORKSPACE_DIR
    if not source.is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    for current, dirnames, filenames in os.walk(source):
        # Prune symlinked directories before ``os.walk`` would descend into them;
        # a symlink is skipped entirely instead of copied or followed.
        dirnames[:] = sorted(name for name in dirnames if not (Path(current) / name).is_symlink())
        destination_dir = root / Path(current).relative_to(source)
        destination_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(filenames):
            entry = Path(current) / name
            if entry.is_symlink():
                continue
            shutil.copy2(entry, destination_dir / name)
