from __future__ import annotations

import json
import os
import shutil as _shutil
import subprocess as _subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

from benchmark.adapters.environments.docker import DockerEnvironment, resolve_docker_binary
from benchmark.schemas import TaskSpec


def test_resolve_docker_binary_prefers_explicit_and_env(monkeypatch) -> None:
    assert resolve_docker_binary("/usr/bin/true") == "/usr/bin/true"
    monkeypatch.setenv("BENCHMARK_DOCKER_BIN", "/usr/bin/true")
    assert resolve_docker_binary() == "/usr/bin/true"
    monkeypatch.delenv("BENCHMARK_DOCKER_BIN")
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("os.path.exists", lambda path: False)
    assert resolve_docker_binary() is None


def test_reset_argv_pins_security_flags(tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(args, *, timeout, check=True):
        recorded.append(list(args))
        return type("R", (), {"returncode": 0, "stdout": "x", "stderr": ""})()

    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary="/usr/bin/true")
    environment._docker = fake_run  # type: ignore[method-assign]
    environment.reset(TaskSpec(id="t", instruction="x"))
    run = next(args for args in recorded if args[0] == "run")
    for flag in ("--network", "none", "--user", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"):
        assert flag in run
    _assert_security_flags(run)
    assert f"{tmp_path.resolve()}:/workspace:rw" in run


def test_verify_command_mounts_read_only(tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    def fake_run(args, *, timeout, check=True):
        recorded.append(list(args))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary="/usr/bin/true")
    environment._docker = fake_run  # type: ignore[method-assign]
    environment.reset(TaskSpec(id="t", instruction="x"))
    environment.verify_command(["true"])
    verify = next(args for args in recorded if args[0] == "run" and "--rm" in args)
    _assert_security_flags(verify)
    assert f"{tmp_path.resolve()}:/workspace:ro" in verify
    assert "--network" in verify and verify[verify.index("--network") + 1] == "none"


def test_reset_reports_a_missing_daemon_actionably(tmp_path: Path, monkeypatch) -> None:
    # On this host the Docker Desktop fallback path exists, so a real resolve
    # would succeed. Force the "no binary at all" case explicitly.
    monkeypatch.setattr("benchmark.adapters.environments.docker.resolve_docker_binary", lambda *a, **k: None)
    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary=None)
    with pytest.raises(RuntimeError, match="Docker"):
        environment.reset(TaskSpec(id="t", instruction="x"))


def test_preflight_wraps_a_non_executable_binary_actionably(tmp_path: Path) -> None:
    # A resolved-but-non-executable path raises PermissionError (an OSError), not
    # FileNotFoundError. It must surface as preflight's actionable RuntimeError
    # rather than leak a raw OS error to the caller.
    binary = tmp_path / "docker-not-executable"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")  # deliberately no execute bit
    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary=str(binary))

    with pytest.raises(RuntimeError, match="Docker preflight failed"):
        environment.preflight()


