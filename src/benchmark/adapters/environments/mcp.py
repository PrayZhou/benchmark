"""Optional MCP environment adapter scaffold."""
from __future__ import annotations

from typing import Any

from ...schemas import TaskSpec
from ..errors import unavailable

_NEEDS = "an MCP server bridge that exposes tools through the harness action API"


class MCPEnvironment:
    """Explicit, honest placeholder for MCP server environments.

    The MCP runtime is optional and no successful run is claimed until a real
    tool-call bridge is supplied.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        unavailable("mcp", feature="MCPEnvironment", needs=_NEEDS)

    def reset(self, task: TaskSpec) -> None:
        unavailable("mcp", feature="MCPEnvironment", needs=_NEEDS)
