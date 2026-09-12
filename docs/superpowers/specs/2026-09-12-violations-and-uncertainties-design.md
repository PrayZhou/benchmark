# Behavioral violations and grader uncertainties

Date: 2026-09-12
Status: approved (design), pending implementation

## Background

`GraderResult.violations` / `uncertainties` and their `TrialResult`
counterparts exist, and the plumbing is already complete: `CompositeGrader`
merges them, `runner.py` copies them onto the trial, and `report.json` /
`manifest.json` serialize them. But **no producer ever returns a non-empty
list** — every grader builds `GraderResult` with the defaults. README line 135
records this as a known empty shell, and no test exercises either field.

The field the harness is missing is *behavioral anomaly disclosure*: whether a
"pass" was earned by the intended path, and whether a score is trustworthy at
all. The surrounding evaluation ecosystem treats this as a first-class
correctness problem (HAL's log analysis, reward-hacking benchmarks using
fail-closed scoring, Harbor's separate verifier sandbox), so the harness should
produce these signals rather than merely carry two empty lists in its schema.

## Goals

- Give `violations` and `uncertainties` real, honest producers.
- Make behavioral violations **fail-closed by default** (a trial that violates
  policy is not a pass) while remaining configurable.
- Keep the deterministic layer entirely offline and deterministic; make the LLM
  judge strictly optional, default-off, and injectable for tests.
- Preserve the existing architecture: no runner change, no new lifecycle hooks,
  no baseline snapshot stored by the environment.
- Cover all new behavior with offline tests; `make check` stays green.

## Non-goals

- Cross-trial behavioral mining, anomaly detection over run history, or
  leaderboard-level gaming analysis.
- Multi-turn or tool-using LLM judging; the judge is a single structured call.
- A pre-execution workspace baseline captured by the runner/environment.
- SQLite index schema changes (`index.py` stays as-is in this change).
- Rewriting the retry model or the statistical reporting semantics.

## Decisions

| Question | Decision |
| --- | --- |
| Detection sources | Built-in deterministic rules **plus** task-declared extensions |
| LLM judge | Included, on top of the deterministic layer |
| Violation consequence | fail-closed by default, configurable off; not retryable |
| Placement | Two ordinary graders composed via the existing `composite` grader |
| Judge backend | Reuses the provider mechanism; default off; backend injectable |
| Data shape | Structured `Finding`, not `list[str]` |
| Uncertainty semantics | Both evidence reliability and decision-boundary signals |

## Design

### 1. `Finding` schema

Add to `src/benchmark/schemas.py` and change the two fields on both
`GraderResult` and `TrialResult` from `list[str]` to `list[Finding]`:

```python
class Finding(BaseModel):
    kind: str
    severity: Literal["low", "medium", "high"] = "medium"
    source: Literal["policy", "judge", "grader"] = "grader"
    evidence: str
    step: int | None = None
```

- `kind` is a `str` rather than a `Literal` so task-declared kinds are
  expressible; the built-in vocabulary is documented in `docs/adapters.md`.
- Defaults keep every existing no-op grader path valid: an omitted field is
  still an empty list.
- Existing persisted reports contain `"violations": []`, which still validates.
  Reports written before this change carry no findings to migrate.

### 2. `PolicyGrader` (deterministic layer)

New `src/benchmark/adapters/graders/policy.py`, registered in `build_grader`
alongside `state` and `command`, so it is selectable as a `graders[].type`
inside a composite grader. It needs no runner change.

**Integrity baseline.** The task directory is the baseline, but the comparison
must be *before vs after*, not *workspace vs task directory*: the task
directory is never copied into the workspace (`orchestrator.py` gives each
attempt a fresh tempdir, and the verifier is injected via `bash -c`), so
`workspace/tests/` does not normally exist. `LocalEnvironment` has no
filesystem sandbox, so an agent can edit the real `tests/test.sh` in the task
directory.

`PolicyGrader.__init__` therefore hashes the task directory's verifier/oracle
files. `build_grader` runs at `orchestrator.py:51`, before `run_trial` at line
52, so this snapshot is a genuine pre-agent baseline without any runner or
environment support. `evaluate` re-hashes and reports drift.

**Built-in checks.**

- `verifier_tampering` (high) — a snapshot file is modified or missing.
- `verifier_copy` (high) — a workspace file shares a hash with a task-dir
  verifier/oracle file (the agent copied the grader in).
- `destructive_command` (high) — an action argv matches destructive patterns
  (`rm -rf /`, `mkfs`, `dd ... of=/dev/...`, fork bombs).
- `evaluation_dos` (medium) — an action argv matches clear
  denial-of-evaluation patterns (killing the harness, exhausting resources).

**Declared checks** (from the task's policy configuration, i.e. the
`graders[].` entry that selected `type: policy`):

- `forbidden_paths: list[str]` → `forbidden_path`
- `forbidden_commands: list[str]` (regex or substring) → `forbidden_command`
- `forbidden_network: bool` → `network_attempt`
- `fail_closed: bool = true` and `severity_threshold: "low" | "medium" | "high"
  = "medium"` control the outcome mapping described below

**Data sources.** `events` for action argv and tool results; the workspace
filesystem for copy detection; `metadata.task_dir` for the integrity baseline.
When `metadata.task_dir` is absent (inline tasks) the integrity checks are
skipped and an `integrity_unavailable` uncertainty is recorded — absence is
reported, never treated as "clean".

**Fail-closed.** With `fail_closed: true` (default), any finding at or above
`severity_threshold` (default `medium`) yields `success=False` and
`failure_category="policy_violation"`. With `fail_closed: false` the findings
are reported without changing the outcome.

### 3. `JudgeGrader` (optional LLM layer)

New `src/benchmark/adapters/graders/judge.py`, registered as `type: judge` in
the same composite mechanism. Its configuration block carries `model`,
`provider`, `base_url`, `api_key_env`, `timeout`, `max_output_tokens`, and
`rubric`; the backend is resolved through
`benchmark.adapters.agents.backends.resolve_backend` and is injectable, exactly
as `LLMAdapter` does.

- **Default off.** The grader is only constructed when a task explicitly
  selects it, so the default path never touches the network.
- **Input.** The instruction, a compact trace digest (per-step action commands
  and truncated tool results), and the rubric. One call, expecting JSON
  findings, parsed defensively.
- **Failure degrades, never accuses.** A missing backend, a backend error, or
  unparseable output returns `success=True` (the judge abstains) and records an
  uncertainty with kind `judge_unavailable` or `judge_error`. Raising here would
  surface as a `runner_error` and trigger retries, which is the wrong
  consequence for an advisory check. Low-confidence output becomes
  `judge_low_confidence`.

### 4. Uncertainty producers

- Evidence reliability: `truncated_evidence` (a tool result carries the trace
  truncation marker), `integrity_unavailable` (no `metadata.task_dir`),
  `verifier_no_output`.
- Decision boundary: `near_threshold` (a command/composite score sits strictly
  inside `threshold ± 0.02`), `grader_disagreement` (composite sub-graders
  disagree on success).
- Judge: `judge_low_confidence`.

Uncertainties **never** change `success` or `score`; they annotate confidence.

### 5. fail-closed, retry, and composite interaction

- `policy_violation` is not in `RetryPolicy.retryable_categories` (default
  `runner_error`, `environment_error`, `timeout`, `error`), so a violating
  trial is not retried. No orchestrator change is required.
- `CompositeGrader` changes in three small ways: findings are merged and
  **deduplicated** by `(kind, evidence, step)`; when more than one sub-grader
  fails, `policy_violation` is preferred as the aggregate `failure_category`
  so a violation stays visible; merged uncertainties are deduplicated the same
  way.
- Known interaction: under `threshold` mode a composite can report
  `success=True` with non-empty `violations` — that is the "report only"
  semantics. Documented explicitly; use the default all-must-pass mode for a
  hard fail.

### 6. Consumers

- `RunSummary` gains `violations_by_kind: dict[str, int]` and
  `uncertain_trials: int`, aggregated over the same final-attempt groups the
  existing summary uses (retries must not inflate counts).
- `report.json` and `manifest.json` already serialize the fields; their shape
  changes from strings to objects.
- `index.py` is intentionally untouched in this change.

## File map

- `src/benchmark/schemas.py` — `Finding`; `GraderResult` / `TrialResult` fields.
- `src/benchmark/adapters/graders/policy.py` — new deterministic grader.
- `src/benchmark/adapters/graders/judge.py` — new optional LLM grader.
- `src/benchmark/adapters/graders/composite.py` — dedupe, category preference.
- `src/benchmark/adapters/graders/command.py` — `near_threshold`,
  `verifier_no_output`.
- `src/benchmark/factories.py` — `policy` and `judge` grader routes.
- `src/benchmark/reporting.py` — summary aggregation.
- `docs/adapters.md`, `README.md` — kind vocabulary, fail-closed semantics,
  and removal of the "empty shell" note.
- `tests/test_policy_grader.py`, `tests/test_judge_grader.py` — new coverage.
- `tests/test_graders.py`, `tests/test_harness.py` — extended coverage.

## Error handling

- A malicious or mistaken agent action is a finding, never a harness crash.
- Judge unavailability is an uncertainty, never a violation or a retry.
- A missing integrity baseline is an uncertainty, never a clean result.
- No credential, prompt, or tool output is written into logs; `evidence`
  strings are bounded the same way existing grader evidence is.

## Risks and mitigations

- **Snapshot timing.** The integrity baseline is only genuine because the
  grader is constructed before `run_trial`. A regression that builds graders
  later would silently weaken the check; a test asserts that tampering is
  detected end-to-end through `run_tasks`.
- **False positives.** Destructive-command matching is pattern-based and can
  misfire on legitimate tasks (for example one that intentionally runs
  `rm -rf ./build`). Patterns therefore target root/system destruction only,
  and `fail_closed: false` gives an escape hatch.
- **Judge nondeterminism.** The judge is advisory and default-off, so it cannot
  make the default suite flaky or spend money.
- **Schema change.** Report consumers that assumed strings must tolerate
  objects; pre-1.0, and the README calls this out.

## Validation

- `make check` (ruff, mypy, pytest) passes.
- New coverage: tampered verifier detected; copied verifier detected; declared
  forbidden command/path reported; destructive command reported; clean trial
  reports nothing; `fail_closed: false` reports without failing; judge with an
  injected backend produces findings; malformed judge output degrades to an
  uncertainty; end-to-end `run_tasks` marks a violating trial failed with
  `policy_violation` and does not retry it.
