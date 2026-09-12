import time
from pathlib import Path

import pytest

from benchmark.adapters.agents.scripted import ScriptedAgent
from benchmark.adapters.environments.local import LocalEnvironment
from benchmark.adapters.graders.command import CommandGrader
from benchmark.reporting import summarize
from benchmark.runner import run_trial
from benchmark.schemas import TaskSpec


def test_scripted_trial_succeeds(tmp_path: Path) -> None:
    task = TaskSpec(
        id="test.marker",
        instruction="create marker",
        environment={"actions": [{"type": "command", "command": ["bash", "-lc", "printf READY > marker.txt"]}, {"type": "finish"}]},
        grader={"command": ["bash", "-lc", "test -f marker.txt && test \"$(cat marker.txt)\" = READY"]},
    )
    result = run_trial(
        task,
        LocalEnvironment(tmp_path),
        ScriptedAgent(task.environment["actions"]),
        CommandGrader(task.grader["command"]),
        tmp_path / "traces",
        trial_id="trial-success",
    )
    assert result.success is True
    assert result.score == 1.0
    assert (tmp_path / "marker.txt").read_text() == "READY"
    assert result.termination_reason == "agent_finished"


def test_seed_reaches_the_environment_and_derived_runs_differ(tmp_path: Path) -> None:
    """The derived seed must be observable by the agent's commands."""
    from benchmark.orchestrator import run_tasks

    task = TaskSpec(
        id="test.seed",
        instruction="record the seed",
        seed=42,
        environment={"actions": [
            {"type": "command", "command": ["bash", "-lc", 'printf "$BENCHMARK_SEED" > seed.txt']},
            {"type": "finish"},
        ]},
        grader={"command": ["bash", "-lc", "test -s seed.txt"]},
    )
    from benchmark.schemas import RunConfig

    output = tmp_path / "out"
    results = run_tasks([task], tmp_path, output, RunConfig(runs_per_task=3))
    by_index = {result.run_index: result for result in results}
    assert [by_index[i].seed for i in range(3)] == [42, 43, 44]
    for index in range(3):
        recorded = output / "artifacts" / "test.seed" / str(index) / "1" / "seed.txt"
        assert recorded.read_text() == str(42 + index)


def test_trial_records_max_steps_termination(tmp_path: Path) -> None:
    from benchmark.schemas import Limits

    task = TaskSpec(
        id="test.steps",
        instruction="never finish",
        limits=Limits(max_steps=2),
        environment={"actions": []},
        grader={"command": ["bash", "-lc", "true"]},
    )
    never_finishing = [{"type": "command", "command": ["bash", "-lc", "true"]}] * 5
    result = run_trial(
        task,
        LocalEnvironment(tmp_path),
        ScriptedAgent(never_finishing),
        CommandGrader(task.grader["command"]),
        tmp_path / "traces",
        trial_id="trial-steps",
    )
    assert result.termination_reason == "max_steps"


