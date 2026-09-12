from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ...schemas import TaskSpec
from .local import TIMEOUT_RETURNCODE, CompletedCommand


def resolve_docker_binary(configured: str | None = None) -> str | None:
    """Find a usable docker CLI without assuming it is on PATH."""
    if configured:
        return configured
    env = os.getenv("BENCHMARK_DOCKER_BIN")
    if env:
        return env
    found = shutil.which("docker")
    if found:
        return found
    for candidate in (
        "/Applications/Docker.app/Contents/Resources/bin/docker",
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


class DockerEnvironment:
    """Ephemeral Docker-backed terminal environment.

    The task workspace is mounted at ``/workspace``. Commands run in a
    non-root container with networking disabled by default. Docker is invoked
    through its CLI so the core package has no Docker SDK dependency.
    """

    def __init__(
        self,
        root: Path,
        image: str,
        network: str = "none",
        *,
        cpu_limit: float = 1.0,
        memory_limit: str = "512m",
        pids_limit: int = 128,
        user: str = "10001:10001",
        command_timeout: float = 30.0,
        docker_binary: str | None = None,
    ) -> None:
        self.root = root.resolve()
        self.image = image
        self.network = network
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.pids_limit = pids_limit
        self.user = user
        self.command_timeout = command_timeout
        self.docker_binary = resolve_docker_binary(docker_binary)
        self.container_name = f"agent-benchmark-{uuid.uuid4().hex[:12]}"
        self._state: dict[str, Any] = {}
        self._started = False
        self.seed: int = 0

    def preflight(self) -> None:
        """Fail fast and actionably when no usable Docker daemon is reachable."""
        try:
            self._docker(["version", "--format", "{{.Server.Version}}"], timeout=10)
        except (RuntimeError, TimeoutError) as exc:
            raise RuntimeError(
                f"Docker preflight failed; point at a working CLI with BENCHMARK_DOCKER_BIN "
                f"(resolved binary: {self.docker_binary!r}) and start the daemon: {exc}"
            ) from exc

    def reset(self, task: TaskSpec) -> None:
        self.preflight()
        self.cleanup()
        self.seed = task.seed
        self.root.mkdir(parents=True, exist_ok=True)
        command = [
            "run", "-d", "--name", self.container_name,
            "--network", self.network,
            "--user", self.user,
            "--cpus", str(self.cpu_limit),
            "--memory", self.memory_limit,
            "--pids-limit", str(self.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-e", f"BENCHMARK_SEED={self.seed}",
            "-e", f"PYTHONHASHSEED={self.seed}",
            "-v", f"{self.root}:/workspace:rw",
            "-w", "/workspace",
            self.image, "sleep", "infinity",
        ]
        self._docker(command, timeout=30)
        self._started = True
        self._state = {"task_id": task.id, "completed": False, "seed": task.seed}

    def observe(self) -> dict[str, Any]:
        return {"state": dict(self._state), "cwd": "/workspace", "container": self.container_name, "seed": self.seed}

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
        return {"ok": completed.returncode == 0, "returncode": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr}

    def run_command(self, command: list[str], timeout: float | None = None) -> CompletedCommand:
        if not self._started:
            raise RuntimeError("Docker environment has not been reset")
        try:
            completed = self._docker(["exec", self.container_name, *command],
                                     timeout=self._effective_timeout(timeout), check=False)
        except TimeoutError as exc:
            return CompletedCommand(TIMEOUT_RETURNCODE, "", str(exc))
        return CompletedCommand(completed.returncode, completed.stdout, completed.stderr)

    def verify_command(self, command: list[str], timeout: float | None = None) -> CompletedCommand:
        """Run a verifier in a separate, read-only container."""
        if not self._started:
            raise RuntimeError("Docker environment has not been reset")
        verifier_name = f"{self.container_name}-verifier"
        args = [
            "run", "--rm", "--name", verifier_name,
            "--network", "none", "--user", self.user,
            "--cpus", str(self.cpu_limit), "--memory", self.memory_limit,
            "--pids-limit", str(self.pids_limit), "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{self.root}:/workspace:ro", "-w", "/workspace",
            self.image, *command,
        ]
        try:
            completed = self._docker(args, timeout=self._effective_timeout(timeout), check=False)
        except TimeoutError as exc:
            return CompletedCommand(TIMEOUT_RETURNCODE, "", str(exc))
        return CompletedCommand(completed.returncode, completed.stdout, completed.stderr)

    def _effective_timeout(self, timeout: float | None) -> float:
        """Cap the command at its own timeout and at any tighter trial budget."""
        return self.command_timeout if timeout is None else min(self.command_timeout, timeout)

    def terminal(self) -> bool:
        return bool(self._state.get("completed"))

    def artifacts(self) -> list[str]:
        return [str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()]

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def cleanup(self) -> None:
        if self._started and self.docker_binary is not None:
            # Best-effort teardown: a failed ``rm`` must not mask the trial's
            # real result, so this never raises.
            subprocess.run([self.docker_binary, "rm", "-f", self.container_name],
                           capture_output=True, text=True, check=False)
        self._started = False

    def _docker(self, args: list[str], *, timeout: float, check: bool = True) -> subprocess.CompletedProcess[str]:
        binary = self.docker_binary
        if binary is None:
            raise RuntimeError("Docker CLI not found; set BENCHMARK_DOCKER_BIN or pass docker_binary")
        try:
            completed = subprocess.run([binary, *args], capture_output=True, text=True,
                                       timeout=timeout, check=False)
            if check and completed.returncode != 0:
                raise subprocess.CalledProcessError(completed.returncode, [binary, *args],
                                                     output=completed.stdout, stderr=completed.stderr)
            return completed
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Docker CLI not found at {binary!r}; set BENCHMARK_DOCKER_BIN or pass docker_binary"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Docker command timed out after {timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "docker command failed").strip()
            raise RuntimeError(detail) from exc
