from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.tasks import TaskDirectoryError, discover_tasks, load_task_dir


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
    from benchmark.adapters.environments.local import LocalEnvironment
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
