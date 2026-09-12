from __future__ import annotations

from pathlib import Path

from .adapters.agents.scripted import ScriptedAgent
from .adapters.environments.local import LocalEnvironment
from .adapters.graders.command import CommandGrader
from .interfaces import AgentAdapter, Environment, Grader
from .schemas import TaskSpec


def build_agent(task: TaskSpec) -> AgentAdapter:
    if task.agent == "scripted":
        return ScriptedAgent(task.environment.get("actions", [{"type": "finish"}]))
    if task.agent == "llm":
        from .adapters.agents.backends import resolve_backend
        from .adapters.agents.llm import LLMAdapter
        provider = task.provider
        if not provider.model:
            raise ValueError("provider.model is required for llm agent")
        # ``provider.provider`` selects the backend; an unknown name is rejected
        # here, before any trial runs, instead of silently using OpenAI-compatible.
        backend_name, backend_factory = resolve_backend(provider.provider)
        return LLMAdapter(task.instruction, model=provider.model, timeout=provider.timeout_seconds,
                          max_output_tokens=provider.max_output_tokens, seed=task.seed,
                          pass_seed=provider.pass_seed, base_url=provider.base_url,
                          api_key_env=provider.api_key_env, provider=backend_name,
                          backend_factory=backend_factory)
    if task.agent == "mcp":
        from .adapters.agents.mcp import MCPAgent
        return MCPAgent()  # honest scaffold; raises its clear refusal
    if task.agent == "cli":
        from .adapters.agents.cli import CLIAgent
        if not task.cli.get("command"):
            raise ValueError("cli.command is required for agent 'cli'")
        options = dict(task.cli)
        # All of a CLI agent's launches happen inside one act(), so the trial
        # deadline is only enforced by the process timeout. Never let the CLI
        # outlive the trial budget.
        configured_timeout = options.get("timeout")
        options["timeout"] = (min(float(configured_timeout), task.limits.timeout_seconds)
                              if configured_timeout is not None else task.limits.timeout_seconds)
        return CLIAgent(task.instruction, **options)
    if task.agent == "multi_agent":
        from .adapters.agents.multi_agent import MultiAgentAdapter
        return MultiAgentAdapter.from_task(task)
    raise ValueError(f"unknown agent adapter: {task.agent}")


def build_environment(task: TaskSpec, root: Path) -> Environment:
    if task.environment_type == "local":
        return LocalEnvironment(
            root,
            command_timeout=_command_timeout(task),
            env_allowlist=task.environment.get("env_allowlist"),
            env=task.environment.get("env"),
        )
    if task.environment_type == "docker":
        from .adapters.environments.docker import DockerEnvironment
        image = task.environment.get("image")
        if not image:
            raise ValueError("environment.image is required for docker")
        return DockerEnvironment(root, image=image, network=task.environment.get("network", "none"),
                                 command_timeout=_command_timeout(task),
                                 docker_binary=task.environment.get("docker_binary"))
    if task.environment_type == "harbor":
        from .adapters.environments.harbor import HarborEnvironment
        return HarborEnvironment(task.environment)  # type: ignore[return-value]  # scaffold
    if task.environment_type == "browsergym":
        from .adapters.environments.browsergym import BrowserGymEnvironment
        return BrowserGymEnvironment(task.environment)  # type: ignore[return-value]  # scaffold
    if task.environment_type == "osworld":
        from .adapters.environments.osworld import OSWorldEnvironment
        return OSWorldEnvironment(task.environment)  # type: ignore[return-value]  # scaffold
    if task.environment_type == "mcp":
        from .adapters.environments.mcp import MCPEnvironment
        return MCPEnvironment(task.environment)  # type: ignore[return-value]  # scaffold
    raise ValueError(f"unknown environment adapter: {task.environment_type}")


def build_grader(task: TaskSpec) -> Grader:
    if task.grader_type == "command":
        command = task.grader.get("command", ["bash", "-lc", "test -f marker.txt"])
        threshold = task.grader.get("pass_threshold")
        return CommandGrader(command, isolated=task.environment_type != "local",
                             score_pattern=task.grader.get("score_pattern"),
                             pass_threshold=1.0 if threshold is None else float(threshold))
    if task.grader_type == "state":
        from .adapters.graders.state import StateGrader
        return StateGrader(task.grader.get("expected", {}))
    if task.grader_type == "composite":
        from .adapters.graders.composite import CompositeGrader
        graders = [build_grader(TaskSpec(**{**task.model_dump(), "grader_type": item["type"], "grader": item}))
                   for item in task.grader.get("graders", [])]
        return CompositeGrader(graders, weights=task.grader.get("weights"),
                               threshold=task.grader.get("threshold"))
    if task.grader_type == "policy":
        from .adapters.graders.policy import PolicyGrader
        return PolicyGrader(
            task.metadata.get("task_dir"),
            forbidden_paths=task.grader.get("forbidden_paths"),
            forbidden_commands=task.grader.get("forbidden_commands"),
            forbidden_network=bool(task.grader.get("forbidden_network", False)),
            fail_closed=bool(task.grader.get("fail_closed", True)),
            severity_threshold=task.grader.get("severity_threshold", "medium"),
        )
    if task.grader_type == "judge":
        from .adapters.graders.judge import JudgeGrader
        return JudgeGrader(
            model=task.grader.get("model", ""),
            rubric=task.grader.get("rubric"),
            provider=task.grader.get("provider", "openai-compatible"),
            base_url=task.grader.get("base_url"),
            api_key_env=task.grader.get("api_key_env", "OPENAI_API_KEY"),
            timeout=float(task.grader.get("timeout", 60.0)),
            max_output_tokens=int(task.grader.get("max_output_tokens", 1024)),
            fail_closed=bool(task.grader.get("fail_closed", True)),
            severity_threshold=task.grader.get("severity_threshold", "medium"),
        )
    raise ValueError(f"unknown grader adapter: {task.grader_type}")


def _command_timeout(task: TaskSpec) -> float:
    value = task.environment.get("command_timeout")
    return 30.0 if value is None else float(value)
