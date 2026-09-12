from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .schemas import RunConfig, TaskSpec, TrialResult

logger = logging.getLogger(__name__)

# Paths that never belong in an artifact index: VCS data, dependency trees and
# tool caches only add noise (and unbounded size) to a run.
DEFAULT_IGNORE: tuple[str, ...] = (
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store", "*.pyc", "*.pyo",
)
# Cap the number of skip records kept in the index; counts stay exact.
MAX_SKIP_RECORDS = 100

# Shape version of the per-artifact ``index.json``. Independent of the run
# manifest version: the two documents change for different reasons.
ARTIFACT_INDEX_SCHEMA_VERSION = "1"

# Packages whose versions materially change a run's semantics.
_FINGERPRINT_PACKAGES = ("agent-benchmark", "pydantic", "pyyaml", "openai")


def _git_commit(root: Path) -> str | None:
    """Return HEAD for the source tree, or ``None`` outside a git checkout."""
    for start in (root, Path(__file__).resolve().parent):
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=start, capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    return None


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _FINGERPRINT_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except Exception:  # PackageNotFoundError or a broken distribution
            versions[name] = None
    return versions


def _safe_url(url: str | None) -> str | None:
    """Strip credentials embedded in a URL before it is written to a manifest."""
    if not url:
        return url
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def provider_summary(tasks: Iterable[TaskSpec]) -> list[dict[str, Any]]:
    """Deduplicated, secret-free view of every provider configuration used.

    Records the *shape* of the provider setup (model, gateway, limits, seed
    policy) so a run can be reproduced without storing API keys: only the
    environment-variable *name* is kept, and URL credentials are stripped.
    """
    unique: dict[str, dict[str, Any]] = {}
    for task in tasks:
        provider = task.provider
        entry = {
            "provider": provider.provider,
            "model": provider.model,
            "base_url": _safe_url(provider.base_url),
            "api_key_env": provider.api_key_env,
            "timeout_seconds": provider.timeout_seconds,
            "max_output_tokens": provider.max_output_tokens,
            "pass_seed": provider.pass_seed,
        }
        unique[json.dumps(entry, sort_keys=True)] = entry
    return sorted(unique.values(), key=lambda item: (str(item["provider"]), str(item["model"])))


class ArtifactStore:
    """Safe, hashed, bounded copy of files produced by one isolated workspace."""

    def __init__(
        self,
        root: Path,
        *,
        max_files: int = 2000,
        max_file_bytes: int = 16 * 1024 * 1024,
        max_total_bytes: int = 128 * 1024 * 1024,
        ignore: Sequence[str] | None = None,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if max_total_bytes < 1:
            raise ValueError("max_total_bytes must be positive")
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.ignore = tuple(ignore) if ignore is not None else DEFAULT_IGNORE
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, dict[str, Any]] = {}
        self.skipped: list[dict[str, Any]] = []
        self.skipped_counts: dict[str, int] = {}

    def _record_skip(self, path: str, reason: str, size: int) -> None:
        self.skipped_counts[reason] = self.skipped_counts.get(reason, 0) + 1
        if len(self.skipped) < MAX_SKIP_RECORDS:
            self.skipped.append({"path": path, "reason": reason, "size": size})

    def _ignored(self, relative: Path) -> bool:
        as_posix = relative.as_posix()
        for pattern in self.ignore:
            if fnmatch.fnmatch(as_posix, pattern):
                return True
            if any(fnmatch.fnmatch(part, pattern) for part in relative.parts):
                return True
        return False

    def collect(self, workspace: Path) -> list[str]:
        workspace = workspace.resolve()
        if not workspace.is_dir():
            return []
        try:
            self.root.relative_to(workspace)
        except ValueError:
            pass
        else:
            raise ValueError("artifact store must not be inside workspace")
        refs: list[str] = []
        total_bytes = 0
        for source in sorted(workspace.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(workspace)
            if ".." in relative.parts:
                continue
            key = relative.as_posix()
            size = source.stat().st_size
            if self._ignored(relative):
                self._record_skip(key, "ignored", size)
                continue
            if len(self.index) >= self.max_files:
                self._record_skip(key, "file_limit", size)
                continue
            if size > self.max_file_bytes:
                self._record_skip(key, "file_too_large", size)
                continue
            if total_bytes + size > self.max_total_bytes:
                self._record_skip(key, "total_bytes_limit", size)
                continue
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".tmp")
            digest = hashlib.sha256()
            with source.open("rb") as source_handle, temporary.open("wb") as dest_handle:
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    dest_handle.write(chunk)
            os.replace(temporary, destination)
            self.index[key] = {"path": key, "sha256": digest.hexdigest(), "size": size}
            total_bytes += size
            refs.append(key)
        if self.skipped_counts:
            logger.warning(
                "artifacts skipped",
                extra={"event": "artifacts_skipped", "skipped": self.skipped_counts},
            )
        return sorted(refs)

    def write_index(self) -> Path:
        """Persist the hash index so artifacts stay verifiable after the run."""
        path = self.root / "index.json"
        payload = {
            "schema_version": ARTIFACT_INDEX_SCHEMA_VERSION,
            "files": [self.index[key] for key in sorted(self.index)],
            "limits": {
                "max_files": self.max_files,
                "max_file_bytes": self.max_file_bytes,
                "max_total_bytes": self.max_total_bytes,
                "ignore": list(self.ignore),
            },
            "skipped_counts": self.skipped_counts,
            "skipped": self.skipped,
        }
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return path


class RunManifest:
    # v2: ``violations``/``uncertainties`` changed from ``list[str]`` to
    # ``list[Finding]`` objects, so a v1 consumer cannot read a v2 manifest.
    schema_version = "2"

    def __init__(
        self,
        run_id: str,
        tasks: list[TaskSpec],
        *,
        root: Path,
        harness_version: str = "0.1.0",
        run_config: RunConfig | None = None,
    ) -> None:
        task_list = list(tasks)
        self.data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            # Run-level fingerprints: what produced this run, beyond the tasks.
            "harness_version": harness_version,
            "git_commit": _git_commit(root),
            "python": sys.version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "dependencies": _dependency_versions(),
            "providers": provider_summary(task_list),
            "run_config": (run_config or RunConfig()).model_dump(mode="json"),
            "task_ids": [task.id for task in task_list],
            "tasks": [task.model_dump(mode="json") for task in task_list],
            "trials": [],
        }
        self.root = root

    def add_trial(self, result: TrialResult) -> None:
        self.data["trials"].append(result.model_dump(mode="json"))

    def write(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "manifest.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return path
