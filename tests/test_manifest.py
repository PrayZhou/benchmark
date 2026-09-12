import hashlib
import json
from pathlib import Path

import pytest

from benchmark.manifest import ArtifactStore, RunManifest
from benchmark.schemas import RunConfig, TaskSpec, TrialResult


def test_artifact_store_collects_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "nested").mkdir()
    (workspace / "nested" / "result.txt").write_text("ok")
    refs = ArtifactStore(tmp_path / "artifacts").collect(workspace)
    assert refs == ["nested/result.txt"]
    assert (tmp_path / "artifacts" / "nested" / "result.txt").read_text() == "ok"


def test_artifact_index_persists_verifiable_hashes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("ok")
    store = ArtifactStore(tmp_path / "artifacts")
    store.collect(workspace)
    index = json.loads(store.write_index().read_text())
    assert index["files"] == [{
        "path": "result.txt",
        "sha256": hashlib.sha256(b"ok").hexdigest(),
        "size": 2,
    }]


def test_manifest_persists_trial(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="do it")
    result = TrialResult(task_id="t", trial_id="tr", agent_id="a", seed=1, status="completed")
    manifest = RunManifest("run", [task], root=tmp_path)
    manifest.add_trial(result)
    path = manifest.write()
    assert path.exists()
    assert '"trial_id": "tr"' in path.read_text()


def test_manifest_declares_a_schema_version(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="do it")
    payload = json.loads(RunManifest("run", [task], root=tmp_path).write().read_text())
    # v2: violations/uncertainties are list[Finding], not list[str].
    assert payload["schema_version"] == "2"


def test_manifest_header_carries_run_fingerprints(tmp_path: Path) -> None:
    task = TaskSpec(
        id="t", instruction="x", agent="llm",
        provider={"provider": "openai", "model": "gpt-x", "api_key_env": "MY_KEY",
                  "base_url": "https://user:secret@gateway.example/v1", "pass_seed": False},
    )
    manifest = RunManifest("run-1", [task], root=tmp_path, harness_version="9.9.9",
                           run_config=RunConfig(runs_per_task=2))
    payload = json.loads(manifest.write().read_text())

    assert payload["harness_version"] == "9.9.9"
    assert payload["run_config"]["runs_per_task"] == 2
    assert payload["python_version"]
    assert "dependencies" in payload
    assert "git_commit" in payload  # None outside a checkout, still present

    providers = payload["providers"]
    assert providers[0]["model"] == "gpt-x"
    # URL credentials are stripped; only the env-var *name* is recorded.
    assert providers[0]["base_url"] == "https://gateway.example/v1"
    assert providers[0]["api_key_env"] == "MY_KEY"
    assert "secret" not in json.dumps(providers)


def test_manifest_deduplicates_providers(tmp_path: Path) -> None:
    tasks = [TaskSpec(id=f"t{i}", instruction="x", provider={"provider": "openai", "model": "same"})
             for i in range(3)]
    payload = json.loads(RunManifest("run", tasks, root=tmp_path).write().read_text())
    assert len(payload["providers"]) == 1


def test_artifact_store_skips_ignored_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "__pycache__").mkdir(parents=True)
    (workspace / ".git").mkdir()
    (workspace / "keep.txt").write_text("ok")
    (workspace / "__pycache__" / "x.pyc").write_text("junk")
    (workspace / ".git" / "config").write_text("junk")

    store = ArtifactStore(tmp_path / "artifacts")
    assert store.collect(workspace) == ["keep.txt"]
    payload = json.loads(store.write_index().read_text())
    assert payload["skipped_counts"]["ignored"] == 2


def test_artifact_store_enforces_per_file_size_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "big.bin").write_text("x" * 100)
    (workspace / "small.txt").write_text("ok")

    store = ArtifactStore(tmp_path / "artifacts", max_file_bytes=10)
    assert store.collect(workspace) == ["small.txt"]
    payload = json.loads(store.write_index().read_text())
    assert payload["skipped_counts"] == {"file_too_large": 1}
    assert payload["limits"]["max_file_bytes"] == 10


def test_artifact_store_enforces_file_count_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (workspace / name).write_text("ok")

    store = ArtifactStore(tmp_path / "artifacts", max_files=1)
    assert len(store.collect(workspace)) == 1
    assert store.skipped_counts["file_limit"] == 2


def test_artifact_store_rejects_non_positive_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "artifacts", max_files=0)
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "artifacts", max_file_bytes=0)
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "artifacts", max_total_bytes=0)

