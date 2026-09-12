# Real Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the harness able to run a real evaluation: verified Docker execution as the default, a real coding-CLI agent adapter, and a small oracle-backed task set whose violations/uncertainties are queryable from the index.

**Architecture:** Docker stays behind the existing `Environment` protocol; only its binary discovery, preflight, and default status change. The CLI agent is a new `AgentAdapter` that shells out to a configured executable. The task set uses the existing `tasks/<name>/` contract plus a new `oracle.sh`; the index gains two columns and a `compare` filter.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, ruff, mypy, uv, Docker (optional at runtime).

**Spec:** `docs/superpowers/specs/2026-09-12-real-execution-docker-cli-tasks-design.md`

## Global Constraints

- Python `>=3.11`; no new runtime dependencies (Pydantic + PyYAML only).
- `make check` = `ruff check src tests` + `mypy` + `pytest`, all green. Ruff line length 120; selects `E, F, I, B, UP`.
- **All default tests must run offline**: no network, no Docker daemon, no real coding CLI. Docker integration tests gate on daemon availability with `pytest.skip`; CLI tests use a stub executable.
- **Safety:** no test may execute a destructive shell command. Tests that use `LocalEnvironment` must run only benign commands (`true`, `printf`, `sleep`). Never `rm -rf /` or anything that touches paths outside `tmp_path`.
- `policy_violation` stays outside `RetryPolicy.retryable_categories`.
- No credential, prompt, or tool output in logs.
- Docker findings from the probe: binary at `/Applications/Docker.app/Contents/Resources/bin/docker`, socket `~/.docker/run/docker.sock`, CLI **not on PATH**, **zero local images** (first run needs a network pull).
- `src/benchmark/index.py` is now in scope (schema v2). Bump `SCHEMA_VERSION` and migrate with `PRAGMA user_version`.

---

### Task 1: Docker binary discovery, preflight, and default flip

**Files:**
- Modify: `src/benchmark/adapters/environments/docker.py`
- Modify: `src/benchmark/schemas.py` (`TaskSpec.environment_type` default)
- Modify: `src/benchmark/factories.py` (pass through `docker_binary`)
- Modify: `configs/example.yaml`, `tasks/hello_task/task.yaml` (explicit `environment_type: local`)
- Modify: `tests/test_graders.py` (its `build_environment` call must opt into local)
- Test: `tests/test_docker_environment.py` (create)

**Interfaces:**
- Produces: `resolve_docker_binary(configured: str | None = None) -> str | None`; `DockerEnvironment(root, image, network="none", *, docker_binary=None, ...)`; `DockerEnvironment.preflight() -> None`; `TaskSpec.environment_type` default `"docker"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_docker_environment.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.adapters.environments.docker import DockerEnvironment, resolve_docker_binary
from benchmark.schemas import TaskSpec


def test_resolve_docker_binary_prefers_explicit_and_env(monkeypatch) -> None:
    assert resolve_docker_binary("/usr/bin/true") == "/usr/bin/true"
    monkeypatch.setenv("BENCHMARK_DOCKER_BIN", "/usr/bin/true")
    assert resolve_docker_binary() == "/usr/bin/true"
    monkeypatch.delenv("BENCHMARK_DOCKER_BIN")
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("os.path.exists", lambda path: False)
    assert resolve_docker_binary() is None


def test_reset_argv_pins_security_flags(tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(args, *, timeout, check=True):
        recorded.append(list(args))
        return type("R", (), {"returncode": 0, "stdout": "x", "stderr": ""})()

    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary="/usr/bin/true")
    environment._docker = fake_run  # type: ignore[method-assign]
    environment.reset(TaskSpec(id="t", instruction="x"))
    run = next(args for args in recorded if args[0] == "run")
    for flag in ("--network", "none", "--user", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"):
        assert flag in run
    assert f"{tmp_path.resolve()}:/workspace:rw" in run


def test_verify_command_mounts_read_only(tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(args, *, timeout, check=True):
        recorded.append(list(args))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary="/usr/bin/true")
    environment._docker = fake_run  # type: ignore[method-assign]
    environment.reset(TaskSpec(id="t", instruction="x"))
    environment.verify_command(["true"])
    verify = next(args for args in recorded if args[0] == "run" and "--rm" in args)
    assert f"{tmp_path.resolve()}:/workspace:ro" in verify
    assert "--network" in verify and verify[verify.index("--network") + 1] == "none"


def test_reset_reports_a_missing_daemon_actionably(tmp_path: Path) -> None:
    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary=None)
    with pytest.raises(RuntimeError, match="Docker"):
        environment.reset(TaskSpec(id="t", instruction="x"))
```

