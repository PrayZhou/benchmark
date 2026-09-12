from __future__ import annotations

from ...interfaces import Environment, Grader
from ...schemas import GraderResult, TaskSpec, TraceEvent


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
            failure_category = failures[0].failure_category if failures else "below_threshold"
        return GraderResult(
            success=success,
            score=score,
            evidence=[item for result in results for item in result.evidence],
            failure_category=failure_category,
            violations=[item for result in results for item in result.violations],
            uncertainties=[item for result in results for item in result.uncertainties],
        )

    def _score(self, results: list[GraderResult]) -> float:
        if not results:
            return 0.0
        if self.weights is None:
            return sum(result.score for result in results) / len(results)
        total = sum(self.weights)
        return sum(weight * result.score for weight, result in zip(self.weights, results, strict=True)) / total
