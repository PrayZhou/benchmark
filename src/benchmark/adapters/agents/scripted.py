from __future__ import annotations

import time

from ...schemas import AgentResult


class ScriptedAgent:
    """Deterministic baseline used to validate the harness without an LLM."""

    agent_id = "scripted"

    def __init__(self, actions: list[dict]) -> None:
        self.actions = list(actions)
        self._steps = 0
        self._started = time.monotonic()
        self._result: AgentResult | None = None

    def act(self, observation: dict) -> dict:
        del observation
        if not self.actions:
            self._result = AgentResult(
                status="completed",
                final_message="scripted actions exhausted",
                steps=self._steps,
                tool_calls=self._steps,
                duration_seconds=time.monotonic() - self._started,
            )
            return {"type": "finish"}
        self._steps += 1
        return self.actions.pop(0)

    def finished(self) -> bool:
        return self._result is not None

    def result(self) -> AgentResult:
        if self._result is None:
            self._result = AgentResult(
                status="completed",
                final_message="scripted actions completed",
                steps=self._steps,
                tool_calls=self._steps,
                duration_seconds=time.monotonic() - self._started,
            )
        return self._result
