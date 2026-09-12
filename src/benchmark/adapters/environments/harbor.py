"""Optional Harbor environment adapter scaffold."""
from __future__ import annotations

from typing import Any

from ...schemas import TaskSpec
from ..errors import unavailable

_NEEDS = "a Harbor SDK client that resets a task environment and executes its commands"


class HarborEnvironment:
    """Integration point for Harbor task environments.

    Harbor is intentionally not bundled; this scaffold refuses to run until its
    SDK is installed and a concrete bridge is implemented.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        unavailable("harbor", feature="HarborEnvironment", needs=_NEEDS)

    def reset(self, task: TaskSpec) -> None:
        unavailable("harbor", feature="HarborEnvironment", needs=_NEEDS)
