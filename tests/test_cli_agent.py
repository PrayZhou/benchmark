"""CLIAgent behaviour, exercised against a stub executable written to tmp_path.

No coding CLI is installed on this host and egress is blocked, so every test
launches a local stub script instead. The stub records the argv and stdin it
received, echoes them as JSON on stdout, and exits with the code stored in a
``.code`` sidecar file.
"""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from benchmark.adapters.agents.cli import CLIAgent
from benchmark.factories import build_agent
from benchmark.schemas import AgentResult, TaskSpec

STUB = """#!/usr/bin/env python3
import json, sys, pathlib
payload = {"argv": sys.argv[1:], "stdin": sys.stdin.read()}
for arg in sys.argv[1:]:
    candidate = pathlib.Path(arg)
    if candidate.is_file():
        payload["prompt"] = candidate.read_text()
print(json.dumps(payload))
sys.exit(int(pathlib.Path(__file__).with_suffix(".code").read_text().strip()))
"""


def _write_stub(tmp_path: Path, *, code: int = 0, body: str = STUB) -> str:
    """Create the executable stub plus its exit-code sidecar; return its path."""
    script = tmp_path / "stub"
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (tmp_path / "stub.code").write_text(str(code), encoding="utf-8")
    return str(script)


def _act(agent: CLIAgent, observation: dict | None = None) -> dict:
    action = agent.act(observation if observation is not None else {})
    assert action["type"] == "finish"
    assert agent.finished()
    return action


def _run(agent: CLIAgent, observation: dict | None = None) -> AgentResult:
    _act(agent, observation)
    return agent.result()


def _payload(result: AgentResult) -> dict:
    assert result.final_message is not None
    return json.loads(result.final_message)


# --- prompt delivery modes -------------------------------------------------


