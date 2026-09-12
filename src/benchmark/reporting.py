from __future__ import annotations

import json
from pathlib import Path

from .schemas import RunSummary, TrialResult


def group_key(result: TrialResult) -> str:
    """Identity of the logical trial a result belongs to.

    ``parent_trial_id`` is assigned by the orchestrator per (task, run_index,
    seed) group. Results produced outside the orchestrator have none, so they
    fall back to their own trial id and form a group of one.
    """
    return result.parent_trial_id or result.trial_id


def final_attempts(results: list[TrialResult]) -> list[TrialResult]:
    """Reduce every retry group to the attempt that decided its outcome."""
    groups: dict[str, TrialResult] = {}
    for result in results:
        key = group_key(result)
        current = groups.get(key)
        if current is None or result.attempt > current.attempt:
            groups[key] = result
    return list(groups.values())


def summarize(results: list[TrialResult]) -> RunSummary:
    decided = final_attempts(results)
    counts: dict[str, int] = {}
    for result in results:
        counts[group_key(result)] = counts.get(group_key(result), 0) + 1
    total = len(decided)
    successful = sum(result.success for result in decided)
    failures: dict[str, int] = {}
    for result in decided:
        if result.failure_category:
            failures[result.failure_category] = failures.get(result.failure_category, 0) + 1
    violations_by_kind: dict[str, int] = {}
    uncertain_trials = 0
    for result in decided:
        for finding in result.violations:
            violations_by_kind[finding.kind] = violations_by_kind.get(finding.kind, 0) + 1
        if result.uncertainties:
            uncertain_trials += 1
    return RunSummary(
        total=total,
        completed=sum(result.status == "completed" for result in decided),
        successful=successful,
        success_rate=successful / total if total else 0.0,
        average_score=sum(result.score for result in decided) / total if total else 0.0,
        average_duration_seconds=sum(result.duration_seconds for result in decided) / total if total else 0.0,
        failures_by_category=failures,
        total_attempts=len(results),
        retried_groups=sum(1 for count in counts.values() if count > 1),
        violations_by_kind=violations_by_kind,
        uncertain_trials=uncertain_trials,
    )


def write_report(results: list[TrialResult], output_dir: Path, *, dataset: str | None = None) -> Path:
    """Write the run report.

    ``summary`` counts each logical trial once; ``trials`` keeps every attempt
    so the retry history stays auditable. ``dataset`` labels the run set so
    ``benchmark compare --dataset`` can select it later.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(results)
    payload = {
        "dataset": dataset,
        "summary": summary.model_dump(mode="json"),
        "trials": [result.model_dump(mode="json") for result in results],
    }
    path = output_dir / "report.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
