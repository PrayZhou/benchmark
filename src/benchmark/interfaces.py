from __future__ import annotations

from typing import Any, Protocol

from .schemas import AgentResult, GraderResult, TaskSpec, TraceEvent


class TraceRecorder(Protocol):
    def record(self, event: TraceEvent) -> None: ...


class Environment(Protocol):
    def reset(self, task: TaskSpec) -> None: ...

    def observe(self) -> dict[str, Any]: ...

    def execute(self, action: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]: ...

    def terminal(self) -> bool: ...

    def artifacts(self) -> list[str]: ...

    def cleanup(self) -> None: ...


class AgentAdapter(Protocol):
    agent_id: str

    def act(self, observation: dict[str, Any]) -> dict[str, Any]: ...

    def finished(self) -> bool: ...

    def result(self) -> AgentResult: ...


class Grader(Protocol):
    def evaluate(
        self,
        task: TaskSpec,
        environment: Environment,
        events: list[TraceEvent],
    ) -> GraderResult: ...
