# Optional adapters

The harness ships small, import-safe scaffolds for integrations that are not core dependencies:

- `HarborEnvironment`
- `BrowserGymEnvironment`
- `OSWorldEnvironment`
- `MCPEnvironment`
- `MCPAgent`
- `MultiAgentAdapter`

Importing these modules does not import third-party SDKs. Constructing an adapter checks for its optional dependency and raises `benchmark.adapters.OptionalDependencyError` with an actionable message when it is unavailable. If the dependency is installed, the current scaffold raises `NotImplementedError` rather than claiming a successful run; backend wiring remains intentionally explicit. Every scaffold uses the same `benchmark.adapters.errors.unavailable` helper, so `__init__` and `reset()` always report the same reason, and the message names the bridge that still has to be written.

```python
from benchmark.adapters import OptionalDependencyError
from benchmark.adapters.environments import HarborEnvironment

try:
    environment = HarborEnvironment()
except OptionalDependencyError as exc:
    print(exc)
```

These adapters are extension points. `benchmark.factories` resolves `environment_type: mcp` to `MCPEnvironment`, `agent: mcp` to `MCPAgent`, and `agent: multi_agent` to `MultiAgentAdapter`, but all of them still refuse to run until their backend bridge is implemented. Install and configure each upstream project according to its own documentation before implementing the bridge.

## Inspect AI

`benchmark.integrations.inspect_ai` provides an optional callback bridge. It lazily imports Inspect AI, validates configuration as the core `TaskSpec`, and runs the normal `benchmark.runner.run_trial` through injected builders:

```python
from benchmark.integrations.inspect_ai import build_inspect_task

task = build_inspect_task(
    config,
    environment_builder=make_environment,
    agent_builder=make_agent,
    grader_builder=make_grader,
)
```

Each builder receives the validated `TaskSpec`. The solver stores the serialized `TrialResult` in Inspect state metadata, and the scorer maps its score, success answer, status, and result metadata to an Inspect `Score`. Install the optional dependency with `pip install inspect-ai`; importing the integration module itself remains safe without it.

## Coding CLI agent

`agent: cli` runs an external coding CLI as a black box: the harness launches
the command once and maps its exit code and output to a normal `AgentResult`.
The command, its argument template, and how the prompt is delivered are all
task-supplied through the `cli:` block. Two documented starting points live in
`benchmark.adapters.agents.profiles` (`claude` and `codex`), so the invocation
does not have to be rediscovered per task:

```yaml
id: claude-refactor
instruction: Rename the function and keep the tests green.
agent: cli
environment_type: local
cli:
  command: claude
  args: ["{instruction}"]
  prompt_mode: argv        # argv | stdin | prompt_file
  prompt_flag: "-p"        # claude -p ... (non-interactive print mode)
  timeout: 300             # optional; clamped to the trial budget
```

`command` may be a bare name (resolved on `PATH`) or an absolute path.
`prompt_mode` chooses the delivery channel: `argv` substitutes `{instruction}`
into the args, `stdin` pipes it, and `prompt_file` writes it to a temp file and
substitutes `{prompt_file}`. Any profile key can be overridden in the `cli:`
block.

Known limitations:

- The CLI runs **on the host**, so `agent: cli` requires
  `environment_type: local`. A container environment reports a `/workspace`
  path that does not exist on the host; `build_agent` rejects the combination
  at build time, and container-delegated execution is not implemented yet.
- A missing binary, a timeout, or an unexpected exit code all become an
  `AgentResult(status="error")` rather than crashing the harness.
- The `claude` and `codex` profiles in `profiles.py` are **unverified**. No
  coding CLI is installed and egress is blocked in the development environment,
  so the presets encode the documented non-interactive invocations but have not
  been run against the real tools. Confirm them against the installed CLI
  version before relying on them.

## Behavioral findings

`GraderResult.violations` and `GraderResult.uncertainties` carry structured
`Finding` records (`kind`, `severity`, `source`, `evidence`, `step`).

- `PolicyGrader` (`grader_type: policy`) runs deterministic checks without any
  network: it snapshots the task directory's protected files at construction
  time and reports `verifier_tampering` when they change, `verifier_copy` when a
  workspace file matches a protected file, `destructive_command`,
  `evaluation_dos`, and the task-declared `forbidden_path` / `forbidden_command`
  / `network_attempt`. It also records a `truncated_evidence` uncertainty when a
  recorded `tool_result` carries the trace truncation marker. With
  `fail_closed: true` (default) any finding at or
  above `severity_threshold` (default `medium`) fails the trial with
  `failure_category: "policy_violation"`, which is deliberately not retryable.
  The `verifier_copy` scan is bounded: it caps at 5000 workspace files, skips
  files over 1 MiB and skips symlinks, so a flooding agent can evade that
  specific check. The task-directory tampering check is unaffected.
- `JudgeGrader` (`grader_type: judge`) is an optional single-call LLM check. It
  is only constructed when a task selects it, so the default path never touches
  the network. Backend failures and unparseable output become
  `judge_unavailable` / `judge_error` uncertainties rather than failures, and
  score `0.0`: an abstaining judge is advisory and cannot inflate a
  `threshold`-mode composite it takes part in.
- Uncertainties (`near_threshold`, `grader_disagreement`,
  `integrity_unavailable`, `truncated_evidence`, `judge_low_confidence`, ...)
  never change the score
  or success of a trial. The built-in `kind` vocabulary above is extensible by
  task-declared rules.
