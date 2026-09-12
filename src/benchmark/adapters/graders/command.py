from __future__ import annotations

import re

from ...interfaces import Environment
from ...schemas import Finding, GraderResult, TaskSpec, TraceEvent

# Scores this close to the pass threshold are reported as uncertain: a small
# environment difference could flip the verdict either way.
NEAR_THRESHOLD_EPSILON = 0.02


class CommandGrader:
    """Run the verifier outside the agent's writable execution context.

    By default the verifier is pass/fail (exit code 0 or not). Configure
    ``score_pattern`` with a regular expression that has one numeric capture
    group (e.g. ``r"SCORE:\\s*([0-9.]+)"``) to let the verifier award partial
    credit: the captured value is clamped to ``[0, 1]`` and success requires a
    zero exit code *and* ``score >= pass_threshold``.
    """

    def __init__(
        self,
        command: list[str],
        *,
        isolated: bool = True,
        score_pattern: str | None = None,
        pass_threshold: float = 1.0,
    ) -> None:
        if not 0.0 <= pass_threshold <= 1.0:
            raise ValueError("pass_threshold must be within [0, 1]")
        self.command = command
        self.isolated = isolated
        self.score_pattern = score_pattern
        self.pass_threshold = pass_threshold
        self._score_regex = re.compile(score_pattern, re.IGNORECASE) if score_pattern else None
        if self._score_regex is not None and self._score_regex.groups < 1:
            raise ValueError("score_pattern must contain a capturing group for the score")

    def evaluate(self, task: TaskSpec, environment: Environment, events: list[TraceEvent]) -> GraderResult:
        del task, events
        try:
            runner = getattr(environment, "verify_command", None) if self.isolated else None
            if runner is None:
                runner = environment.run_command  # type: ignore[attr-defined]
            completed = runner(self.command)
        except Exception as exc:
            return GraderResult(success=False, failure_category="verifier_error", evidence=[str(exc)])
        output = completed.stdout.strip() or completed.stderr.strip() or "verifier produced no output"
        silent = not completed.stdout.strip() and not completed.stderr.strip()
        uncertainties = (
            [Finding(kind="verifier_no_output", severity="low", source="grader",
                     evidence="verifier produced no stdout or stderr")]
            if silent else []
        )
        if self._score_regex is None:
            if completed.returncode == 0:
                return GraderResult(success=True, score=1.0, evidence=[output], uncertainties=uncertainties)
            return GraderResult(success=False, score=0.0, failure_category="test_failure",
                                evidence=[output], uncertainties=uncertainties)

        score = self._parse_score(completed.stdout, completed.stderr)
        if score is None:
            return GraderResult(
                success=False, score=0.0, failure_category="verifier_error",
                evidence=[f"{output} (no score matched {self.score_pattern!r})"],
                uncertainties=uncertainties,
            )
        if abs(score - self.pass_threshold) < NEAR_THRESHOLD_EPSILON:
            uncertainties = uncertainties + [Finding(
                kind="near_threshold", severity="low", source="grader",
                evidence=f"score {score:g} is within {NEAR_THRESHOLD_EPSILON:g} of threshold {self.pass_threshold:g}",
            )]
        success = completed.returncode == 0 and score >= self.pass_threshold
        return GraderResult(
            success=success,
            score=score,
            failure_category=None if success else "test_failure",
            evidence=[f"{output} (score={score:g}, threshold={self.pass_threshold:g})"],
            uncertainties=uncertainties,
        )

    def _parse_score(self, stdout: str, stderr: str) -> float | None:
        match = self._score_regex.search(f"{stdout}\n{stderr}") if self._score_regex else None
        if match is None:
            return None
        try:
            return min(1.0, max(0.0, float(match.group(1))))
        except (ValueError, IndexError):
            return None
