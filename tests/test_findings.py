from __future__ import annotations

import pydantic
import pytest

from benchmark.schemas import Finding, GraderResult, TrialResult


def test_finding_defaults_and_round_trip() -> None:
    finding = Finding(kind="verifier_tampering", evidence="tests/test.sh modified")
    assert finding.severity == "medium"
    assert finding.source == "grader"
    assert finding.step is None
    payload = finding.model_dump(mode="json")
    assert Finding.model_validate(payload) == finding


def test_finding_rejects_unknown_severity_and_source() -> None:
    with pytest.raises(pydantic.ValidationError):
        Finding(kind="x", evidence="y", severity="critical")
    with pytest.raises(pydantic.ValidationError):
        Finding(kind="x", evidence="y", source="environment")


def test_grader_and_trial_results_default_to_empty_findings() -> None:
    grader = GraderResult(success=True)
    assert grader.violations == [] and grader.uncertainties == []
    trial = TrialResult(task_id="t", trial_id="1", agent_id="a", seed=0, status="completed")
    assert trial.violations == [] and trial.uncertainties == []


def test_grader_result_accepts_findings() -> None:
    grader = GraderResult(
        success=False,
        violations=[Finding(kind="destructive_command", severity="high", source="policy", evidence="rm -rf /")],
        uncertainties=[Finding(kind="near_threshold", source="grader", evidence="score 0.79")],
    )
    assert grader.violations[0].kind == "destructive_command"
    assert grader.uncertainties[0].severity == "medium"
