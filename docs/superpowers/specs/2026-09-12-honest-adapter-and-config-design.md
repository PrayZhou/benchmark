# Honest adapters, real config fields, and real replay

Date: 2026-09-12
Status: approved (design), pending implementation

## Background

Four places in the harness claim more than they deliver:

1. Harbor, BrowserGym, OSWorld, MCP and multi-agent adapters are honest
   scaffolds: they import-safely, check for an optional dependency, and refuse
   to run. That is intentional, but the scaffolds are inconsistent with each
   other (e.g. `HarborEnvironment.reset` raises `OptionalDependencyError` even
   when the dependency *is* present).
2. There is an `MCPAgent`, but the agent factory has no `agent: mcp` route while
   the environment factory does have `environment_type: mcp`. The asymmetry is
   an oversight, not a design decision.
3. `ProviderConfig.provider` does not select a backend — every LLM run goes to
   the OpenAI-compatible path regardless of the string. `RunConfig.dataset` is
   carried into the manifest and the SQLite index but nothing sets it
   meaningfully or queries by it.
4. `replay.py` only lists recorded actions; it does not replay execution.
   `regrade.py` re-scores an *artifact snapshot* while the surrounding language
   ("replay", "regrade") suggests re-scoring the original run.

## Goals

- Make the optional-adapter layer uniform and routable, without installing any
  third-party SDK or fabricating a bridge.
- Make `provider` and `dataset` load-bearing: an unknown provider is rejected,
  and a dataset label has a consumer.
- Make `replay` actually re-execute recorded actions and report divergences;
  make `regrade` state exactly what it re-scores.
- Keep all new behavior covered by offline tests; `make check` stays green.

## Non-goals

- Implementing real Harbor / BrowserGym / OSWorld / MCP / multi-agent SDK
  bridges. They remain deliberate refusal points.
- Adding new backend providers beyond the existing OpenAI-compatible one.
- Changing the statistical reporting semantics or the retry model.

## Workstream 1 — Uniform optional-adapter scaffolds (findings 1 and 2)

### Shared helper

Add to `src/benchmark/adapters/errors.py`:

```python
def unavailable(package: str, *, feature: str, needs: str) -> NoReturn:
    """Refuse an optional adapter: missing dep vs missing bridge.

    Raises OptionalDependencyError when the optional package is absent, and
    NotImplementedError when it is present but the bridge is not written yet.
    """
```

- `require_optional(package, feature=feature)` keeps its current behavior.
- When the package imports, raise
  `NotImplementedError(f"{feature} backend wiring is not implemented ({needs})")`.

### Apply uniformly

Every scaffold constructor and `reset()` uses `unavailable(...)` with a
feature-specific `needs` string:

- `HarborEnvironment` — needs "a Harbor SDK client that resets and executes against a Harbor task"
- `BrowserGymEnvironment` — needs "a BrowserGym obs/action bridge"
- `OSWorldEnvironment` — needs "an OSWorld controller bridge"
- `MCPEnvironment` — needs "an MCP tool-call server bridge"
- `MCPAgent` — needs "an MCP client that maps MCP tools to harness actions"
- `MultiAgentAdapter` — needs "a multi-agent runtime bridge"

This removes the Harbor `reset` inconsistency: `reset()` now reports the same
reason as the constructor.

### Factory routing

`src/benchmark/factories.py`:

```python
if task.agent == "mcp":
    from .adapters.agents.mcp import MCPAgent
    return MCPAgent()  # honest scaffold; raises its clear refusal
```

Placed alongside the existing `multi_agent` route. No environment change.

### Docs

- `docs/adapters.md`: list the `agent: mcp` route; state the uniform contract
  (missing dep vs present-but-unwired).
- `README.md`: correct the roadmap/ability sections so the scaffolds are
  described consistently.

### Tests

- Extend `tests/test_optional_adapters.py`: add an `agent: mcp` route to
  `ROUTES`; parametrize the missing-dependency and present-but-unwired
  assertions over all six adapters; assert the message names the feature.
