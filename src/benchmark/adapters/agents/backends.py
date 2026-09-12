"""LLM backend registry: provider name -> backend factory.

``ProviderConfig.provider`` selects a backend through :func:`resolve_backend`.
Only OpenAI-compatible backends exist today; an unknown name is rejected rather
than silently routed to the OpenAI-compatible path.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol


class LLMBackend(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str,
                 max_output_tokens: int, timeout: float, seed: int | None = None) -> Any: ...


class OpenAIBackend:
    """Thin SDK adapter. Importing this module does not require ``openai``."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY") -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("OpenAI backend requires the optional 'openai' package") from exc
        key = api_key or os.getenv(api_key_env)
        if not key:
            raise RuntimeError(f"{api_key_env} is not set")
        self.client = OpenAI(api_key=key, base_url=base_url, timeout=None, max_retries=0)

    def complete(self, messages, tools, *, model, max_output_tokens, timeout, seed=None):
        kwargs: dict[str, Any] = {}
        if seed is not None:
            kwargs["seed"] = seed
        return self.client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            max_tokens=max_output_tokens, timeout=timeout, **kwargs,
        )


BACKENDS: dict[str, Callable[..., LLMBackend]] = {
    "openai": OpenAIBackend,
    "openai-compatible": OpenAIBackend,
}
# ``cli.py`` uses "mock" as its "provider not configured" sentinel, so both it
# and an empty string fall back to the default backend instead of erroring.
UNSET_PROVIDERS = frozenset({"", "mock"})
DEFAULT_BACKEND = "openai-compatible"


def resolve_backend(name: str | None) -> tuple[str, Callable[..., LLMBackend]]:
    """Resolve a configured provider name to ``(canonical_name, factory)``."""
    normalized = (name or "").strip().lower()
    key = DEFAULT_BACKEND if normalized in UNSET_PROVIDERS else normalized
    if key not in BACKENDS:
        supported = ", ".join(sorted(BACKENDS))
        raise ValueError(f"unsupported provider {name!r}; supported providers: {supported}")
    return key, BACKENDS[key]
