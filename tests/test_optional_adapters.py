import builtins

import pytest

from benchmark.adapters import OptionalDependencyError
from benchmark.adapters.agents import MCPAgent, MultiAgentAdapter
from benchmark.adapters.environments import (
    BrowserGymEnvironment,
    HarborEnvironment,
    MCPEnvironment,
    OSWorldEnvironment,
)
from benchmark.schemas import TaskSpec


def _build_mcp_environment(factories, tmp_path):
    return factories.build_environment(TaskSpec(id="t", instruction="x", environment_type="mcp"), tmp_path)


def _build_mcp_agent(factories, tmp_path):
    return factories.build_agent(TaskSpec(id="t", instruction="x", agent="mcp"))


def _build_multi_agent(factories, tmp_path):
    return factories.build_agent(TaskSpec(id="t", instruction="x", agent="multi_agent"))


ROUTES = [_build_mcp_environment, _build_mcp_agent, _build_multi_agent]
ENVIRONMENTS = [HarborEnvironment, BrowserGymEnvironment, OSWorldEnvironment, MCPEnvironment]


@pytest.mark.parametrize(
    "adapter",
    [HarborEnvironment, BrowserGymEnvironment, OSWorldEnvironment, MCPEnvironment, MCPAgent, MultiAgentAdapter],
)
def test_optional_adapters_are_import_safe_and_clear_when_dependency_missing(adapter, monkeypatch):
    real_import = builtins.__import__

    def missing_optional(name, *args, **kwargs):
        if name in {"harbor", "browsergym", "osworld", "mcp", "multi_agent"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_optional)
    with pytest.raises(OptionalDependencyError, match="optional"):
        adapter()


def test_exports_do_not_import_optional_sdks():
    import benchmark.adapters.agents as agents
    import benchmark.adapters.environments as environments

    assert environments.HarborEnvironment
    assert environments.MCPEnvironment
    assert agents.MCPAgent


@pytest.mark.parametrize("route", ROUTES)
def test_factory_routes_resolve_to_optional_scaffolds(route, monkeypatch, tmp_path):
    """The mcp/multi_agent routes must reach their scaffold, not a missing module or attribute."""
    from benchmark import factories

    real_import = builtins.__import__

    def missing_optional(name, *args, **kwargs):
        if name in {"mcp", "multi_agent"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_optional)
    with pytest.raises(OptionalDependencyError, match="optional"):
        route(factories, tmp_path)


@pytest.mark.parametrize("route", ROUTES)
def test_factory_routes_refuse_to_fabricate_a_run(route, monkeypatch, tmp_path):
    """With the optional dependency present the scaffold still refuses to pretend it ran."""
    import types

    from benchmark import factories

    real_import = builtins.__import__

    def present_optional(name, *args, **kwargs):
        if name in {"mcp", "multi_agent"}:
            return types.ModuleType(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", present_optional)
    with pytest.raises(NotImplementedError):
        route(factories, tmp_path)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_reset_reports_the_same_wiring_gap_as_construction(environment, monkeypatch):
    """``reset`` must not claim a different (or false) reason than ``__init__``."""
    import types

    real_import = builtins.__import__

    def present_optional(name, *args, **kwargs):
        if name in {"harbor", "browsergym", "osworld", "mcp"}:
            return types.ModuleType(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", present_optional)
    instance = object.__new__(environment)
    with pytest.raises(NotImplementedError, match="backend wiring is not implemented"):
        instance.reset(TaskSpec(id="t", instruction="x"))
