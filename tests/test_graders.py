from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.adapters.environments.local import LocalEnvironment
from benchmark.adapters.graders.command import CommandGrader
from benchmark.adapters.graders.composite import CompositeGrader
from benchmark.factories import build_grader
from benchmark.schemas import GraderResult, TaskSpec


class FixedGrader:
    """A grader with a canned result, for composite/aggregation tests."""

    def __init__(self, result: GraderResult) -> None:
        self._result = result

    def evaluate(self, task, environment, events) -> GraderResult:
        del task, environment, events
        return self._result


def _environment(tmp_path: Path) -> LocalEnvironment:
    environment = LocalEnvironment(tmp_path)
    environment.reset(TaskSpec(id="t", instruction="x"))
    return environment


def test_command_grader_stays_binary_without_a_score_pattern(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    passed = CommandGrader(["bash", "-lc", "true"], isolated=False).evaluate(task, _environment(tmp_path), [])
    failed = CommandGrader(["bash", "-lc", "false"], isolated=False).evaluate(task, _environment(tmp_path), [])
    assert (passed.success, passed.score) == (True, 1.0)
    assert (failed.success, failed.score) == (False, 0.0)
    assert failed.failure_category == "test_failure"


def test_command_grader_awards_partial_credit(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    grader = CommandGrader(["bash", "-lc", "echo 'SCORE: 0.5'; exit 1"], isolated=False,
                           score_pattern=r"score:\s*([0-9.]+)", pass_threshold=0.6)
    result = grader.evaluate(task, _environment(tmp_path), [])
    assert result.score == 0.5
    assert result.success is False
    assert result.failure_category == "test_failure"


def test_command_grader_passes_when_the_score_meets_the_threshold(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    grader = CommandGrader(["bash", "-lc", "echo 'SCORE: 0.8'"], isolated=False,
                           score_pattern=r"score:\s*([0-9.]+)", pass_threshold=0.75)
    result = grader.evaluate(task, _environment(tmp_path), [])
    assert result.score == 0.8
    assert result.success is True


def test_command_grader_clamps_scores_into_range(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    grader = CommandGrader(["bash", "-lc", "echo 'SCORE: 1.7'"], isolated=False,
                           score_pattern=r"score:\s*([0-9.]+)")
    assert grader.evaluate(task, _environment(tmp_path), []).score == 1.0


def test_command_grader_reports_a_missing_score_as_a_verifier_error(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    grader = CommandGrader(["bash", "-lc", "echo done"], isolated=False,
                           score_pattern=r"score:\s*([0-9.]+)")
    result = grader.evaluate(task, _environment(tmp_path), [])
    assert result.success is False
    assert result.score == 0.0
    assert result.failure_category == "verifier_error"


def test_command_grader_validates_its_configuration() -> None:
    with pytest.raises(ValueError, match="capturing group"):
        CommandGrader(["true"], score_pattern=r"score:\s*[0-9.]+")
    with pytest.raises(ValueError, match="pass_threshold"):
        CommandGrader(["true"], pass_threshold=1.5)


def test_composite_grader_defaults_to_all_must_pass() -> None:
    task = TaskSpec(id="t", instruction="x")
    passing = FixedGrader(GraderResult(success=True, score=1.0))
    failing = FixedGrader(GraderResult(success=False, score=0.5, failure_category="test_failure"))
    assert CompositeGrader([passing, passing]).evaluate(task, None, []).success is True
    result = CompositeGrader([passing, failing]).evaluate(task, None, [])
    assert result.success is False
    assert result.score == 0.75
    assert result.failure_category == "test_failure"


def test_composite_grader_supports_weights_and_threshold() -> None:
    task = TaskSpec(id="t", instruction="x")
    graders = [
        FixedGrader(GraderResult(success=True, score=1.0)),
        FixedGrader(GraderResult(success=False, score=0.5, failure_category="test_failure")),
    ]
    result = CompositeGrader(graders, weights=[3.0, 1.0], threshold=0.8).evaluate(task, None, [])
    assert result.score == 0.875
    assert result.success is True


def test_composite_grader_validates_weights_and_threshold() -> None:
    grader = FixedGrader(GraderResult(success=True, score=1.0))
    with pytest.raises(ValueError, match="one entry per grader"):
        CompositeGrader([grader], weights=[1.0, 2.0])
    with pytest.raises(ValueError, match="non-negative"):
        CompositeGrader([grader], weights=[-1.0])
    with pytest.raises(ValueError, match="positive"):
        CompositeGrader([grader], weights=[0.0])
    with pytest.raises(ValueError, match="threshold"):
        CompositeGrader([grader], threshold=2.0)


def test_factory_wires_partial_credit_and_weighted_composites() -> None:
    command_task = TaskSpec(id="t", instruction="x", grader_type="command",
                            grader={"command": ["true"], "score_pattern": r"score:\s*([0-9.]+)",
                                    "pass_threshold": 0.5})
    grader = build_grader(command_task)
    assert grader.score_pattern == r"score:\s*([0-9.]+)"
    assert grader.pass_threshold == 0.5

    composite_task = TaskSpec(id="t", instruction="x", grader_type="composite", grader={
        "threshold": 0.5, "weights": [3.0, 1.0],
        "graders": [{"type": "command", "command": ["true"]}, {"type": "command", "command": ["false"]}],
    })
    composite = build_grader(composite_task)
    assert composite.weights == [3.0, 1.0]
    assert composite.threshold == 0.5
    assert len(composite.graders) == 2


def test_factory_reads_command_timeout_and_env_options(tmp_path: Path) -> None:
    from benchmark.factories import build_environment

    task = TaskSpec(id="t", instruction="x",
                    environment={"command_timeout": 0.5, "env_allowlist": ["PATH", "TASK_TOKEN"],
                                 "env": {"TASK_TOKEN": "visible"}})
    built = build_environment(task, tmp_path)
    assert built.command_timeout == 0.5
    assert built.env_allowlist == ("PATH", "TASK_TOKEN")
    assert built.env == {"TASK_TOKEN": "visible"}


def test_composite_grader_dedupes_and_prefers_policy_violation() -> None:
    from benchmark.schemas import Finding

    duplicated = Finding(kind="destructive_command", severity="high", source="policy", evidence="rm -rf /")
    policy = FixedGrader(GraderResult(success=False, score=0.0, failure_category="policy_violation",
                                      violations=[duplicated]))
    other = FixedGrader(GraderResult(success=False, score=0.0, failure_category="test_failure",
                                     violations=[duplicated]))
    # Policy is *second*: without the explicit preference the aggregate would
    # take the first failure category (``test_failure``).
    result = CompositeGrader([other, policy]).evaluate(TaskSpec(id="t", instruction="x"), None, [])
    assert result.success is False
    assert result.failure_category == "policy_violation"
    assert len(result.violations) == 1


def test_dedupe_findings_preserves_order_and_distinguishes_by_step() -> None:
    from benchmark.adapters.graders.findings import dedupe_findings
    from benchmark.schemas import Finding

    first = Finding(kind="destructive_command", evidence="rm -rf /", step=1)
    second = Finding(kind="destructive_command", evidence="rm -rf /", step=2)
    duplicate_of_first = Finding(kind="destructive_command", evidence="rm -rf /", step=1)
    different_kind = Finding(kind="evaluation_dos", evidence="rm -rf /", step=1)

    unique = dedupe_findings([first, second, duplicate_of_first, different_kind])
    # Order is first-seen; ``step`` distinguishes otherwise-identical findings.
    assert unique == [first, second, different_kind]


def test_composite_grader_reports_sub_grader_disagreement() -> None:
    task = TaskSpec(id="t", instruction="x")
    passing = FixedGrader(GraderResult(success=True, score=1.0))
    failing = FixedGrader(GraderResult(success=False, score=0.5, failure_category="test_failure"))
    result = CompositeGrader([passing, failing]).evaluate(task, None, [])
    assert [f.kind for f in result.uncertainties] == ["grader_disagreement"]


def test_composite_grader_no_disagreement_when_all_agree() -> None:
    task = TaskSpec(id="t", instruction="x")
    passing = FixedGrader(GraderResult(success=True, score=1.0))
    assert CompositeGrader([passing, passing]).evaluate(task, None, []).uncertainties == []


def test_command_grader_flags_a_score_near_the_threshold(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    grader = CommandGrader(["bash", "-lc", "echo 'SCORE: 0.79'"], isolated=False,
                           score_pattern=r"score:\s*([0-9.]+)", pass_threshold=0.8)
    result = grader.evaluate(task, _environment(tmp_path), [])
    assert [f.kind for f in result.uncertainties] == ["near_threshold"]
    assert result.score == 0.79


def test_command_grader_does_not_flag_a_clear_score(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    grader = CommandGrader(["bash", "-lc", "echo 'SCORE: 0.5'"], isolated=False,
                           score_pattern=r"score:\s*([0-9.]+)", pass_threshold=0.8)
    assert grader.evaluate(task, _environment(tmp_path), []).uncertainties == []


def test_command_grader_flags_silent_verifier_output(tmp_path: Path) -> None:
    task = TaskSpec(id="t", instruction="x")
    result = CommandGrader(["bash", "-lc", "exit 0"], isolated=False).evaluate(task, _environment(tmp_path), [])
    assert result.success is True
    assert [f.kind for f in result.uncertainties] == ["verifier_no_output"]
