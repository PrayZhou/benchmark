from __future__ import annotations

import json

import pytest

from benchmark.adapters.graders.judge import JudgeGrader
from benchmark.factories import build_grader
from benchmark.schemas import TaskSpec, TraceEvent


class _Backend:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def complete(self, messages, tools, *, model, max_output_tokens, timeout, seed=None):
        self.calls += 1
        return {"choices": [{"message": {"content": self.content}}], "usage": {}}


class _BoomBackend:
    def complete(self, *args, **kwargs):
        raise RuntimeError("gateway exploded")


def _events() -> list[TraceEvent]:
    return [TraceEvent(type="action", step=0, payload={"type": "command", "command": ["ls"]})]


def test_judge_reports_findings_from_its_backend() -> None:
    backend = _Backend(json.dumps({"findings": [
        {"kind": "spec_gaming", "severity": "high", "evidence": "searched for the answer key", "step": 0},
    ], "confident": True}))
    grader = JudgeGrader(model="m", backend=backend)
    result = grader.evaluate(TaskSpec(id="t", instruction="x"), None, _events())
    assert result.success is False
    assert result.failure_category == "policy_violation"
    assert [f.kind for f in result.violations] == ["spec_gaming"]
    assert all(f.source == "judge" for f in result.violations)
    assert backend.calls == 1


def test_judge_low_confidence_is_only_an_uncertainty() -> None:
    backend = _Backend(json.dumps({"findings": [], "confident": False}))
    grader = JudgeGrader(model="m", backend=backend)
    result = grader.evaluate(TaskSpec(id="t", instruction="x"), None, _events())
    assert result.success is True
    assert [f.kind for f in result.uncertainties] == ["judge_low_confidence"]


def test_judge_malformed_output_degrades_to_uncertainty() -> None:
    grader = JudgeGrader(model="m", backend=_Backend("not json at all"))
    result = grader.evaluate(TaskSpec(id="t", instruction="x"), None, _events())
    assert result.success is True
    assert [f.kind for f in result.uncertainties] == ["judge_error"]


def test_judge_backend_failure_degrades_to_uncertainty() -> None:
    grader = JudgeGrader(model="m", backend=_BoomBackend())
    result = grader.evaluate(TaskSpec(id="t", instruction="x"), None, _events())
    assert result.success is True
    assert [f.kind for f in result.uncertainties] == ["judge_unavailable"]


def test_judge_requires_a_model() -> None:
    with pytest.raises(ValueError, match="model"):
        JudgeGrader(model="")


def test_factory_routes_judge_grader() -> None:
    task = TaskSpec(id="t", instruction="x", grader_type="judge", grader={"model": "gpt-x"})
    assert isinstance(build_grader(task), JudgeGrader)


def test_judge_unhashable_severity_defaults_instead_of_raising() -> None:
    # A JSON list is unhashable: ``severity not in SEVERITY_ORDER`` would raise
    # TypeError, escape ``evaluate``, and turn into a retryable runner_error.
    backend = _Backend(json.dumps({"findings": [
        {"kind": "x", "severity": ["high"], "evidence": "e"},
    ], "confident": True}))
    grader = JudgeGrader(model="m", backend=backend, fail_closed=False)
    result = grader.evaluate(TaskSpec(id="t", instruction="x"), None, _events())
    assert result.success is True
    assert [f.severity for f in result.violations] == ["medium"]


def test_judge_dict_severity_defaults_instead_of_raising() -> None:
    backend = _Backend(json.dumps({"findings": [
        {"kind": "x", "severity": {}, "evidence": "e"},
    ], "confident": True}))
    grader = JudgeGrader(model="m", backend=backend, fail_closed=False)
    result = grader.evaluate(TaskSpec(id="t", instruction="x"), None, _events())
    assert result.success is True
    assert [f.severity for f in result.violations] == ["medium"]


def test_judge_non_dict_finding_degrades_to_error() -> None:
    backend = _Backend(json.dumps({"findings": [["not", "a", "mapping"]], "confident": True}))
    grader = JudgeGrader(model="m", backend=backend)
    result = grader.evaluate(TaskSpec(id="t", instruction="x"), None, _events())
    assert result.success is True
    assert [f.kind for f in result.uncertainties] == ["judge_error"]