Append to `tests/test_graders.py` (or update the existing test) so the local route is explicit:

```python
def test_environment_defaults_to_docker(tmp_path: Path) -> None:
    from benchmark.factories import build_environment
    from benchmark.adapters.environments.docker import DockerEnvironment

    task = TaskSpec(id="t", instruction="x", environment={"image": "alpine:3"})
    assert isinstance(build_environment(task, tmp_path), DockerEnvironment)

    local = TaskSpec(id="t", instruction="x", environment_type="local")
    from benchmark.adapters.environments.local import LocalEnvironment
    assert isinstance(build_environment(local, tmp_path), LocalEnvironment)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_docker_environment.py -v`
Expected: FAIL — `resolve_docker_binary` does not exist.

- [ ] **Step 3: Implement**

In `docker.py`:

```python
def resolve_docker_binary(configured: str | None = None) -> str | None:
    """Find a usable docker CLI without assuming it is on PATH."""
    if configured:
        return configured
    env = os.getenv("BENCHMARK_DOCKER_BIN")
    if env:
        return env
    found = shutil.which("docker")
    if found:
        return found
    for candidate in (
        "/Applications/Docker.app/Contents/Resources/bin/docker",
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
    ):
        if os.path.exists(candidate):
            return candidate
    return None
```

- `__init__` takes `docker_binary: str | None = None` and stores
  `self.docker_binary = resolve_docker_binary(docker_binary)`.
- `_docker(...)` becomes an instance method using `self.docker_binary`; when it
  is `None`, raise `RuntimeError("Docker CLI not found; set BENCHMARK_DOCKER_BIN or pass docker_binary")`.
- `reset()` calls `self.preflight()` first; `preflight()` runs
  `self._docker(["version", "--format", "{{.Server.Version}}"], timeout=10)` and
  wraps failures in a `RuntimeError` naming the binary and the missing daemon.

In `schemas.py`: `environment_type: str = "docker"`.

In `factories.py`: pass `docker_binary=task.environment.get("docker_binary")`
into `DockerEnvironment`.

In `configs/example.yaml` and `tasks/hello_task/task.yaml`: add
`environment_type: local`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_docker_environment.py tests/test_graders.py tests/test_harness.py -v`
Expected: PASS. Then `make check`.

- [ ] **Step 5: Checkpoint**

Run: `make check`
Expected: green.

---

### Task 2: Gated Docker integration test

**Files:**
- Modify: `tests/test_docker_environment.py`

**Interfaces:**
- Consumes: `resolve_docker_binary`, `DockerEnvironment` from Task 1.
- Produces: an integration test that skips without a daemon and proves the real container path.

- [ ] **Step 1: Write the test**

```python
import os
import shutil as _shutil
import subprocess as _subprocess


