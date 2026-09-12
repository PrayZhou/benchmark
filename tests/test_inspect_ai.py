from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from benchmark.integrations.inspect_ai import build_inspect_task
from benchmark.schemas import TrialResult


class FakeState:
    def __init__(self) -> None:
        self.metadata = {}


def install_fake_inspect(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    class Task:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MemoryDataset(list):
        pass

    class Sample:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Score:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def decorator(factory):
        return factory

    root = types.ModuleType("inspect_ai")
    root.Task = Task
    dataset = types.ModuleType("inspect_ai.dataset")
    dataset.MemoryDataset, dataset.Sample = MemoryDataset, Sample
    scorer = types.ModuleType("inspect_ai.scorer")
    scorer.Score, scorer.scorer = Score, decorator
    solver = types.ModuleType("inspect_ai.solver")
    solver.solver = decorator
    for name, module in (("inspect_ai", root), ("inspect_ai.dataset", dataset), ("inspect_ai.scorer", scorer), ("inspect_ai.solver", solver)):
        monkeypatch.setitem(sys.modules, name, module)
    return {"Task": Task, "Score": Score}


def test_inspect_bridge_runs_core_trial_and_maps_score(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_fake_inspect(monkeypatch)
    task_config = {"id": "demo", "instruction": "do it"}
    expected = TrialResult(task_id="demo", trial_id="t1", agent_id="a", seed=0, status="completed", success=True, score=0.75)
    calls = []

    def fake_run_trial(**kwargs):
        calls.append(kwargs)
        return expected

    task = build_inspect_task(
        task_config,
        environment_builder=lambda task: "environment",
        agent_builder=lambda task: "agent",
        grader_builder=lambda task: "grader",
        run_trial_fn=fake_run_trial,
        trace_dir=tmp_path / "traces",
    )
    state = FakeState()
    solved = asyncio.run(task.solver(state, None))
    assert solved is state
    assert calls[0]["task"].id == "demo"
    score = asyncio.run(task.scorer(state, None))
    assert score.value == 0.75
    assert score.answer == "success"
    assert score.metadata["benchmark_result"]["trial_id"] == "t1"


def test_inspect_bridge_reports_missing_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "inspect_ai" or name.startswith("inspect_ai."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    import benchmark.integrations.inspect_ai as integration

    monkeypatch.setattr(integration, "_load_inspect", lambda: (_ for _ in ()).throw(RuntimeError("Inspect AI integration requires the optional 'inspect-ai' package")))
    with pytest.raises(RuntimeError, match="optional 'inspect-ai'"):
        integration.build_inspect_task(
            {"id": "demo", "instruction": "do it"},
            environment_builder=lambda task: None,
            agent_builder=lambda task: None,
            grader_builder=lambda task: None,
        )