def test_trial_records_artifact_index(tmp_path: Path) -> None:
    from benchmark.manifest import ArtifactStore

    task = TaskSpec(
        id="test.index",
        instruction="write a file",
        environment={"actions": [
            {"type": "command", "command": ["bash", "-lc", "printf READY > marker.txt"]},
            {"type": "finish"},
        ]},
        grader={"command": ["bash", "-lc", "test -f marker.txt"]},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = run_trial(
        task,
        LocalEnvironment(workspace),
        ScriptedAgent(task.environment["actions"]),
        CommandGrader(task.grader["command"]),
        tmp_path / "traces",
        trial_id="trial-index",
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    assert result.artifact_index_path is not None
    assert Path(result.artifact_index_path).exists()
    assert "marker.txt" in result.artifacts


def test_local_environment_reports_a_bad_command_as_a_tool_result(tmp_path: Path) -> None:
    """A wrong command must not become a runner_error; the agent needs the feedback."""
    environment = LocalEnvironment(tmp_path)
    environment.reset(TaskSpec(id="test.bad-command", instruction="x"))
    result = environment.execute({"type": "command", "command": ["definitely-not-a-real-binary-xyz"]})
    assert result["ok"] is False
    assert result["returncode"] == 127
    assert result["stderr"]


def test_summary_counts_failures() -> None:
    from benchmark.schemas import TrialResult

    results = [
        TrialResult(task_id="a", trial_id="1", agent_id="x", seed=0, status="completed", success=True, score=1),
        TrialResult(task_id="b", trial_id="2", agent_id="x", seed=0, status="failed", failure_category="test_failure"),
    ]
    summary = summarize(results)
    assert summary.total == 2
    assert summary.success_rate == 0.5
    assert summary.failures_by_category == {"test_failure": 1}


def _attempt(trial_id: str, group: str | None, attempt: int, *, success: bool, category: str | None = None):
    from benchmark.schemas import TrialResult

    return TrialResult(
        task_id="t", trial_id=trial_id, agent_id="x", seed=0,
        status="completed" if success else "error", success=success,
        score=1.0 if success else 0.0, attempt=attempt,
        parent_trial_id=group, failure_category=category, duration_seconds=1.0,
    )


def test_summary_counts_each_retry_group_once() -> None:
    """A trial that succeeds on its third attempt is one success, not one-in-three."""
    summary = summarize([
        _attempt("a1", "g1", 1, success=False, category="runner_error"),
        _attempt("a2", "g1", 2, success=False, category="runner_error"),
        _attempt("a3", "g1", 3, success=True),
    ])
    assert summary.total == 1
    assert summary.success_rate == 1.0
    assert summary.failures_by_category == {}
    assert summary.total_attempts == 3
    assert summary.retried_groups == 1


def test_summary_reports_only_final_failure_category() -> None:
    summary = summarize([
        _attempt("b1", "g2", 1, success=False, category="runner_error"),
        _attempt("b2", "g2", 2, success=False, category="test_failure"),
    ])
    assert summary.total == 1
    assert summary.success_rate == 0.0
    assert summary.failures_by_category == {"test_failure": 1}


def test_summary_groups_are_independent() -> None:
    summary = summarize([
        _attempt("c1", "g3", 1, success=True),
        _attempt("d1", "g4", 1, success=False, category="test_failure"),
        _attempt("d2", "g4", 2, success=False, category="test_failure"),
    ])
    assert summary.total == 2
    assert summary.success_rate == 0.5
    assert summary.total_attempts == 3


def test_summary_without_parent_id_treats_each_result_as_a_group() -> None:
    summary = summarize([
        _attempt("e1", None, 1, success=True),
        _attempt("e2", None, 1, success=False, category="test_failure"),
    ])
    assert summary.total == 2
    assert summary.success_rate == 0.5
    assert summary.retried_groups == 0


def test_summary_handles_empty_results() -> None:
    summary = summarize([])
    assert summary.total == 0
    assert summary.success_rate == 0.0
    assert summary.total_attempts == 0


def test_report_keeps_every_attempt_for_audit(tmp_path: Path) -> None:
    import json

    from benchmark.reporting import write_report

    path = write_report([
        _attempt("f1", "g5", 1, success=False, category="runner_error"),
        _attempt("f2", "g5", 2, success=True),
    ], tmp_path)
    payload = json.loads(path.read_text())
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["success_rate"] == 1.0
    assert len(payload["trials"]) == 2


def test_local_environment_does_not_leak_host_credentials(tmp_path: Path, monkeypatch) -> None:
    """Host secrets must not reach agent commands through the environment."""
    monkeypatch.setenv("COMATE_API_KEY", "sk-super-secret")
    environment = LocalEnvironment(tmp_path)
    environment.reset(TaskSpec(id="test.env-leak", instruction="x"))
    completed = environment.run_command(["bash", "-lc", "printf '%s' \"${COMATE_API_KEY:-unset}\""])
    assert completed.stdout == "unset"
    assert "sk-super-secret" not in completed.stdout


def test_local_environment_injects_seed_and_explicit_extras(tmp_path: Path) -> None:
    environment = LocalEnvironment(tmp_path, env={"TASK_TOKEN": "visible"})
    environment.reset(TaskSpec(id="test.env-extras", instruction="x", seed=7))
    completed = environment.run_command(["bash", "-lc", "printf '%s %s' \"$TASK_TOKEN\" \"$BENCHMARK_SEED\""])
    assert completed.stdout == "visible 7"


def test_environment_command_timeout_is_configurable(tmp_path: Path) -> None:
    environment = LocalEnvironment(tmp_path, command_timeout=0.2)
    environment.reset(TaskSpec(id="test.command-timeout", instruction="x"))
    started = time.monotonic()
    completed = environment.run_command(["sleep", "2"])
    assert completed.returncode == 124
    assert time.monotonic() - started < 1.5


def test_single_command_cannot_outrun_the_trial_budget(tmp_path: Path) -> None:
    from benchmark.schemas import Limits

    task = TaskSpec(
        id="test.budget",
        instruction="sleep past the budget",
        limits=Limits(max_steps=3, timeout_seconds=0.5),
        environment={"actions": [{"type": "command", "command": ["sleep", "2"]}]},
        grader={"command": ["bash", "-lc", "false"]},
    )
    started = time.monotonic()
    result = run_trial(
        task,
        LocalEnvironment(tmp_path),
        ScriptedAgent(task.environment["actions"]),
        CommandGrader(task.grader["command"], isolated=False),
        tmp_path / "traces",
        trial_id="trial-budget",
    )
    assert result.termination_reason == "timeout"
    assert time.monotonic() - started < 1.5


def test_agent_errors_are_categorized_and_retryable(tmp_path: Path) -> None:
    """A model/gateway error must not be masked by the grader's test_failure."""
    from benchmark.schemas import AgentResult, RetryPolicy

    assert "error" in RetryPolicy().retryable_categories

    class ErroringAgent:
        agent_id = "erroring"

        def __init__(self) -> None:
            self._result: AgentResult | None = None

        def act(self, observation: dict) -> dict:
            del observation
            self._result = AgentResult(status="error", error="gateway 404")
            return {"type": "finish"}

        def finished(self) -> bool:
            return self._result is not None

        def result(self) -> AgentResult:
            return self._result

    task = TaskSpec(id="test.agent-error", instruction="x", grader={"command": ["bash", "-lc", "false"]})
    result = run_trial(
        task,
        LocalEnvironment(tmp_path),
        ErroringAgent(),
        CommandGrader(task.grader["command"], isolated=False),
        tmp_path / "traces",
        trial_id="trial-agent-error",
    )
    assert result.status == "error"
    assert result.success is False
    assert result.failure_category == "error"


def test_run_tasks_retries_agent_errors_by_default(tmp_path: Path, monkeypatch) -> None:
    from benchmark import orchestrator
    from benchmark.schemas import AgentResult, RetryPolicy, RunConfig

    class ErroringAgent:
        agent_id = "erroring"

        def __init__(self) -> None:
            self._result: AgentResult | None = None

        def act(self, observation: dict) -> dict:
            del observation
            self._result = AgentResult(status="error", error="gateway 404")
            return {"type": "finish"}

        def finished(self) -> bool:
            return self._result is not None

        def result(self) -> AgentResult:
            return self._result

    task = TaskSpec(
        id="test.retry-error",
        instruction="x",
        environment={"actions": [
            {"type": "command", "command": ["bash", "-lc", "printf READY > marker.txt"]},
            {"type": "finish"},
        ]},
        grader={"command": ["bash", "-lc", "test -f marker.txt"]},
    )
    built: list[str] = []

    def build(task_: TaskSpec):
        built.append(task_.id)
        return ErroringAgent() if len(built) == 1 else ScriptedAgent(task_.environment["actions"])

    monkeypatch.setattr(orchestrator, "build_agent", build)
    results = orchestrator.run_tasks([task], tmp_path, tmp_path / "out",
                                     RunConfig(retry=RetryPolicy(max_attempts=2)))
    assert [result.failure_category for result in results] == ["error", None]
    assert results[0].success is False
    assert results[-1].success is True


def test_agent_result_rejects_the_dead_failed_status() -> None:
    import pydantic

    from benchmark.schemas import AgentResult

    with pytest.raises(pydantic.ValidationError):
        AgentResult(status="failed")