def _daemon_available() -> bool:
    binary = resolve_docker_binary()
    if not binary:
        return False
    try:
        done = _subprocess.run([binary, "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=10, check=False)
    except (OSError, _subprocess.SubprocessError):
        return False
    return done.returncode == 0


integration = pytest.mark.skipif(not _daemon_available(), reason="docker daemon not available")


@integration
def test_container_runs_commands_and_verifier_is_read_only(tmp_path: Path) -> None:
    image = os.getenv("BENCHMARK_TEST_IMAGE", "python:3.12-slim")
    if _subprocess.run([resolve_docker_binary(), "image", "inspect", image],
                       capture_output=True, timeout=30, check=False).returncode != 0:
        if _subprocess.run([resolve_docker_binary(), "pull", image],
                           capture_output=True, timeout=300, check=False).returncode != 0:
            pytest.skip("base image unavailable offline")
    environment = DockerEnvironment(tmp_path, image=image, command_timeout=30)
    environment.reset(TaskSpec(id="t", instruction="x", seed=7))
    try:
        ok = environment.execute({"type": "command", "command": ["sh", "-c", "printf READY"]})
        assert ok["returncode"] == 0 and ok["stdout"] == "READY"

        bad = environment.execute({"type": "command", "command": ["sh", "-c", "exit 3"]})
        assert bad["returncode"] == 3 and bad["ok"] is False

        (tmp_path / "marker.txt").write_text("READY")
        verified = environment.verify_command(["sh", "-c", "cat marker.txt"])
        assert verified.stdout.strip() == "READY"

        denied = environment.verify_command(["sh", "-c", "printf x >> marker.txt"])
        assert denied.returncode != 0, "verifier must not be able to write the workspace"
    finally:
        environment.cleanup()
```

- [ ] **Step 2: Run with Docker available**

Run: `uv run pytest tests/test_docker_environment.py -v`
Expected: PASS (integration test pulls once). Without a daemon: SKIPPED.

Note for the implementer: this is the one test that needs network for the first
pull. If `docker pull` fails, the test must `skip`, never `fail`.

- [ ] **Step 3: Checkpoint**

Run: `make check`

---

### Task 3: `CLIAgent` adapter and factory route

**Files:**
- Create: `src/benchmark/adapters/agents/cli.py`
- Modify: `src/benchmark/schemas.py` (add `cli: dict[str, Any]` to `TaskSpec`)
- Modify: `src/benchmark/factories.py` (`agent == "cli"`)
- Test: `tests/test_cli_agent.py` (create)

**Interfaces:**
- Produces: `CLIAgent(instruction, *, command, args=None, prompt_mode="prompt_file", prompt_flag=None, timeout=600, max_steps=1, cwd=None, env=None, success_exit_codes=(0,), max_output_chars=4000)`; factory reads `task.cli`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_agent.py` with a stub executable written to `tmp_path`:

```python
STUB = """#!/usr/bin/env python3
import json, sys, pathlib
payload = {"argv": sys.argv[1:], "stdin": sys.stdin.read()}
print(json.dumps(payload))
sys.exit(int(pathlib.Path(__file__).with_suffix(".code").read_text().strip()))
"""
```

Tests must assert: `prompt_file` mode writes the instruction and substitutes
`{prompt_file}`; `stdin` mode feeds it on stdin; `argv` mode substitutes
`{instruction}`; exit 0 → `status == "completed"`; exit 4 → `status == "error"`;
a stub that sleeps past `timeout` yields `status == "error"`; `final_message`
carries the bounded tail of combined output.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_agent.py -v`
Expected: FAIL — `benchmark.adapters.agents.cli` missing.

- [ ] **Step 3: Implement**

Implement `CLIAgent` per the spec's Workstream 2 contract: resolve `command` with
`shutil.which` (absolute paths pass through), build argv with template
substitution per `prompt_mode`, run `subprocess.run(..., timeout=..., cwd=cwd,
env=merged, capture_output=True, text=True)`, map exit codes, and return an
`AgentResult`. `act()` on the first call launches the process and stores the
result; subsequent calls return `{"type": "finish"}`. `max_steps > 1` re-launches
with the previous output appended to the instruction.

Add `cli: dict[str, Any] = Field(default_factory=dict)` to `TaskSpec` and route
`agent == "cli"` to `CLIAgent(task.instruction, **task.cli)` in `build_agent`.
A missing `command` raises `ValueError` at build time.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_agent.py -v`
Expected: PASS. Then `make check`.

- [ ] **Step 5: Checkpoint**

Run: `make check`

---

### Task 4: CLI profiles and documentation

**Files:**
- Create: `src/benchmark/adapters/agents/profiles.py`
- Modify: `docs/adapters.md`
- Test: `tests/test_cli_agent.py`

**Interfaces:**
- Consumes: `CLIAgent` from Task 3.
- Produces: `PROFILES: dict[str, dict[str, Any]]` with `claude` and `codex` entries; a test that each profile's keys are accepted by `CLIAgent`.

- [ ] **Step 1: Write the failing test**

```python
def test_profiles_are_valid_cli_configs() -> None:
    from benchmark.adapters.agents.profiles import PROFILES
    from benchmark.adapters.agents.cli import CLIAgent

    assert {"claude", "codex"} <= set(PROFILES)
    for profile in PROFILES.values():
        CLIAgent("do the thing", **profile)  # must not raise
```

- [ ] **Step 2: Implement**

`profiles.py` holds two plain dicts (command, args, prompt_mode, prompt_flag)
documented as unverified defaults. Add a short `docs/adapters.md` section for
`agent: cli` with a YAML example and the note that the profiles have not been
run against the real CLIs in this environment.

- [ ] **Step 3: Run tests, checkpoint**

Run: `uv run pytest tests/test_cli_agent.py -v && make check`

---

### Task 5: Index schema v2, migration, and `compare --violations`

**Files:**
- Modify: `src/benchmark/index.py`
- Modify: `src/benchmark/cli.py` (`compare --violations`)
- Test: `tests/test_index.py`

**Interfaces:**
- Produces: `SCHEMA_VERSION = "2"`; `trials.violations`, `trials.uncertainties`, `trials.violation_count`; `runs.violation_count`, `runs.uncertain_trials`; `RunIndex.compare(..., violations_only: bool = False)`.

- [ ] **Step 1: Write the failing tests**

Assert: a run whose trial carries findings indexes `violation_count >= 1`; a v1
database (create one by executing the old `_SCHEMA` by hand) opens, gains the new
columns, and indexes without error; `compare(violations_only=True)` excludes runs
with zero violations; `PRAGMA user_version` reads 2.

- [ ] **Step 2: Implement**

- Add the columns to `_SCHEMA`; call `_migrate(connection)` after
  `executescript`, which reads `PRAGMA user_version` and issues
  `ALTER TABLE ... ADD COLUMN ...` for any missing column, then
  `PRAGMA user_version = 2`.
- Store `json.dumps([f.model_dump() for f in result.violations])` and the
  uncertainty list; `violation_count = len(result.violations)`.
- `compare` adds `violation_count` / `uncertain_trials` to each run view and, when
  `violations_only`, filters to `violation_count > 0`.
- `cli.py`: add `compare --violations` mapped to `violations_only=True`.

- [ ] **Step 3: Run tests, checkpoint**

Run: `uv run pytest tests/test_index.py -v && make check`

---

### Task 6: `benchmark oracle` task-set self-check

**Files:**
- Create: `src/benchmark/oracle.py`
- Modify: `src/benchmark/cli.py` (`oracle` subcommand)
- Test: `tests/test_oracle.py` (create)

**Interfaces:**
- Produces: `run_oracles(tasks: list[TaskSpec], root: Path) -> list[dict]` where each entry has `task_id`, `oracle` (path), `verifier_passed` (bool), `stdout`, `stderr`; `write_oracles(...)`; CLI `benchmark oracle --tasks-root tasks [--output DIR]`.

- [ ] **Step 1: Write the failing test**

Build a fixture task dir with an `oracle.sh` that creates the artifact and a
`tests/test.sh` that checks it; assert `verifier_passed is True`. Then a task
whose oracle does not satisfy the verifier; assert `verifier_passed is False`.
Also assert a task with no `oracle.sh` is reported as `oracle=None` and skipped,
not crashed.

- [ ] **Step 2: Implement**

`oracle.py` mirrors `regrade`/`replay`: for each task with `metadata["task_dir"]`,
create a temp workspace, `build_environment(task, workspace)` — forcing
`LocalEnvironment` so the oracle never needs Docker — run `bash <task_dir>/oracle.sh`
with the workspace as cwd, then run the task's grader via `build_grader` and
record `success`. Clean up the workspace. `oracle.sh` is located through
`metadata["task_dir"]`, and its absence is a skip, not an error.

- [ ] **Step 3: Run tests, checkpoint**

Run: `uv run pytest tests/test_oracle.py -v && make check`

---

### Task 7: Task set with oracle solutions

**Files:**
- Create: `tasks/fix_fizzbuzz/{instruction.md,tests/test.sh,task.yaml,oracle.sh,src/fizzbuzz.py}`
- Create: `tasks/parse_log_counts/{instruction.md,tests/test.sh,task.yaml,oracle.sh,input.log}`
- Create: `tasks/makefile_target/{instruction.md,tests/test.sh,task.yaml,oracle.sh,Makefile}`
- Create: `tasks/seed_reproducible/{instruction.md,tests/test.sh,task.yaml,oracle.sh}`
- Create: `configs/tasks.yaml`

**Interfaces:**
- Consumes: the `tasks/<name>/` contract and `benchmark oracle` from Task 6.

- [ ] **Step 1: Author the four tasks**

Each task: a natural-language `instruction.md`, a `tests/test.sh` verifier that
only inspects end state, an `oracle.sh` reference solution, and a `task.yaml`
with `environment_type: local`, a `seed`, and tight `limits`. Keep verifiers
deterministic and offline (no network, no package installs). `fix_fizzbuzz` is
the strongest signal: the bug is subtle enough that only a real agent iteration
solves it, and the verifier runs assertions, not a transcript check.

- [ ] **Step 2: Verify the task set is self-consistent**

Run: `uv run python -m benchmark oracle --tasks-root tasks`
Expected: every task reports `verifier_passed: true`; `hello_task` is included
and passes. This is the gate that says the verifier accepts the reference
solution — an unsolvable task fails here, loudly.

- [ ] **Step 3: Verify discovery**

Run: `uv run pytest tests/test_tasks.py -v && make check`

---

### Task 8: Documentation and known limitations

**Files:**
- Modify: `README.md`, `docs/adapters.md`

- [ ] **Step 1: Update the docs**

- README: Docker is the default environment and `local` is an explicit opt-in
  with a safety warning; `agent: cli`; the task set and `benchmark oracle`;
  `compare --violations`.
- `docs/adapters.md`: the `agent: cli` contract and profiles.
- A "Known limitations" section recording exactly: Docker integration tests skip
  without a daemon; the first run needs an image pull; the `claude`/`codex`
  profiles are unverified against the real CLIs; `LocalEnvironment` is still not
  a sandbox.

- [ ] **Step 2: Final checkpoint**

Run: `make check` and `uv run python -m benchmark oracle --tasks-root tasks`
Expected: both green.

---

## Self-Review

- **Spec coverage:** default flip + binary/preflight → Task 1; real container
  verification → Task 2; CLI adapter → Task 3; profiles → Task 4; index v2 +
  compare → Task 5; oracle → Task 6; task set → Task 7; docs/limitations → Task 8.
- **Known unverifiable items** are stated in the spec's Known Limitations and
  repeated in Task 8's docs, not hidden.
- **Safety:** every test that touches `LocalEnvironment` uses benign commands;
  the Docker verifier read-only assertion is a *deny* check, not a destructive one.
