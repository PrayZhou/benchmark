from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...schemas import TaskSpec

# Exit code returned when a command exceeds its timeout, mirroring GNU timeout.
TIMEOUT_RETURNCODE = 124

# Host environment variables forwarded to task commands. Everything else stays
# out: agent commands run untrusted code, so inheriting the whole environment
# would hand provider credentials (OPENAI_API_KEY, COMATE_API_KEY, ...) to the
# task. Tasks that genuinely need a variable must opt in via ``env``.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
    "SHELL", "VIRTUAL_ENV", "PYTHONPATH",
)


@dataclass
class CompletedCommand:
    returncode: int
    stdout: str
    stderr: str


class LocalEnvironment:
    """Small deterministic environment for the first harness smoke test.

    DockerEnvironment can replace this adapter without changing the runner API.
    """

    def __init__(
        self,
        root: Path,
        *,
        command_timeout: float = 30.0,
        env_allowlist: Sequence[str] | None = None,
        env: dict[str, Any] | None = None,
    ) -> None:
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        self.root = root
        self.command_timeout = command_timeout
        self.env_allowlist = tuple(env_allowlist) if env_allowlist is not None else DEFAULT_ENV_ALLOWLIST
        self.env = {str(key): str(value) for key, value in (env or {}).items()}
        self._state: dict[str, Any] = {}
        self.seed: int = 0

    def reset(self, task: TaskSpec) -> None:
        self.seed = task.seed
        self._state = {"task_id": task.id, "completed": False, "seed": task.seed}

    def observe(self) -> dict[str, Any]:
        return {"state": dict(self._state), "cwd": str(self.root), "seed": self.seed}

    def execute(self, action: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if action.get("type") == "finish":
            self._state["completed"] = True
            return {"ok": True, "type": "finish"}
        if action.get("type") != "command":
            return {"ok": False, "error": "unsupported action"}
        command = action.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            return {"ok": False, "error": "command must be a string list"}
        completed = self.run_command(command, timeout=timeout)
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def run_command(self, command: list[str], timeout: float | None = None) -> CompletedCommand:
        effective = self._effective_timeout(timeout)
        if effective <= 0:
            return CompletedCommand(TIMEOUT_RETURNCODE, "", "command budget exhausted before it started")
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=effective,
                check=False,
                env=self._command_env(),
            )
        except OSError as exc:
            # A command the agent got wrong (missing executable, bad argv) is a
            # tool result, not a harness failure: surface it so the agent can
            # correct course instead of crashing the whole trial.
            return CompletedCommand(127, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            # A timeout is also a tool result: the agent sees the budget ran out
            # and the runner can terminate on the next budget check.
            message = f"command timed out after {effective:g}s\n{_decode(exc.stderr)}".strip()
            return CompletedCommand(TIMEOUT_RETURNCODE, _decode(exc.stdout), message)
        return CompletedCommand(completed.returncode, completed.stdout, completed.stderr)

    def _effective_timeout(self, timeout: float | None) -> float:
        """Cap the command at its own timeout and at any tighter trial budget."""
        return self.command_timeout if timeout is None else min(self.command_timeout, timeout)

    def _command_env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in self.env_allowlist if name in os.environ}
        env.update(self.env)
        env["BENCHMARK_SEED"] = str(self.seed)
        env["PYTHONHASHSEED"] = str(self.seed)
        return env

    def terminal(self) -> bool:
        return bool(self._state.get("completed"))

    def artifacts(self) -> list[str]:
        return []

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def cleanup(self) -> None:
        pass


def _decode(value: Any) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)
