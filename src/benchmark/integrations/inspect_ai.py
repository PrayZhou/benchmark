from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..runner import run_trial
from ..schemas import TaskSpec, TrialResult

Builder = Callable[[TaskSpec], Any]
TrialRunner = Callable[..., TrialResult]


_MISSING = (
    "Inspect AI integration requires the optional 'inspect-ai' package. "
    "Install it with `pip install inspect-ai`."
)


def _load_inspect() -> tuple[Any, Any, Any, Any, Any, Any]:
    """Load Inspect symbols only when an Inspect task is requested."""
    try:
        from inspect_ai import Task
        from inspect_ai.dataset import MemoryDataset, Sample
        from inspect_ai.scorer import Score, scorer
        from inspect_ai.solver import solver
    except ImportError as exc:
        raise RuntimeError(_MISSING) from exc
    return Task, MemoryDataset, Sample, solver, scorer, Score


def _task(config: dict[str, Any]) -> TaskSpec:
    """Validate the adapter config through the benchmark's canonical schema."""
    return TaskSpec.model_validate(config)


def _record_result(state: Any, result: TrialResult) -> None:
    metadata = getattr(state, "metadata", None)
    if metadata is None:
        metadata = {}
        state.metadata = metadata
    metadata["benchmark_result"] = result.model_dump(mode="json")
    metadata["benchmark_success"] = result.success
    metadata["benchmark_score"] = result.score
    metadata["benchmark_status"] = result.status


def build_inspect_task(
    config: dict[str, Any],
    *,
    environment_builder: Builder,
    agent_builder: Builder,
    grader_builder: Builder,
    run_trial_fn: TrialRunner = run_trial,
    trace_dir: Path | None = None,
    artifact_store_builder: Callable[[TaskSpec], Any] | None = None,
) -> Any:
    """Build an Inspect task backed by the core :func:`run_trial` runner.

    Inspect is optional and imported lazily. Builders receive the validated
    ``TaskSpec`` and make the adapter's runtime dependencies explicit; this
    keeps the integration independent of any particular agent or environment.
    ``run_trial_fn`` is injectable for tests and alternate orchestration.
    """
    Task, MemoryDataset, Sample, solver, scorer, Score = _load_inspect()
    task_spec = _task(config)
    trial_trace_dir = trace_dir or Path(".inspect-traces")

    @solver
    def benchmark_solver():
        async def solve(state: Any, generate: Any) -> Any:
            del generate
            environment = environment_builder(task_spec)
            agent = agent_builder(task_spec)
            grader = grader_builder(task_spec)
            kwargs: dict[str, Any] = {
                "task": task_spec,
                "environment": environment,
                "agent": agent,
                "grader": grader,
                "trace_dir": trial_trace_dir,
            }
            if artifact_store_builder is not None:
                kwargs["artifact_store"] = artifact_store_builder(task_spec)
            result = run_trial_fn(**kwargs)
            _record_result(state, result)
            return state

        return solve

    @scorer
    def benchmark_scorer():
        async def score(state: Any, target: Any) -> Any:
            del target
            result_data = getattr(state, "metadata", {}).get("benchmark_result")
            if not result_data:
                return Score(value=0.0, answer="error", explanation="Benchmark solver did not produce a result")
            result = TrialResult.model_validate(result_data)
            return Score(
                value=result.score,
                answer="success" if result.success else "failure",
                explanation=result.error or result.status,
                metadata={"benchmark_result": result.model_dump(mode="json")},
            )

        return score

    sample = Sample(input=task_spec.instruction, target=task_spec.id)
    return Task(
        dataset=MemoryDataset([sample]),
        solver=benchmark_solver(),
        scorer=benchmark_scorer(),
    )


def run_inspect(config_path: Path, **kwargs: Any) -> Any:
    """Run an Inspect evaluation from a YAML config path.

    The Inspect ``eval`` import is also lazy. Runtime builders are forwarded to
    ``build_inspect_task`` so callers can choose their concrete adapters.
    """
    try:
        from inspect_ai import eval as inspect_eval
    except ImportError as exc:
        raise RuntimeError(_MISSING) from exc

    import yaml

    config = yaml.safe_load(config_path.read_text())
    task = build_inspect_task(config, **kwargs)
    return inspect_eval(task)
