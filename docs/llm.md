# LLM adapter contract

`benchmark.adapters.agents.llm.LLMAdapter` is a provider-neutral `AgentAdapter`. Construct it with the task instruction and an explicit model (install the optional SDK with `pip install -e '.[llm]'`):

```python
agent = LLMAdapter(task.instruction, model="gpt-4.1-mini", backend=my_backend)
```

The adapter sends a system message containing the instruction and user messages containing JSON observations. Each backend call receives `messages`, the two tool definitions, `model`, `max_output_tokens`, `timeout`, and `seed`. A backend may return an OpenAI-compatible response object or dictionaries. The optional OpenAI backend is imported lazily, reads the key from the environment variable named by `api_key_env` (default `OPENAI_API_KEY`), honours `base_url`, uses `max_retries=0`, and never includes credentials in errors.

`benchmark.factories.build_agent` forwards `provider.base_url`, `provider.api_key_env`, `provider.timeout_seconds`, `provider.max_output_tokens`, `provider.pass_seed`, and the trial's derived `seed` from the `TaskSpec`, so an OpenAI-compatible gateway is configured entirely from the task config. `provider.provider` selects the backend through `benchmark.adapters.agents.backends.resolve_backend` (`openai` and `openai-compatible` are aliases for `OpenAIBackend`; an empty string or `mock` means "unset" and falls back to `openai-compatible`). An unknown provider name raises `ValueError` when the agent is built, instead of silently taking the OpenAI-compatible path. Backend construction stays lazy, so no credential is read until the first `act()`. `seed` is passed through to the backend when it is not `None` and `pass_seed` is true; determinism beyond that is the provider's guarantee, not the harness's. Set `provider.pass_seed: false` for gateways that reject or intermittently fail on the `seed` parameter (some internal OpenAI-compatible gateways are known to do this).

## Tools and normalized actions

- `execute_command({"command": ["program", "arg"]})` becomes `{"type": "command", "command": [...]}`.
- `finish({"message": "..."})` becomes `{"type": "finish"}` and completes the agent.

A response containing several tool calls is queued in order and emitted one action per `act` call. The observation supplied after each environment action should contain `last_result`; it is sent back as a tool message associated with the corresponding call ID. This preserves multi-turn feedback and avoids dropping calls. Malformed JSON, malformed command arguments, unknown tools, empty responses, and backend failures end with an `error` result and a generic, secret-safe error message.

Response text and observations/tool results are bounded by `max_observation_chars`. `max_output_tokens` is passed on every call. Usage from every call is accumulated in `AgentResult.input_tokens` and `output_tokens`; `duration_seconds` is wall-clock adapter lifetime and `tool_calls` counts emitted tools. The adapter itself never executes commands: the environment remains responsible for execution and sandboxing.

Real paid provider calls are intentionally never made by the test suite. The adapter accepts an injected `backend`, which is the supported way to test it without network access; `tests/test_llm_adapter.py` exercises that path (tool batching, tool-result feedback, malformed/unknown/empty responses, and secret-safe backend failures).
