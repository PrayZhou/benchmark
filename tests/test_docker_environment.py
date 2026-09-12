from __future__ import annotations

import os
import shutil as _shutil
import subprocess as _subprocess
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
    assert f"{tmp_path.resolve()}:/workspace:ro" in verify
    assert "--network" in verify and verify[verify.index("--network") + 1] == "none"


def test_reset_reports_a_missing_daemon_actionably(tmp_path: Path, monkeypatch) -> None:
    # On this host the Docker Desktop fallback path exists, so a real resolve
    # would succeed. Force the "no binary at all" case explicitly.
    monkeypatch.setattr("benchmark.adapters.environments.docker.resolve_docker_binary", lambda *a, **k: None)
    environment = DockerEnvironment(tmp_path, image="alpine:3", docker_binary=None)
    with pytest.raises(RuntimeError, match="Docker"):
        environment.reset(TaskSpec(id="t", instruction="x"))


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


integration = pytest.mark.skipif(not _daemon_available(), reason="docker daemon not available")


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

    A missing image and a failed pull are both reported as unavailable so the
    caller can skip rather than fail when the host is offline.
    """
    binary = resolve_docker_binary()
    if not binary:
        return False
    if _subprocess.run([binary, "image", "inspect", image],
                       capture_output=True, timeout=30, check=False).returncode == 0:
        return True
    return _subprocess.run([binary, "pull", image], capture_output=True,
                           timeout=300, check=False, env=_pull_env(binary)).returncode == 0


@integration
def test_container_runs_commands_and_verifier_is_read_only(tmp_path: Path) -> None:
    image = os.getenv("BENCHMARK_TEST_IMAGE", "python:3.12-slim")
    if not _image_available(image):
        pytest.skip("base image unavailable offline")
    environment = DockerEnvironment(tmp_path, image=image, command_timeout=30)
    environment.reset(TaskSpec(id="t", instruction="x", seed=7))
    try:
        ok = environment.execute({"type": "command", "command": ["sh", "-c", "printf READY"]})
        assert ok["returncode"] == 0 and ok["stdout"] == "READY"

        bad = environment.execute({"type": "command", "command": ["sh", "-c", "exit 3"]})
        assert bad["returncode"] == 3 and bad["ok"] is False

        (tmp_path / "marker.txt").write_text("READY")
        verified = environment.verify_command(["sh", "-c", "cat marker.txt"])
        assert verified.stdout.strip() == "READY"

        denied = environment.verify_command(["sh", "-c", "printf x >> marker.txt"])
        assert denied.returncode != 0, "verifier must not be able to write the workspace"
    finally:
        environment.cleanup()

    # The spec requires cleanup to remove the container, not just stop it.
    gone = _subprocess.run([resolve_docker_binary(), "inspect", environment.container_name],
                           capture_output=True, timeout=30, check=False)
    assert gone.returncode != 0, "cleanup must remove the container"