- Assert `HarborEnvironment.reset` and the constructor raise the same error
  class for the same dependency state.

## Workstream 2 — `provider` selects a backend; `dataset` has a consumer (finding 3)

### Backend registry

New `src/benchmark/adapters/agents/backends.py`:

```python
BACKENDS: dict[str, Callable[..., LLMBackend]] = {
    "openai": OpenAIBackend,
    "openai-compatible": OpenAIBackend,
}
UNSET_PROVIDERS = {"", "mock"}      # the CLI's "not configured" sentinel
DEFAULT_BACKEND = "openai-compatible"

def resolve_backend(name: str | None) -> tuple[str, Callable[..., LLMBackend]]:
    normalized = (name or "").strip().lower()
    key = DEFAULT_BACKEND if normalized in UNSET_PROVIDERS else normalized
    if key not in BACKENDS:
        raise ValueError(
            f"unsupported provider {name!r}; supported: {', '.join(sorted(BACKENDS))}"
        )
    return key, BACKENDS[key]
```

`OpenAIBackend` **moves** into `backends.py` (it keeps its lazy `openai`
import). `llm.py` imports it and re-exports it, so
`from benchmark.adapters.agents.llm import OpenAIBackend` keeps working. The
import direction is one-way (`llm` → `backends`), which avoids a cycle between
the registry and the adapter.

### `LLMAdapter`

- New constructor parameter `provider: str = DEFAULT_BACKEND` and
  `backend_factory: Callable[..., LLMBackend] | None = None`.
- `_backend()` resolves lazily: an injected `backend` wins; otherwise construct
  via `backend_factory` (or `resolve_backend(self.provider)[1]`).
- The `openai` import and API-key check stay deferred to the first `act()`, so
  factory-level construction never requires credentials.

### `build_agent`

```python
if task.agent == "llm":
    ...
    name, factory = resolve_backend(provider.provider)
    return LLMAdapter(..., provider=name, backend_factory=factory)
```

Unknown provider → `ValueError` at build time, before any trial runs.

`AgentResult.provider` and `TrialResult.provider` continue to be populated by
the orchestrator from `task.provider.provider`; the recorded value is now the
one that actually selected a backend. `manifest.provider_summary` is unchanged.

### `dataset` as provenance with a query

- `RunConfig.dataset` stays optional and is surfaced at the top level of
  `report.json` (`write_report(results, output_dir, dataset=...)`), alongside
  the existing `manifest.run_config.dataset` and `runs.dataset` index column.
- `benchmark index` already stores it. Add `--dataset NAME` to
  `benchmark compare`, filtering the `runs` table (`WHERE dataset = ?`).
  `RunIndex.compare(..., dataset: str | None = None)`.
- No task-selection semantics: the label identifies a run set for comparison,
  which is the only meaning the index can honor.

### Tests

- `resolve_backend`: aliases map to `OpenAIBackend`; `""`/`"mock"` fall back;
  unknown raises with the supported list in the message.
- `build_agent`: unknown provider raises; `"openai"` resolves; injected backend
  is still used verbatim.
- `dataset`: report carries it; `compare --dataset` returns only matching runs
  and omits non-matching ones.

## Workstream 3 — real replay and explicit regrade (finding 4)

### replay

Rewrite `src/benchmark/replay.py`:

```python
def replay_trial(run_dir, trial_id, *, execute: bool = True,
                 timeout: float | None = None) -> dict[str, Any]
```

- Resolve the trial and its `TaskSpec` from `manifest.json` (same lookup as
  `regrade`), then load the trace events.
- `execute=False`: return `{"mode": "list", "actions": [...]}` — explicitly a
  listing, never called a replay.
- `execute=True`:
  - create a temp workspace, `build_environment(task, workspace)`, `reset(task)`;
  - walk the recorded `action` events in step order, calling
    `environment.execute(action, timeout=remaining)` and pairing each with the
    `tool_result` recorded at the same step;
  - `match` compares `ok` and `returncode` always, and `stdout` only when the
    recorded value is not truncated (`"...[truncated "` absent), since trace
    truncation is lossy by design;
  - return `{"mode": "replay", "executed": True, "steps": [...],
    "divergences": [...], "action_count": n}`;
  - always `cleanup()` the environment and remove the temp workspace.
