from __future__ import annotations

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
