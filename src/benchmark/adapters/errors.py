"""Errors and helpers for optional adapter integrations."""
from __future__ import annotations

from typing import NoReturn


class OptionalDependencyError(RuntimeError):
    """Raised when an optional adapter dependency is not installed."""


def require_optional(package: str, *, feature: str | None = None) -> None:
    """Import-check an optional package without importing it at module import time."""
    try:
        __import__(package)
    except ImportError as exc:
        label = feature or package
        raise OptionalDependencyError(
            f"{label} requires the optional '{package}' dependency; "
            f"install it to use this adapter"
        ) from exc


def unavailable(package: str, *, feature: str, needs: str) -> NoReturn:
    """Refuse an optional adapter without fabricating a successful run.

    Distinguishes the two honest failure modes: a missing optional dependency
    (``OptionalDependencyError``) and an installed dependency whose bridge has
    not been written yet (``NotImplementedError``). ``needs`` names the bridge
    so the next implementer knows exactly what is missing.
    """
    require_optional(package, feature=feature)
    raise NotImplementedError(f"{feature} backend wiring is not implemented ({needs})")