def _daemon_available() -> bool:
    binary = resolve_docker_binary()
    if not binary:
        return False
    try:
        done = _subprocess.run([binary, "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=10, check=False)
    except (OSError, _subprocess.SubprocessError):
        return False
    return done.returncode == 0


@pytest.fixture
def docker_image() -> str:
    if not _daemon_available():
        pytest.skip("docker daemon not available")
    image = os.getenv("BENCHMARK_TEST_IMAGE", "python:3.12-slim")
    if not _image_available(image):
        pytest.skip(f"base image unavailable: {image}")
    return image


def _pull_env(binary: str) -> dict[str, str]:
    """Environment whose PATH includes the docker CLI's own directory.

    Docker Desktop keeps ``docker-credential-desktop`` next to the CLI, which
    is absent from PATH on a default install; a pull cannot authenticate
    without it. The resolver only checks that the binary exists, so confirm it
    is executable before trusting its directory.
    """
    env = dict(os.environ)
    bin_dir = os.path.dirname(binary)
    if bin_dir and _shutil.which(os.path.basename(binary), path=bin_dir):
        env["PATH"] = os.pathsep.join([bin_dir, env.get("PATH", "")])
    return env


@cache
def _image_available(image: str) -> bool:
    """Inspect the base image, pulling it at most once per session.

    A missing image, a failed pull, and a pull that hangs until its timeout
    are all reported as unavailable so the caller can skip rather than fail
    when the host is offline.
    """
    binary = resolve_docker_binary()
    if not binary:
        return False
    try:
        if _subprocess.run([binary, "image", "inspect", image],
                           capture_output=True, timeout=30, check=False).returncode == 0:
            return True
        pulled = _subprocess.run([binary, "pull", image], capture_output=True,
                                 timeout=60, check=False, env=_pull_env(binary))
    except (OSError, _subprocess.SubprocessError):
        return False
    return pulled.returncode == 0


def test_image_available_treats_a_hung_pull_as_unavailable(monkeypatch) -> None:
    # A firewall that drops packets makes ``docker pull`` hit its timeout
    # instead of failing fast. That must read as "unavailable" (so the caller
    # skips), never as an exception that reddens the suite. No container starts.
    monkeypatch.setattr(sys.modules[__name__], "resolve_docker_binary", lambda *a, **k: "/usr/bin/docker")
    calls: list[list[str]] = []
    pull_timeouts: list[float | None] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if "pull" in args:
            pull_timeouts.append(kwargs.get("timeout"))
            raise _subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))
        return _subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(_subprocess, "run", fake_run)
    _image_available.cache_clear()
    try:
        assert _image_available("python:3.12-slim") is False
    finally:
        _image_available.cache_clear()
    assert calls[0][1:3] == ["image", "inspect"]
    assert calls[1][1] == "pull"
    # A firewall that silently drops packets must not stall the suite for
    # minutes: the pull timeout is bounded well below the old 300s.
    assert pull_timeouts and pull_timeouts[0] is not None and pull_timeouts[0] <= 60


@pytest.mark.docker_integration
def test_container_runs_commands_and_verifier_is_read_only(tmp_path: Path, docker_image: str) -> None:
    # pytest creates mode-0700 directories. Let the container's non-root user
    # traverse this dedicated workspace; make the marker writable so a denied
    # verifier write proves the mount is read-only, not merely a DAC denial.
    tmp_path.chmod(0o755)
    marker = tmp_path / "marker.txt"
    marker.write_text("READY")
    marker.chmod(0o666)
    environment = DockerEnvironment(tmp_path, image=docker_image, command_timeout=30)
    environment.reset(TaskSpec(id="t", instruction="x", seed=7))
    try:
        inspected = environment._docker(["inspect", environment.container_name], timeout=30)
        config = json.loads(inspected.stdout)[0]
        host = config["HostConfig"]
        assert config["Config"]["User"] == "10001:10001"
        assert host["NetworkMode"] == "none"
        assert "ALL" in host["CapDrop"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["NanoCpus"] == 1_000_000_000
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["PidsLimit"] == 128

        writable = environment.execute({"type": "command", "command": ["sh", "-c", "printf READY > marker.txt"]})
        assert writable["returncode"] == 0, writable
        ok = environment.execute({"type": "command", "command": ["sh", "-c", "printf READY"]})
        assert ok["returncode"] == 0 and ok["stdout"] == "READY"

        bad = environment.execute({"type": "command", "command": ["sh", "-c", "exit 3"]})
        assert bad["returncode"] == 3 and bad["ok"] is False

        verified = environment.verify_command(["sh", "-c", "cat marker.txt"])
        assert verified.returncode == 0 and verified.stdout.strip() == "READY", verified

        denied = environment.verify_command(["sh", "-c", "printf x >> marker.txt"])
        assert denied.returncode != 0, "verifier must not be able to write the workspace"
        assert "Read-only file system" in denied.stderr, denied
        assert marker.read_text() == "READY"
        verifier_gone = environment._docker(
            ["container", "ls", "-aq", "--filter", f"name=^{environment.container_name}-verifier$"],
            timeout=30,
        )
        assert not verifier_gone.stdout.strip(), "verifier container must be removed"
    finally:
        environment.cleanup()

    # The spec requires cleanup to remove the container, not just stop it.
    gone = environment._docker(
        ["container", "ls", "-aq", "--filter", f"name=^{environment.container_name}$"], timeout=30,
    )
    assert not gone.stdout.strip(), "cleanup must remove the container"
    environment.cleanup()  # teardown is idempotent


def _assert_security_flags(args: list[str]) -> None:
    for flag, value in {
        "--network": "none", "--user": "10001:10001", "--cpus": "1.0",
        "--memory": "512m", "--pids-limit": "128", "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
    }.items():
        assert args[args.index(flag) + 1] == value
