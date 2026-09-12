"""Provider-neutral tool-using LLM agent with a pluggable backend."""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ...schemas import AgentResult
from .backends import DEFAULT_BACKEND, LLMBackend, OpenAIBackend, resolve_backend

logger = logging.getLogger(__name__)

__all__ = ["LLMAdapter", "LLMBackend", "OpenAIBackend", "TOOLS"]


@dataclass
class _Call:
    call_id: str
    name: str
    arguments: dict[str, Any]


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Run a command in the task environment.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "array", "items": {"type": "string"}}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finish the task with a final message.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class LLMAdapter:
    agent_id = "llm"

    def __init__(self, instruction: str, *, model: str, backend: LLMBackend | None = None,
                 timeout: float = 60.0, max_output_tokens: int = 1024, max_observation_chars: int = 12000,
                 seed: int | None = None, pass_seed: bool = True, base_url: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", provider: str = DEFAULT_BACKEND,
                 backend_factory: Callable[..., LLMBackend] | None = None) -> None:
        if not model or not model.strip():
            raise ValueError("model is required")
        self.instruction = instruction
        self.model = model
        self.backend = backend
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.max_observation_chars = max_observation_chars
        self.seed = seed
        # Some OpenAI-compatible gateways reject the ``seed`` parameter outright
        # or intermittently; providers that cannot honour it must be able to opt out.
        self.pass_seed = pass_seed
        self.base_url = base_url
        self.api_key_env = api_key_env
        # The provider name is the one that actually selected ``backend_factory``;
        # construction stays lazy so no credential is needed until the first act().
        self.provider = provider
        self.backend_factory = backend_factory
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": instruction}]
        self._queue: deque[_Call] = deque()
        self._pending: _Call | None = None
        self._result: AgentResult | None = None
        self._steps = self._tool_calls = self._input = self._output = 0
        self._started = time.monotonic()
        self._last_error: str | None = None

    def _backend(self) -> LLMBackend:
        if self.backend is None:
            factory = self.backend_factory or resolve_backend(self.provider)[1]
            self.backend = factory(base_url=self.base_url, api_key_env=self.api_key_env)
        return self.backend

    def act(self, observation: dict[str, Any]) -> dict[str, Any]:
        if self._result is not None:
            return {"type": "finish"}
        self._steps += 1
        if self._pending is not None and "last_result" in observation:
            content = json.dumps(observation["last_result"], ensure_ascii=False)[: self.max_observation_chars]
            self.messages.append({"role": "tool", "tool_call_id": self._pending.call_id, "content": content})
            self._pending = None
        if self._queue:
            return self._emit(self._queue.popleft())
        obs = json.dumps(observation, ensure_ascii=False, default=str)[: self.max_observation_chars]
        self.messages.append({"role": "user", "content": "Observation:\n" + obs})
        try:
            response = self._backend().complete(self.messages, TOOLS, model=self.model,
                                                max_output_tokens=self.max_output_tokens, timeout=self.timeout,
                                                seed=self.seed if self.pass_seed else None)
            usage = _get(response, "usage", {}) or {}
            self._input += int(_get(usage, "prompt_tokens", _get(usage, "input_tokens", 0)) or 0)
            self._output += int(_get(usage, "completion_tokens", _get(usage, "output_tokens", 0)) or 0)
            choice = (_get(response, "choices", []) or [None])[0]
            message = _get(choice, "message", {}) or {}
            text = _get(message, "content")
            raw_calls = _get(message, "tool_calls", []) or []
            if raw_calls:
                self.messages.append({"role": "assistant", "content": text, "tool_calls": [
                    {"id": str(_get(raw, "id", "")), "type": "function", "function": {
                        "name": str(_get(_get(raw, "function", {}) or {}, "name", "")),
                        "arguments": str(_get(_get(raw, "function", {}) or {}, "arguments", "{}")),
                    }} for raw in raw_calls
                ]})
            for raw in raw_calls:
                fn = _get(raw, "function", {}) or {}
                name = _get(fn, "name")
                args = _get(fn, "arguments", {})
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                    self._queue.append(_Call(str(_get(raw, "id", f"call-{self._tool_calls}")), str(name), args))
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    self._last_error = f"malformed tool arguments: {exc}"
                    self._result = self._make_result("error", error=self._last_error)
                    return {"type": "finish"}
            if not self._queue:
                self._result = self._make_result("error", error="model returned no tool call")
                return {"type": "finish"}
            return self._emit(self._queue.popleft())
        except Exception as exc:
            self._last_error = f"LLM request failed: {type(exc).__name__}"
            # Type only: provider exceptions can echo the request and its key.
            logger.warning(
                "LLM request failed: %s", type(exc).__name__,
                extra={"event": "llm_error", "error_type": type(exc).__name__},
            )
            self._result = self._make_result("error", error=self._last_error)
            return {"type": "finish"}

    def _emit(self, call: _Call) -> dict[str, Any]:
        self._tool_calls += 1
        self._pending = call
        if call.name == "execute_command":
            command = call.arguments.get("command")
            if isinstance(command, list) and all(isinstance(x, str) for x in command):
                return {"type": "command", "command": command}
            self._result = self._make_result("error", error="malformed execute_command arguments")
            return {"type": "finish"}
        if call.name == "finish":
            self._result = self._make_result("completed", final_message=str(call.arguments.get("message", "")))
            return {"type": "finish"}
        self._result = self._make_result("error", error="unknown tool requested")
        return {"type": "finish"}

    def _make_result(self, status: Literal["completed", "timeout", "error"], **kwargs: Any) -> AgentResult:
        return AgentResult(status=status, steps=self._steps, tool_calls=self._tool_calls,
                           input_tokens=self._input, output_tokens=self._output,
                           duration_seconds=time.monotonic() - self._started, **kwargs)

    def finished(self) -> bool:
        return self._result is not None

    def result(self) -> AgentResult:
        return self._result or self._make_result("error", error="agent did not finish")
