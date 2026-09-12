"""Optional multi-agent runtime adapter scaffold."""
from __future__ import annotations

from typing import Any

from ...schemas import AgentResult, TaskSpec
from ..errors import unavailable

_NEEDS = "a multi-agent runtime bridge"


class MultiAgentAdapter:
    """Fixed-topology multi-agent integration point.

    The runtime is optional and no completion is claimed until a real bridge is
    supplied.
    """

    agent_id = "multi-agent"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        package = kwargs.pop("package", "multi_agent")
        unavailable(package, feature="MultiAgentAdapter", needs=_NEEDS)

    @classmethod
    def from_task(cls, task: TaskSpec) -> MultiAgentAdapter:
        """Build the adapter from a task, forwarding the optional runtime package."""
        return cls(package=task.environment.get("package", "multi_agent"))

    def act(self, observation: dict[str, Any]) -> dict[str, Any]:
        unavailable("multi_agent", feature="MultiAgentAdapter", needs=_NEEDS)

    def finished(self) -> bool:
        return True

    def result(self) -> AgentResult:
        return AgentResult(status="error", error="MultiAgentAdapter is not implemented")
