# Real execution: Docker default, CLI agent adapter, task set + index

Date: 2026-09-12
Status: approved (scope), pending implementation

## Background

The harness is a complete骨架 but cannot run a real evaluation yet. Three
gaps, in priority order:

1. **`DockerEnvironment` has no tests and is not the default.** `LocalEnvironment`
   is the default and is explicitly *not* a sandbox — this session produced a
   test that ran `rm -rf /` on the host, which only a non-executing stub
   prevented. The only real isolation path is unverified and unreachable by
   default.
2. **No real agent adapter.** Only `scripted` and the in-repo `LLMAdapter` tool
   loop exist, so any task set would measure the harness's own loop rather than
   a real coding agent.
3. **No task set and no violation/uncertainty indexing.** One `hello_task`, no
   oracle solutions, and `index.py` never learned about `violations` /
   `uncertainties`, so `benchmark compare` cannot surface them.

**Verified environment facts (2026-09-12):** Docker Desktop is installed at
`/Applications/Docker.app` with the binary at
`/Applications/Docker.app/Contents/Resources/bin/docker`; the daemon is running
(client/server 29.7.2, `linux/aarch64`); the socket is
`~/.docker/run/docker.sock`; the CLI is **not on `PATH`**; **zero local
images**, so the first container run requires a network pull. No coding CLI
(`claude`, `codex`, `aider`, `openhands`, `swe-agent`) is installed.

## Goals

- Make `DockerEnvironment` verified and the default for real tasks, with
  `LocalEnvironment` demoted to an explicit opt-in.
- Add a real, provider-agnostic coding-CLI agent adapter that is testable
  offline with a stub executable.
- Ship a small task set with oracle solutions, and make violations /
  uncertainties first-class in the SQLite index and `compare`.

## Non-goals

- Installing Docker or any coding CLI. The repo works with what is present and
  skips cleanly when it is not.
- A distributed scheduler, cost budgets, or a leaderboard.
- Replacing the existing `LLMAdapter`; the CLI adapter is additive.
- Browsing/desktop environments (BrowserGym / OSWorld stay scaffolds).

## Workstream 1 — Docker as the verified default

### Binary discovery and preflight

- `DockerEnvironment.__init__` gains `docker_binary: str = "docker"`.
- New `resolve_docker_binary(configured: str | None = None) -> str | None` in
  the Docker adapter: honours an explicit value, then
  `BENCHMARK_DOCKER_BIN`, then `shutil.which("docker")`, then known macOS
  Docker Desktop paths. Returns `None` when nothing is found.
- `reset()` fails fast with an actionable error naming the binary and how to
  point at it, instead of a bare `FileNotFoundError`.
- A cheap preflight (`docker version --format {{.Server.Version}}`) runs once
  per reset with a short timeout; a missing daemon is a clear
  `RuntimeError`, not a confusing exec failure.

### Default flip

- `TaskSpec.environment_type` default changes `"local"` → `"docker"`.
- `build_environment` keeps both routes; `"local"` must now be explicit.
- `tasks/hello_task/task.yaml` and `configs/example.yaml` explicitly opt into
  `local`, so the offline smoke path keeps working without Docker.
- `LocalEnvironment` gains no new behaviour; the security posture is unchanged,
  only its default status.

### Tests

- **Unit (no daemon):** argv construction for `reset` / `run_command` /
  `verify_command` / `cleanup` is asserted by injecting a fake docker runner,
  so the security-relevant flags (`--network none`, `--user`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, read-only verifier mount) are pinned
  without Docker.
- **Integration (gated):** a marked test that skips unless
  `resolve_docker_binary` finds a working daemon. It pulls/uses a small base
  image (default `python:3.12-slim`, overridable via `BENCHMARK_TEST_IMAGE`),
  then asserts: a command runs and returns stdout; a non-zero exit is a tool
  result; the verifier runs read-only and cannot write to the workspace; and
  `cleanup` removes the container.
- The image is pulled at most once per session; a failed pull skips rather
  than fails (`pytest.skip`) so an offline CI stays green.

## Workstream 2 — generic coding-CLI agent adapter

### Adapter

New `src/benchmark/adapters/agents/cli.py`, `agent: cli`:

```python
class CLIAgent:
    agent_id = "cli"
    def __init__(self, instruction, *, command, args=None, prompt_mode="argv",
                 prompt_flag=None, stdin_text=None, timeout=600, max_steps=1,
                 env=None, cwd=None, success_exit_codes=(0,), final_message_chars=4000): ...
```

