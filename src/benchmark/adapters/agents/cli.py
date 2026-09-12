"""Generic adapter that runs a coding CLI as a black box.

The harness has two built-in ways to act: the deterministic ``scripted`` agent
and the in-repo ``LLMAdapter`` tool loop. This adapter is the third: it shells
out to an external coding CLI so a task set can measure a real agent instead of
the harness's own loop. Nothing about the CLI is special-cased here — the caller
supplies the command and the argument template.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from ...schemas import AgentResult

PROMPT_MODES = ("argv", "stdin", "prompt_file")
PREVIOUS_OUTPUT_HEADER = "\n\nPrevious output:\n"


def resolve_cli_command(command: str) -> str:
    """Resolve a bare command on ``PATH``; an absolute path is used as given.

    An unresolvable command is returned unchanged: the launch then fails inside
    ``act()`` as an ``AgentResult``, which is a result the harness can retry,
    rather than a construction-time exception.
    """
    if os.path.isabs(command):
        return command
    return shutil.which(command) or command


class CLIAgent:
    """One-shot (by default) adapter for an external coding CLI.

    The CLI is invoked directly by this adapter, not through the environment, so
    all ``max_steps`` invocations happen inside a single ``act()`` call and the
    harness sees exactly one terminal action. ``max_steps > 1`` re-launches the
    CLI with the previous run's output appended to the instruction, for tools
    that need more than one invocation.

    The working directory defaults to the ``cwd`` carried on the observation —
    the environment delivers its per-trial workspace there — so a task's files
    are visible without plumbing the workspace through ``build_agent``. An
    explicit ``cli.cwd`` wins over the observation.

    The CLI runs on the host, so this adapter requires ``environment_type:
    local``; container-delegated execution is not implemented yet.

    A broken CLI never crashes the harness: a missing binary, an unexpected exit
    code, and a timeout all become ``AgentResult(status="error")``.
    """

    agent_id = "cli"

    def __init__(
        self,
        instruction: str,
        *,
        command: str,
        args: list[str] | None = None,
        prompt_mode: str = "prompt_file",
        prompt_flag: str | None = None,
        timeout: float = 600,
        max_steps: int = 1,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        success_exit_codes: Sequence[int] = (0,),
        max_output_chars: int = 4000,
    ) -> None:
        if not command or not str(command).strip():
            raise ValueError("command is required")
        if prompt_mode not in PROMPT_MODES:
            raise ValueError(f"unsupported prompt_mode: {prompt_mode!r}; expected one of {PROMPT_MODES}")
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.instruction = instruction
        self.command = resolve_cli_command(str(command))
        # ``args`` is a template list: ``{instruction}`` / ``{prompt_file}`` are
        # substituted per ``prompt_mode``. Items are stringified so a YAML config
        # with numeric arguments (ports, retries) still launches.
        self.args = [str(item) for item in (args or [])]
        self.prompt_mode = prompt_mode
        # A leading flag for the CLI's headless/print mode, e.g. ``claude -p``.
        self.prompt_flag = prompt_flag
        self.timeout = timeout
        self.max_steps = max_steps
        # An explicit cwd is stringified (a YAML int must not reach subprocess as
        # a TypeError); when unset, ``act()`` uses the observation's cwd.
        self.cwd = str(cwd) if cwd is not None else None
        # subprocess requires a ``str -> str`` environment; coerce here so a
        # YAML value can never turn a launch into a TypeError.
        self.env = {str(key): str(value) for key, value in env.items()} if env else None
        self.success_exit_codes = tuple(success_exit_codes)
        self.max_output_chars = max_output_chars
        self._steps = 0
        self._last_output: str | None = None
        self._result: AgentResult | None = None
        self._started = time.monotonic()

    def act(self, observation: dict[str, Any]) -> dict[str, Any]:
        # The environment reports the per-trial workspace here; an explicit
        # ``cli.cwd`` overrides it. Read before any launch and never mutated.
        cwd = self.cwd or _optional_str(observation.get("cwd"))
        while self._steps < self.max_steps:
            instruction = self.instruction
            if self._last_output:
                instruction += PREVIOUS_OUTPUT_HEADER + self._last_output
            status, error, output = self._launch(instruction, cwd)
            self._steps += 1
            self._last_output = output
            self._result = AgentResult(
                status=status,
                final_message=output,
                error=error,
                steps=self._steps,
                duration_seconds=time.monotonic() - self._started,
            )
        return {"type": "finish"}

    def _launch(self, instruction: str, cwd: str | None) -> tuple[Literal["completed", "error"], str | None, str]:
        prompt_path: str | None = None
        try:
            args, stdin_text, prompt_path = self._prepare(instruction)
            env = self._merged_env()
            if stdin_text is None:
                completed = subprocess.run(
                    [self.command, *args], cwd=cwd, env=env, timeout=self.timeout,
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
                )
            else:
                completed = subprocess.run(
                    [self.command, *args], cwd=cwd, env=env, timeout=self.timeout,
                    capture_output=True, text=True, input=stdin_text, check=False,
                )
        except subprocess.TimeoutExpired as exc:
            output = _tail(_combine(_text(exc.stdout), _text(exc.stderr)), self.max_output_chars)
            return "error", f"CLI timed out after {self.timeout:g}s", output
        except OSError as exc:
            # Missing/executable-broken binary: a result the harness can retry.
            return "error", f"failed to launch {self.command!r}: {type(exc).__name__}", ""
        finally:
            if prompt_path is not None:
                Path(prompt_path).unlink(missing_ok=True)
        output = _tail(_combine(completed.stdout, completed.stderr), self.max_output_chars)
        if completed.returncode in self.success_exit_codes:
            return "completed", None, output
        return "error", f"CLI exited with code {completed.returncode}", output

    def _prepare(self, instruction: str) -> tuple[list[str], str | None, str | None]:
        """Render the argv and (for ``stdin``) the text to pipe in.

        Returns the rendered args, the stdin text, and the prompt-file path to
        clean up afterwards. Only one of the latter two is ever set.
        """
        args = list(self.args)
        stdin_text: str | None = None
        prompt_path: str | None = None
        if self.prompt_mode == "prompt_file":
            # The prompt file is the safest long-prompt channel and the one CLI
            # tools expect; it lives only for the duration of the process.
            handle = tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False)
            try:
                handle.write(instruction)
            finally:
                handle.close()
            prompt_path = handle.name
            args = [item.replace("{prompt_file}", prompt_path) for item in args]
        elif self.prompt_mode == "argv":
            args = [item.replace("{instruction}", instruction) for item in args]
        else:
            stdin_text = instruction
        if self.prompt_flag:
            args = [self.prompt_flag, *args]
        return args, stdin_text, prompt_path

    def _merged_env(self) -> dict[str, str] | None:
        if self.env is None:
            return None
        return {**os.environ, **self.env}

    def finished(self) -> bool:
        return self._result is not None and self._steps >= self.max_steps

    def result(self) -> AgentResult:
        if self._result is None:
            return AgentResult(status="error", error="CLI agent did not run", steps=self._steps)
        return self._result


def _combine(stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stdout, stderr) if part)


def _tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text[-limit:]


def _optional_str(value: Any) -> str | None:
    """Coerce an observation-supplied path to ``str`` without ever raising."""
    if value is None:
        return None
    return str(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)
