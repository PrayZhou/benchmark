"""LLMAdapter behaviour exercised through an injected, offline backend.

These tests never make a paid provider call: the adapter's ``backend`` injection
point is the supported way to test it without network access.
"""
import json

import pytest

from benchmark.adapters.agents.backends import DEFAULT_BACKEND, resolve_backend
from benchmark.adapters.agents.llm import TOOLS, LLMAdapter, OpenAIBackend
from benchmark.factories import build_agent
from benchmark.schemas import TaskSpec


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


def _response(tool_calls: list[dict], *, content: str | None = None,
              prompt_tokens: int = 1, completion_tokens: int = 2) -> dict:
    return {
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        "choices": [{"message": {"content": content, "tool_calls": tool_calls}}],
    }


def _finish(message: str = "done", call_id: str = "call-1") -> dict:
    return _response([_call(call_id, "finish", {"message": message})])


def _command(command: list[str], call_id: str = "call-1") -> dict:
    return _response([_call(call_id, "execute_command", {"command": command})])


class FakeBackend:
    """Returns scripted OpenAI-compatible responses and records every call."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, messages, tools, *, model, max_output_tokens, timeout, seed=None):
        self.calls.append({
            "messages": [dict(message) for message in messages],
            "tools": tools,
            "model": model,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
            "seed": seed,
        })
        return self.responses.pop(0)


def test_finish_completes_the_agent_and_records_usage():
    backend = FakeBackend([_finish("all done")])
    agent = LLMAdapter("do it", model="m", backend=backend, seed=7)

    assert agent.act({"state": {}}) == {"type": "finish"}
    assert agent.finished() is True
    result = agent.result()
    assert result.status == "completed"
    assert result.final_message == "all done"
    assert result.tool_calls == 1
    assert (result.input_tokens, result.output_tokens) == (1, 2)

    call = backend.calls[0]
    assert call["model"] == "m"
    assert call["seed"] == 7
    assert call["tools"] == TOOLS
    assert call["messages"][0] == {"role": "system", "content": "do it"}


def test_execute_command_then_finish_feeds_the_tool_result_back():
    backend = FakeBackend([
        _command(["echo", "hi"], call_id="c1"),
        _finish("ok", call_id="c2"),
    ])
    agent = LLMAdapter("go", model="m", backend=backend)

    assert agent.act({"state": {}}) == {"type": "command", "command": ["echo", "hi"]}
    assert agent.finished() is False
    assert agent.act({"state": {}, "last_result": {"ok": True, "stdout": "hi"}}) == {"type": "finish"}

    result = agent.result()
    assert result.status == "completed"
    assert result.tool_calls == 2
    # usage accumulates across every backend call
    assert (result.input_tokens, result.output_tokens) == (2, 4)

    tool_messages = [m for m in backend.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and tool_messages[-1]["tool_call_id"] == "c1"
    assert "hi" in tool_messages[-1]["content"]


def test_multiple_tool_calls_are_emitted_one_per_act_and_in_order():
    backend = FakeBackend([_response([
        _call("a", "execute_command", {"command": ["one"]}),
        _call("b", "execute_command", {"command": ["two"]}),
        _call("c", "finish", {}),
    ])])
    agent = LLMAdapter("go", model="m", backend=backend)

    assert agent.act({"state": {}}) == {"type": "command", "command": ["one"]}
    assert agent.act({"state": {}, "last_result": {"ok": True}}) == {"type": "command", "command": ["two"]}
    assert agent.act({"state": {}, "last_result": {"ok": True}}) == {"type": "finish"}
    assert agent.result().tool_calls == 3
    assert len(backend.calls) == 1  # the whole batch came from one backend round trip


def test_observation_is_bounded_by_max_observation_chars():
    backend = FakeBackend([_finish()])
    agent = LLMAdapter("go", model="m", backend=backend, max_observation_chars=20)

    agent.act({"state": "x" * 100})

    user = [m for m in backend.calls[0]["messages"] if m["role"] == "user"][-1]
    assert len(user["content"]) <= len("Observation:\n") + 20


def test_malformed_tool_arguments_end_with_an_error():
    backend = FakeBackend([_response([
        {"id": "x", "function": {"name": "execute_command", "arguments": "{not json"}},
    ])])
    agent = LLMAdapter("go", model="m", backend=backend)

    assert agent.act({"state": {}}) == {"type": "finish"}
    result = agent.result()
    assert result.status == "error"
    assert "malformed tool arguments" in result.error


def test_malformed_execute_command_arguments_are_rejected():
    backend = FakeBackend([_response([
        _call("x", "execute_command", {"command": "echo hi"}),
    ])])
    agent = LLMAdapter("go", model="m", backend=backend)

    agent.act({"state": {}})
    result = agent.result()
    assert result.status == "error"
    assert result.error == "malformed execute_command arguments"


def test_unknown_tool_is_rejected():
    backend = FakeBackend([_response([_call("x", "delete_everything", {})])])
    agent = LLMAdapter("go", model="m", backend=backend)

    agent.act({"state": {}})
    assert agent.result().error == "unknown tool requested"


def test_response_without_a_tool_call_is_an_error():
    backend = FakeBackend([_response([], content="I will just chat")])
    agent = LLMAdapter("go", model="m", backend=backend)

    agent.act({"state": {}})
    result = agent.result()
    assert result.status == "error"
    assert "no tool call" in result.error


def test_backend_failure_is_reported_without_leaking_credentials():
    class BoomBackend:
        def complete(self, *args, **kwargs):
            raise RuntimeError("api_key=sk-secret-value")

    agent = LLMAdapter("go", model="m", backend=BoomBackend())
    assert agent.act({"state": {}}) == {"type": "finish"}
    result = agent.result()
    assert result.status == "error"
    assert result.error == "LLM request failed: RuntimeError"
    assert "sk-secret-value" not in (result.error or "")


def test_model_is_required():
    with pytest.raises(ValueError):
        LLMAdapter("go", model="   ")


def test_seed_can_be_withheld_for_gateways_that_reject_it():
    backend = FakeBackend([_finish()])
    agent = LLMAdapter("go", model="m", backend=backend, seed=3, pass_seed=False)

    agent.act({"state": {}})
    assert backend.calls[0]["seed"] is None


def test_openai_backend_reports_the_missing_optional_dependency(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def missing_optional(name, *args, **kwargs):
        if name == "openai":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_optional)
    with pytest.raises(RuntimeError, match="optional 'openai'"):
        OpenAIBackend(api_key="sk-not-a-real-key")


def test_factory_forwards_provider_config_to_the_adapter():
    task = TaskSpec(
        id="t", instruction="x", agent="llm", seed=5,
        provider={"provider": "openai", "model": "gpt-test", "base_url": "https://gateway.example/v1",
                  "api_key_env": "MY_KEY", "timeout_seconds": 3.5, "max_output_tokens": 77,
                  "pass_seed": False},
    )
    agent = build_agent(task)
    assert isinstance(agent, LLMAdapter)
    assert agent.model == "gpt-test"
    assert agent.base_url == "https://gateway.example/v1"
    assert agent.api_key_env == "MY_KEY"
    assert agent.timeout == 3.5
    assert agent.max_output_tokens == 77
    assert agent.seed == 5
    assert agent.pass_seed is False


def test_factory_requires_a_model_for_the_llm_agent():
    with pytest.raises(ValueError, match="provider.model"):
        build_agent(TaskSpec(id="t", instruction="x", agent="llm"))


def test_resolve_backend_maps_names_and_treats_mock_as_unset():
    assert resolve_backend("openai") == ("openai", OpenAIBackend)
    assert resolve_backend("OpenAI-Compatible")[0] == "openai-compatible"
    assert resolve_backend("openai-compatible")[1] is OpenAIBackend
    assert resolve_backend("")[0] == DEFAULT_BACKEND
    assert resolve_backend("mock")[0] == DEFAULT_BACKEND
    assert resolve_backend(None)[0] == DEFAULT_BACKEND


def test_resolve_backend_rejects_an_unknown_provider():
    with pytest.raises(ValueError, match="unsupported provider 'anthropic'"):
        resolve_backend("anthropic")


def test_factory_rejects_an_unknown_provider_before_any_trial():
    task = TaskSpec(id="t", instruction="x", agent="llm",
                    provider={"provider": "anthropic", "model": "claude"})
    with pytest.raises(ValueError, match="unsupported provider"):
        build_agent(task)


def test_adapter_resolves_the_backend_lazily_from_the_provider():
    """Building the adapter must not import a provider SDK or require a key."""
    agent = build_agent(TaskSpec(id="t", instruction="x", agent="llm",
                                 provider={"provider": "openai-compatible", "model": "m"}))

    assert agent.provider == "openai-compatible"
    assert agent.backend is None
    assert agent.backend_factory is OpenAIBackend


def test_adapter_uses_the_resolved_backend_factory():
    seen: dict = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return FakeBackend([_finish()])

    agent = LLMAdapter("go", model="m", provider="custom", backend_factory=factory,
                       base_url="https://gateway.example/v1", api_key_env="MY_KEY")
    agent.act({"state": {}})

    assert seen == {"base_url": "https://gateway.example/v1", "api_key_env": "MY_KEY"}
