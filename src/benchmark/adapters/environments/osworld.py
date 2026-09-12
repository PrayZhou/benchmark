"""Optional OSWorld environment adapter scaffold."""
from __future__ import annotations

from typing import Any

from ...schemas import TaskSpec
from ..errors import unavailable

_NEEDS = "an OSWorld controller bridge"


class OSWorldEnvironment:
    """Explicit, honest placeholder for OSWorld integration."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        unavailable("osworld", feature="OSWorldEnvironment", needs=_NEEDS)

    def reset(self, task: TaskSpec) -> None:
        unavailable("osworld", feature="OSWorldEnvironment", needs=_NEEDS)
