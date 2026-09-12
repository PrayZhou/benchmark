"""Optional MCP agent adapter scaffold."""
from __future__ import annotations

from typing import Any

from ...schemas import AgentResult
from ..errors import unavailable

_NEEDS = "an MCP client that maps MCP tools to harness actions"


class MCPAgent:
    """MCP integration scaffold; never reports a fabricated successful run."""

    agent_id = "mcp"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        unavailable("mcp", feature="MCPAgent", needs=_NEEDS)

    def act(self, observation: dict[str, Any]) -> dict[str, Any]:
        unavailable("mcp", feature="MCPAgent", needs=_NEEDS)

    def finished(self) -> bool:
        return True

    def result(self) -> AgentResult:
        return AgentResult(status="error", error="MCPAgent is not implemented")