- Environments that cannot reconstruct (the optional scaffolds) propagate their
  `OptionalDependencyError`/`NotImplementedError` unchanged.

`write_replay` gains the same kwargs and stores the richer document.

CLI: `benchmark replay RUN_DIR TRIAL_ID [--no-execute]`, default executes.

### regrade

Keep the mechanism (rebuild a workspace from the archived artifact snapshot and
run the supplied verifier) but make the contract explicit:

- Output gains `"mode": "artifact_snapshot"`, `"artifacts_found": bool`,
  `"artifact_index": <path|None>`, `"grader_isolated": bool`.
- If the artifact directory is missing, raise `FileNotFoundError` naming the
  expected path instead of silently grading an empty workspace.
- `grader_mode` is computed from the grader's `isolated` attribute rather than
  from `hasattr(environment, "verify_command")`.

### Docs

README and `docs/adapters.md` state the distinction plainly: **replay**
re-executes recorded actions as a determinism check; **regrade** re-scores the
archived artifact snapshot with a grader. They are not the same operation and
neither re-runs the original agent.

### Tests

- `tests/test_replay.py`: a scripted trial's trace replays with zero
  divergences; a trace whose action was edited to diverge reports it; a
  truncated stdout does not count as divergence; `execute=False` returns
  `mode == "list"`.
- `tests/test_regrade.py`: regrade against a real artifact snapshot reports
  `mode == "artifact_snapshot"` and the verifier result; missing artifacts raise
  `FileNotFoundError`.

## File map

- `src/benchmark/adapters/errors.py` — `unavailable` helper.
- `src/benchmark/adapters/environments/{harbor,browsergym,osworld,mcp}.py` — use it.
- `src/benchmark/adapters/agents/{mcp,multi_agent}.py` — use it.
- `src/benchmark/factories.py` — `agent: mcp` route; backend resolution.
- `src/benchmark/adapters/agents/backends.py` — new registry.
- `src/benchmark/adapters/agents/llm.py` — `provider` / `backend_factory`.
- `src/benchmark/schemas.py` — no breaking change expected.
- `src/benchmark/replay.py`, `src/benchmark/regrade.py` — real replay / explicit regrade.
- `src/benchmark/reporting.py` — optional dataset in the report.
- `src/benchmark/index.py` — dataset filter in `compare`.
- `src/benchmark/cli.py` — `--no-execute`, `compare --dataset`.
- `docs/adapters.md`, `README.md` — accuracy.
- `tests/*` — new and extended coverage.

## Error handling

- Optional adapters: `OptionalDependencyError` (absent dep) or
  `NotImplementedError` (present, unwired) — never a fabricated success.
- Unknown provider: `ValueError` listing supported backends.
- Replay: propagate the environment's own refusal; never report a partial replay
  as a full one.
- Regrade: `FileNotFoundError` when the artifact snapshot is absent.
- No credentials in messages or reports; existing logging rules still apply.

## Risks and mitigations

- **Backward compatibility of `provider: "mock"`.** Keep it as the unset
  sentinel mapping to `openai-compatible`, matching `cli.py` merge behavior.
- **Lazy backend construction.** Tests inject `backend`; production resolution
  happens inside `_backend()` so no key is needed at build time.
- **Truncated traces.** Replay compares stdout only when trustworthy and always
  compares return codes, avoiding false divergences.
- **Replay side effects.** Replay runs in a fresh temp workspace and never
  touches the original run directory.

## Validation

- `make check` (ruff, mypy, pytest) passes.
- Manual smoke: `benchmark run configs/example.yaml`, then
  `benchmark replay <run_dir> <trial_id>` and
  `benchmark replay <run_dir> <trial_id> --no-execute`.
