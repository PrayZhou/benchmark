from __future__ import annotations

from ...interfaces import Environment, Grader
from ...schemas import Finding, GraderResult, TaskSpec, TraceEvent
from .findings import dedupe_findings


class CompositeGrader:
    """Aggregate several graders, optionally weighted with a pass threshold.

    Defaults preserve the original all-must-pass semantics and a plain mean
    score. ``weights`` turns the score into a weighted mean; ``threshold``
    switches success to ``score >= threshold`` so partial credit can decide the
    outcome.
    """

    def __init__(self, graders: list[Grader], *, weights: list[float] | None = None,
                 threshold: float | None = None) -> None:
        if weights is not None:
            if len(weights) != len(graders):
                raise ValueError("weights must have one entry per grader")
            if any(weight < 0 for weight in weights):
                raise ValueError("weights must be non-negative")
            if sum(weights) <= 0:
                raise ValueError("at least one weight must be positive")
        if threshold is not None and not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be within [0, 1]")
        self.graders = graders
        self.weights = weights
        self.threshold = threshold

    def evaluate(self, task: TaskSpec, environment: Environment, events: list[TraceEvent]) -> GraderResult:
        results = [grader.evaluate(task, environment, events) for grader in self.graders]
        score = self._score(results)
        failures = [result for result in results if not result.success]
        if self.threshold is None:
            success = not failures
        else:
            success = score >= self.threshold
        failure_category = None
        if not success:
            categories = [result.failure_category for result in failures if result.failure_category]
            if "policy_violation" in categories:
                failure_category = "policy_violation"
            elif categories:
                failure_category = categories[0]
            else:
                failure_category = "below_threshold"
        violations = dedupe_findings([item for result in results for item in result.violations])
        uncertainties = dedupe_findings([item for result in results for item in result.uncertainties])
        if len({result.success for result in results}) > 1:
            uncertainties = dedupe_findings(uncertainties + [Finding(
                kind="grader_disagreement", severity="low", source="grader",
                evidence=f"sub-graders disagree on success: {[result.success for result in results]}",
            )])
        return GraderResult(
            success=success,
            score=score,
            evidence=[item for result in results for item in result.evidence],
            failure_category=failure_category,
            violations=violations,
            uncertainties=uncertainties,
        )

    def _score(self, results: list[GraderResult]) -> float:
        if not results:
            return 0.0
        if self.weights is None:
            return sum(result.score for result in results) / len(results)
        total = sum(self.weights)
        return sum(weight * result.score for weight, result in zip(self.weights, results, strict=True)) / total