- `command` is the executable (resolved with `shutil.which`, so an absolute path
  also works); `args` is a template list where `{instruction}` and `{prompt_file}`
  are substituted.
- `prompt_mode`: `argv` (instruction embedded in args), `stdin`, or
  `prompt_file` (write the instruction to a temp file and substitute its path —
  the safest for long prompts and the mode CLI tools expect).
- One-shot by default (a coding CLI completes the task in one invocation);
  `max_steps` allows repeated invocations for tools that need it.
- Returns a normalized `AgentResult`: real wall-clock duration, the process exit
  code mapped to `completed`/`error`, and a bounded tail of combined output as
  `final_message`. Token counts stay 0 (a CLI does not report usage).
- Non-zero exit outside `success_exit_codes` → `status="error"`, so the existing
  retry policy applies.

### Profiles

A `profiles.py` (or module constants) with two documented presets so users do
not have to rediscover the invocation:

- `claude`: `claude -p --output-format json` style (instruction via `-p`).
- `codex`: `codex exec` style (instruction via argv/stdin).

Profiles are plain dicts merged into task config; they are documentation-backed
defaults, not hardcoded behaviour. Since no CLI is installed, the profiles are
**not verified against the real tools** — that limitation is recorded, and the
adapter is verified against a stub.

### Tests (offline)

- A stub executable script (created in `tmp_path`, `chmod +x`) that echoes its
  argv/stdin and exits with a configurable code. Assert: prompt delivery in all
  three modes; success and failure exit mapping; timeout produces an error
  result; the instruction reaches the process intact.
- No real CLI and no network.

## Workstream 3 — task set with oracles and indexed violations

### Task set

Add four small, deterministic, offline tasks under `tasks/`, each with
`instruction.md`, `tests/test.sh`, `task.yaml`, and `oracle.sh` (the reference
solution):

- `fix_fizzbuzz` — a Python file with an off-by-one bug; the verifier runs
  `pytest`-style assertions on the function.
- `parse_log_counts` — read a bundled log file, emit counts to `out.json`; the
  verifier checks the JSON.
- `makefile_target` — repair a broken `Makefile` target; the verifier runs
  `make` and checks the produced artifact.
- `seed_reproducible` — assert the seed-derived value is written; exercises the
  existing `BENCHMARK_SEED` plumbing.

`configs/tasks.yaml` loads them via `tasks_root`.

### Oracle support

- `oracle.sh` is the reference solution. A new CLI subcommand
  `benchmark oracle --tasks-root tasks [--output DIR]` runs the oracle against
  each task in a fresh workspace and reports whether each task's own verifier
  passes on the reference solution. This is the task-set self-consistency check
  (the harness's analogue of Terminal-Bench's oracle agent) and is the main
  guard against an unsolvable or mis-specified task.
- The oracle runs through `LocalEnvironment` in a temp dir with `--network none`
  semantics; it never touches the Docker default.

### Index and compare

- `index.py` schema version 1 → 2; `trials` gains `violations` and
  `uncertainties` (JSON text) plus `violation_count`; `runs` gains
  `violation_count` and `uncertain_trials`.
- Migration: on open, missing columns are added with `ALTER TABLE ... ADD COLUMN`
  so a v1 database keeps working; the version is recorded in `PRAGMA user_version`.
- `benchmark compare` surfaces per-run violation/uncertainty counts and gains a
  `--violations` filter that restricts to runs with at least one violation.
- `RunSummary` already carries the counts; the index reads them from trials.

## Known limitations (recorded, not hidden)

- The Docker integration test is skipped wherever no daemon is reachable; CI
  without Docker therefore does not exercise the real container path.
- The first Docker run needs a network pull; an offline host skips.
- The `claude` / `codex` profiles are unverified against the real CLIs (none
  installed). Only the generic adapter mechanics are verified.
- `LocalEnvironment` remains a non-sandbox. Demoting it to opt-in reduces
  accidental exposure; it does not make it safe.

## Validation

- `make check` green (ruff, mypy, pytest), with Docker integration tests skipped
  when no daemon is available.
- With Docker available: the gated integration test passes against a pulled
  base image; `verify_command` proves the read-only mount.
- `benchmark oracle --tasks-root tasks` reports every task's oracle passing.
- `benchmark index` on a run containing a policy violation, then
  `benchmark compare --violations`, shows the violation counts.
