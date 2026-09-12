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


def _run(agent: CLIAgent) -> AgentResult:
    assert agent.act({}) == {"type": "finish"}
    assert agent.finished()
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


# --- factory route ---------------------------------------------------------


def test_factory_routes_cli_agent(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="x", agent="cli",
                    cli={"command": script, "args": ["{instruction}"], "prompt_mode": "argv"})

    agent = build_agent(task)

    assert isinstance(agent, CLIAgent)
    assert agent.agent_id == "cli"


def test_factory_reads_the_cli_instruction(tmp_path: Path) -> None:
    script = _write_stub(tmp_path)
    task = TaskSpec(id="t", instruction="the real instruction", agent="cli",
                    cli={"command": script, "args": ["{instruction}"], "prompt_mode": "argv"})

    payload = _payload(_run(build_agent(task)))

    assert payload["argv"] == ["the real instruction"]


def test_factory_requires_a_command() -> None:
    with pytest.raises(ValueError, match="command"):
        build_agent(TaskSpec(id="t", instruction="x", agent="cli"))