def test_prompt_file_mode_writes_instruction_and_substitutes_path(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    agent = CLIAgent("solve the puzzle", command=script,
                     args=["--prompt-file", "{prompt_file}"], prompt_mode="prompt_file")

    result = _run(agent)

    assert result.status == "completed"
    payload = _payload(result)
    assert payload["argv"][0] == "--prompt-file"
    assert payload["prompt"] == "solve the puzzle"
    # The temp file is removed once the process exits.
    assert not Path(payload["argv"][1]).exists()


def test_stdin_mode_feeds_instruction_on_stdin(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    agent = CLIAgent("explain the bug", command=script, args=[], prompt_mode="stdin")

    payload = _payload(_run(agent))

    assert payload["stdin"] == "explain the bug"
    assert payload["argv"] == []


def test_argv_mode_substitutes_instruction(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    agent = CLIAgent("fix the parser", command=script,
                     args=["-p", "{instruction}"], prompt_mode="argv")

    payload = _payload(_run(agent))

    assert payload["argv"] == ["-p", "fix the parser"]


def test_prompt_flag_is_prepended_to_the_arguments(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    agent = CLIAgent("do it", command=script, args=["{instruction}"],
                     prompt_mode="argv", prompt_flag="-p")

    payload = _payload(_run(agent))

    assert payload["argv"] == ["-p", "do it"]


def test_unknown_prompt_mode_is_rejected(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    with pytest.raises(ValueError, match="prompt_mode"):
        CLIAgent("x", command=script, prompt_mode="telepathy")


# --- exit-code mapping -----------------------------------------------------


def test_success_exit_code_maps_to_completed(tmp_path: Path) -> None:
    script = _write_stub(tmp_path, code=0)
    result = _run(CLIAgent("x", command=script, prompt_mode="stdin"))

    assert result.status == "completed"
    assert result.error is None


def test_unexpected_exit_code_maps_to_error(tmp_path: Path) -> None:
    script = _write_stub(tmp_path, code=4)
    result = _run(CLIAgent("x", command=script, prompt_mode="stdin"))

    assert result.status == "error"
    assert result.error is not None and "4" in result.error


def test_custom_success_exit_codes_are_honoured(tmp_path: Path) -> None:
    script = _write_stub(tmp_path, code=4)
    result = _run(CLIAgent("x", command=script, prompt_mode="stdin", success_exit_codes=(0, 4)))

    assert result.status == "completed"


# --- resilience ------------------------------------------------------------


def test_timeout_yields_an_error_result(tmp_path: Path) -> None:
    body = "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n"
    script = _write_stub(tmp_path, body=body)
    agent = CLIAgent("x", command=script, prompt_mode="stdin", timeout=0.5)

    started = time.monotonic()
    result = _run(agent)
    elapsed = time.monotonic() - started

    assert result.status == "error"
    assert result.error is not None and "timed out" in result.error
    assert elapsed < 4  # killed at the timeout, not after the full sleep


def test_missing_executable_is_an_error_result_not_an_exception(tmp_path: Path) -> None:
    agent = CLIAgent("x", command=str(tmp_path / "definitely-missing"), prompt_mode="stdin")

    result = _run(agent)

    assert result.status == "error"


def test_unresolvable_bare_command_is_an_error_result() -> None:
    agent = CLIAgent("x", command="definitely-not-a-real-coding-cli", prompt_mode="stdin")

    result = _run(agent)

    assert result.status == "error"


# --- command resolution ----------------------------------------------------


def test_absolute_command_passes_through_unchanged(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)

    agent = CLIAgent("x", command=script)

    assert agent.command == script


def test_bare_command_is_resolved_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_stub(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    agent = CLIAgent("x", command="stub")

    assert agent.command == str(tmp_path / "stub")


# --- output handling -------------------------------------------------------


def test_final_message_is_a_bounded_tail_of_combined_output(tmp_path: Path) -> None:
    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('HEAD' + 'x' * 200)\n"
        "sys.stderr.write('TAIL-SENTINEL')\n"
        "sys.exit(0)\n"
    )
    script = _write_stub(tmp_path, body=body)
    agent = CLIAgent("x", command=script, prompt_mode="stdin", max_output_chars=40)

    result = _run(agent)

    assert result.final_message is not None
    assert len(result.final_message) <= 40
    assert result.final_message.endswith("TAIL-SENTINEL")
    assert "HEAD" not in result.final_message


def test_final_message_combines_stdout_and_stderr(tmp_path: Path) -> None:
    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('OUT')\n"
        "sys.stderr.write('ERR')\n"
        "sys.exit(0)\n"
    )
    script = _write_stub(tmp_path, body=body)

    result = _run(CLIAgent("x", command=script, prompt_mode="stdin"))

    assert result.final_message is not None
    assert "OUT" in result.final_message
    assert "ERR" in result.final_message


def test_finish_action_carries_the_bounded_output_tail(tmp_path: Path) -> None:
    """The runner traces every action payload, so the terminal action must carry
    the output; ``final_message`` alone is not persisted and a real CLI run would
    otherwise leave no transcript evidence."""
    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('HEAD' + 'x' * 200)\n"
        "sys.stderr.write('TAIL-SENTINEL')\n"
        "sys.exit(0)\n"
    )
    script = _write_stub(tmp_path, body=body)
    agent = CLIAgent("x", command=script, prompt_mode="stdin", max_output_chars=40)

    action = _act(agent)

    assert action["final_message"] == agent.result().final_message
    assert action["final_message"].endswith("TAIL-SENTINEL")
    assert len(action["final_message"]) <= 40
    assert "HEAD" not in action["final_message"]


def test_cli_output_reaches_the_recorded_trace(tmp_path: Path) -> None:
    """End-to-end: the runner persists action payloads, so a real CLI run must
    leave its output tail in the trace JSONL, not only on AgentResult."""
    from benchmark.adapters.environments.local import LocalEnvironment
    from benchmark.factories import build_grader
    from benchmark.runner import run_trial

    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('CLI-TRANSCRIPT-SENTINEL')\n"
        "sys.exit(0)\n"
    )
    script = _write_stub(tmp_path, body=body)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = TaskSpec(id="t", instruction="x", agent="cli", environment_type="local",
                    cli={"command": script, "prompt_mode": "stdin"},
                    grader={"command": ["bash", "-lc", "true"]})

    result = run_trial(task, LocalEnvironment(workspace), build_agent(task), build_grader(task),
                       tmp_path / "traces", trial_id="cli-trace")

    assert result.trace_path is not None
    events = [json.loads(line) for line in Path(result.trace_path).read_text().splitlines()]
    finish_actions = [event for event in events
                      if event["type"] == "action" and event["payload"].get("type") == "finish"]
    assert finish_actions, "no finish action recorded"
    assert "CLI-TRANSCRIPT-SENTINEL" in finish_actions[-1]["payload"]["final_message"]


def test_no_tokens_are_reported(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)

    result = _run(CLIAgent("x", command=script, prompt_mode="stdin"))

    assert result.input_tokens == 0
    assert result.output_tokens == 0


# --- multi-step ------------------------------------------------------------


def test_one_shot_by_default(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)

    result = _run(CLIAgent("x", command=script, prompt_mode="stdin"))

    assert result.steps == 1


def test_max_steps_relaunches_with_the_previous_output_appended(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    agent = CLIAgent("do the thing", command=script, args=["{instruction}"],
                     prompt_mode="argv", max_steps=2)

    result = _run(agent)

    assert result.steps == 2
    payload = _payload(result)
    final_instruction = payload["argv"][0]
    assert final_instruction.startswith("do the thing")
    assert "Previous output:" in final_instruction


# --- process wiring --------------------------------------------------------


def test_env_and_cwd_are_applied(tmp_path: Path) -> None:
    body = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'env': os.environ.get('STUB_ENV'), 'cwd': os.getcwd()}))\n"
        "sys.exit(0)\n"
    )
    script = _write_stub(tmp_path, body=body)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    agent = CLIAgent("x", command=script, prompt_mode="stdin",
                     env={"STUB_ENV": "value"}, cwd=str(workdir))

    payload = _payload(_run(agent))

    assert payload["env"] == "value"
    assert Path(payload["cwd"]).resolve() == workdir.resolve()


