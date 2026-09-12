from __future__ import annotations

from ...interfaces import Environment
from ...schemas import GraderResult, TaskSpec, TraceEvent


class StateGrader:
    def __init__(self, expected: dict[str, object]) -> None:
        self.expected = expected

    def evaluate(self, task: TaskSpec, environment: Environment, events: list[TraceEvent]) -> GraderResult:
        del task, events
        state = environment.state()  # type: ignore[attr-defined]
        mismatches = [
            f"{key}: expected {value!r}, got {state.get(key)!r}"
            for key, value in self.expected.items()
            if state.get(key) != value
        ]
        if mismatches:
            return GraderResult(success=False, failure_category="state_mismatch", evidence=mismatches)
        return GraderResult(success=True, score=1.0, evidence=["expected state matched"])
