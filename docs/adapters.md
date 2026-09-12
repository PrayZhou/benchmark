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
