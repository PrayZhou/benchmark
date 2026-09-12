"""Optional BrowserGym environment adapter scaffold."""
from __future__ import annotations

from typing import Any

from ...schemas import TaskSpec
from ..errors import unavailable

_NEEDS = "a BrowserGym observation/action bridge"


class BrowserGymEnvironment:
    """Explicit, honest placeholder for BrowserGym integration."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        unavailable("browsergym", feature="BrowserGymEnvironment", needs=_NEEDS)

    def reset(self, task: TaskSpec) -> None:
        unavailable("browsergym", feature="BrowserGymEnvironment", needs=_NEEDS)
