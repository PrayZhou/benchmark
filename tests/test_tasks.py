from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.adapters.environments.docker import DockerEnvironment
from benchmark.adapters.environments.local import LocalEnvironment
from benchmark.schemas import TaskSpec
from benchmark.tasks import TaskDirectoryError, discover_tasks, load_task_dir, stage_workspace


def make_task_dir(root: Path, name: str = "demo_task", *, spec: str | None = None,
                  instruction: str | None = "Do the thing.", verifier: str | None = "test -f out.txt") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    if instruction is not None:
        (directory / "instruction.md").write_text(instruction + "\n")
    if verifier is not None:
        (directory / "tests").mkdir()
        (directory / "tests" / "test.sh").write_text(verifier + "\n")
    if spec is not None:
        (directory / "task.yaml").write_text(spec)
    return directory


def add_workspace(directory: Path, files: dict[str, str]) -> Path:
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=True)
    for name, content in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return workspace


def test_instruction_and_verifier_come_from_files(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path)
    task = load_task_dir(directory)
    assert task.id == "demo_task"
    assert task.instruction == "Do the thing."
    assert task.grader["command"][:2] == ["bash", "-c"]
    assert "test -f out.txt" in task.grader["command"][2]
    assert task.metadata["task_dir"] == str(directory)


def test_task_yaml_supplies_everything_else(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, spec="id: suite.demo\nseed: 7\nlimits:\n  max_steps: 3\n")
    task = load_task_dir(directory)
    assert task.id == "suite.demo"
    assert task.seed == 7
    assert task.limits.max_steps == 3
    assert task.instruction == "Do the thing."


def test_duplicate_instruction_is_rejected(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, spec="instruction: a second copy\n")
    with pytest.raises(TaskDirectoryError, match="instruction is defined in both"):
        load_task_dir(directory)


def test_duplicate_verifier_is_rejected(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, spec="grader:\n  command: [bash, -lc, 'true']\n")
    with pytest.raises(TaskDirectoryError, match="verifier is defined in both"):
        load_task_dir(directory)


def test_missing_instruction_is_rejected(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, instruction=None)
    with pytest.raises(TaskDirectoryError, match="missing instruction.md"):
        load_task_dir(directory)


def test_missing_verifier_is_rejected_for_command_grader(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, verifier=None)
    with pytest.raises(TaskDirectoryError, match="missing tests/test.sh"):
        load_task_dir(directory)


def test_state_grader_does_not_require_a_verifier_script(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, verifier=None,
                              spec="grader_type: state\ngrader:\n  expected:\n    completed: true\n")
    task = load_task_dir(directory)
    assert task.grader_type == "state"


def test_dockerfile_is_recorded_in_metadata(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path)
    (directory / "Dockerfile").write_text("FROM python:3.11-slim\n")
    assert load_task_dir(directory).metadata["dockerfile"] == str(directory / "Dockerfile")


def test_task_yaml_with_a_tasks_list_is_rejected(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path, spec="tasks:\n  - id: a\n")
    with pytest.raises(TaskDirectoryError, match="single task"):
        load_task_dir(directory)


def test_discover_tasks_is_ordered_and_skips_non_task_dirs(tmp_path: Path) -> None:
    make_task_dir(tmp_path, "b_task")
    make_task_dir(tmp_path, "a_task")
    (tmp_path / "not_a_task").mkdir()
    assert [task.id for task in discover_tasks(tmp_path)] == ["a_task", "b_task"]


def test_discover_tasks_rejects_empty_root(tmp_path: Path) -> None:
    with pytest.raises(TaskDirectoryError, match="no task directories"):
        discover_tasks(tmp_path)


def test_missing_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(TaskDirectoryError, match="task directory not found"):
        load_task_dir(tmp_path / "nope")


def test_repository_hello_task_loads_and_runs(tmp_path: Path) -> None:
    """The shipped task directory must be loadable and pass its own verifier."""
    from benchmark.adapters.agents.scripted import ScriptedAgent
    from benchmark.adapters.graders.command import CommandGrader
    from benchmark.runner import run_trial

    repo_task = Path(__file__).resolve().parents[1] / "tasks" / "hello_task"
    task = load_task_dir(repo_task)
    assert task.id == "hello.create_marker"
    result = run_trial(
        task,
        LocalEnvironment(tmp_path),
        ScriptedAgent(task.environment["actions"]),
        CommandGrader(task.grader["command"], isolated=False),
        tmp_path / "traces",
        trial_id="hello",
    )
    assert result.success is True


def test_stage_workspace_copies_the_fixture_tree_into_the_root(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path / "source")
    add_workspace(directory, {"Makefile": "all:\n\techo hi\n", "nested/input.log": "payload"})
    task = load_task_dir(directory)

    stage_workspace(task, tmp_path / "work")

    assert (tmp_path / "work" / "Makefile").read_text() == "all:\n\techo hi\n"
    assert (tmp_path / "work" / "nested" / "input.log").read_text() == "payload"


def test_stage_workspace_is_a_noop_without_a_workspace_directory(tmp_path: Path) -> None:
    task = load_task_dir(make_task_dir(tmp_path / "source"))

    stage_workspace(task, tmp_path / "work")

    assert not (tmp_path / "work").exists()


def test_stage_workspace_is_a_noop_without_task_dir_metadata(tmp_path: Path) -> None:
    stage_workspace(TaskSpec(id="inline", instruction="x"), tmp_path / "work")

    assert not (tmp_path / "work").exists()


def test_stage_workspace_does_not_materialise_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top-secret")
    directory = make_task_dir(tmp_path / "source")
    workspace = add_workspace(directory, {"real.txt": "real"})
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    (workspace / "linkdir").symlink_to(outside)
    task = load_task_dir(directory)

    stage_workspace(task, tmp_path / "work")

    assert (tmp_path / "work" / "real.txt").read_text() == "real"
    assert not (tmp_path / "work" / "link.txt").exists()
    assert not (tmp_path / "work" / "linkdir").exists()


def test_local_environment_reset_stages_the_task_workspace(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path / "source")
    add_workspace(directory, {"fizzbuzz.py": "print('buggy')\n"})
    task = load_task_dir(directory)

    LocalEnvironment(tmp_path / "work").reset(task)

    assert (tmp_path / "work" / "fizzbuzz.py").read_text() == "print('buggy')\n"


def test_docker_environment_reset_stages_before_the_container_starts(tmp_path: Path) -> None:
    directory = make_task_dir(tmp_path / "source")
    add_workspace(directory, {"input.log": "INFO one\n"})
    task = load_task_dir(directory)
    staged_when_container_started: list[bool] = []

    def fake_run(args, *, timeout, check=True):
        if args[0] == "run":
            staged_when_container_started.append((tmp_path / "work" / "input.log").exists())
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    environment = DockerEnvironment(tmp_path / "work", image="alpine:3", docker_binary="/usr/bin/true")
    environment._docker = fake_run  # type: ignore[method-assign]
    environment.reset(task)

    assert staged_when_container_started == [True]
    assert (environment.root / "input.log").read_text() == "INFO one\n"