def test_non_string_args_and_env_values_are_stringified(tmp_path: Path) -> None:
    """YAML supplies plain ints for things like ports; they must not break a launch."""
    script = _write_stub(tmp_path)

    agent = CLIAgent("x", command=script, args=["--port", 8080], prompt_mode="argv",
                     env={"RETRIES": 3})

    assert agent.args == ["--port", "8080"]
    assert agent.env == {"RETRIES": "3"}
    assert _run(agent).status == "completed"


_CWD_STUB = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "print(json.dumps({'cwd': os.getcwd()}))\n"
    "sys.exit(0)\n"
)


def test_act_runs_in_the_observation_cwd(tmp_path: Path) -> None:
    """The per-trial workspace arrives on every observation, not through the factory."""
    script = _write_stub(tmp_path, body=_CWD_STUB)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = CLIAgent("x", command=script, prompt_mode="stdin")

    payload = _payload(_run(agent, {"cwd": str(workspace)}))

    assert Path(payload["cwd"]).resolve() == workspace.resolve()


def test_explicit_cwd_wins_over_the_observation(tmp_path: Path) -> None:
    script = _write_stub(tmp_path, body=_CWD_STUB)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    observed = tmp_path / "observed"
    observed.mkdir()
    agent = CLIAgent("x", command=script, prompt_mode="stdin", cwd=str(explicit))

    payload = _payload(_run(agent, {"cwd": str(observed)}))

    assert Path(payload["cwd"]).resolve() == explicit.resolve()


def test_non_string_cwd_is_coerced_and_cannot_raise(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)

    agent = CLIAgent("x", command=script, prompt_mode="stdin", cwd=123)
    assert agent.cwd == "123"

    # A non-string observation cwd is coerced too; a bogus path is an error result.
    result = _run(CLIAgent("x", command=script, prompt_mode="stdin"), {"cwd": 123})
    assert result.status == "error"


# --- factory route ---------------------------------------------------------


def test_factory_routes_cli_agent(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="x", agent="cli", environment_type="local",
                    cli={"command": script, "args": ["{instruction}"], "prompt_mode": "argv"})

    agent = build_agent(task)

    assert isinstance(agent, CLIAgent)
    assert agent.agent_id == "cli"


def test_factory_reads_the_cli_instruction(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="the real instruction", agent="cli", environment_type="local",
                    cli={"command": script, "args": ["{instruction}"], "prompt_mode": "argv"})

    payload = _payload(_run(build_agent(task)))

    assert payload["argv"] == ["the real instruction"]


def test_factory_rejects_cli_agent_in_a_non_local_environment(tmp_path: Path) -> None:
    """The CLI runs on the host, so a container path like /workspace cannot work."""
    task = TaskSpec(id="t", instruction="x", agent="cli",
                    cli={"command": _write_stub(tmp_path)})  # environment_type defaults to docker

    with pytest.raises(ValueError, match="requires environment_type 'local'"):
        build_agent(task)


def test_factory_builds_cli_agent_in_a_local_environment(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="x", agent="cli", environment_type="local",
                    cli={"command": script})

    agent = build_agent(task)

    assert isinstance(agent, CLIAgent)


def test_factory_requires_a_command() -> None:
    with pytest.raises(ValueError, match="command"):
        build_agent(TaskSpec(id="t", instruction="x", agent="cli", environment_type="local"))


def test_factory_clamps_cli_timeout_to_the_trial_budget(tmp_path: Path) -> None:
    """A CLI timeout longer than the trial must not outlive the trial."""
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="x", agent="cli", environment_type="local",
                    cli={"command": script, "timeout": 600}, limits={"timeout_seconds": 120})

    agent = build_agent(task)

    assert isinstance(agent, CLIAgent)
    assert agent.timeout == 120


def test_factory_defaults_cli_timeout_to_the_trial_budget(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="x", agent="cli", environment_type="local",
                    cli={"command": script}, limits={"timeout_seconds": 90})

    agent = build_agent(task)

    assert isinstance(agent, CLIAgent)
    assert agent.timeout == 90


def test_factory_keeps_a_tighter_cli_timeout(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="x", agent="cli", environment_type="local",
                    cli={"command": script, "timeout": 15}, limits={"timeout_seconds": 120})

    agent = build_agent(task)

    assert isinstance(agent, CLIAgent)
    assert agent.timeout == 15


# --- documented profiles ---------------------------------------------------


def test_profiles_are_valid_cli_configs() -> None:
    from benchmark.adapters.agents.cli import CLIAgent
    from benchmark.adapters.agents.profiles import PROFILES

    assert {"claude", "codex"} <= set(PROFILES)
    for profile in PROFILES.values():
        CLIAgent("do the thing", **profile)  # must not raise
