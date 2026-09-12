from __future__ import annotations

import concurrent.futures
import logging
import tempfile
import time
import uuid
from pathlib import Path

from .factories import build_agent, build_environment, build_grader
from .manifest import ArtifactStore, RunManifest
from .reporting import summarize, write_report
from .runner import run_trial
from .schemas import RunConfig, TaskSpec, TrialResult

logger = logging.getLogger(__name__)


def run_tasks(
    tasks: list[TaskSpec],
    root: Path,
    output_dir: Path,
    run_config: RunConfig | None = None,
) -> list[TrialResult]:
    cfg = run_config or RunConfig()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    manifest = RunManifest(run_id, tasks, root=output_dir, harness_version=cfg.harness_version, run_config=cfg)
    jobs = [(task, i) for task in tasks for i in range(cfg.runs_per_task)]
    logger.info(
        "run started",
        extra={"event": "run_start", "run_id": run_id, "tasks": len(tasks),
               "jobs": len(jobs), "concurrency": cfg.concurrency,
               "runs_per_task": cfg.runs_per_task},
    )

    def execute(job: tuple[TaskSpec, int]) -> list[TrialResult]:
        task, run_index = job
        seed = task.seed + run_index
        # The derived seed must reach the adapters, not just the trial id, or
        # repeated runs are indistinguishable and nothing is reproducible.
        seeded_task = task.model_copy(update={"seed": seed})
        group_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{task.id}:{run_index}:{seed}"))
        attempts: list[TrialResult] = []
        for attempt in range(1, cfg.retry.max_attempts + 1):
            trial_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{group_id}:{attempt}"))
            workspace = Path(tempfile.mkdtemp(prefix=f"{task.id.replace('.', '-')}-{run_index}-{attempt}-", dir=root))
            try:
                environment = build_environment(seeded_task, workspace)
                agent = build_agent(seeded_task)
                grader = build_grader(seeded_task)
                result = run_trial(seeded_task, environment, agent, grader, output_dir / "traces", trial_id=trial_id,
                                   artifact_store=ArtifactStore(
                                       output_dir / "artifacts" / task.id / str(run_index) / str(attempt),
                                       max_files=cfg.artifact_max_files,
                                       max_file_bytes=cfg.artifact_max_file_bytes,
                                       max_total_bytes=cfg.artifact_max_total_bytes,
                                   ),
                                   trace_max_chars=cfg.trace_max_chars)
                result.attempt = attempt
                result.run_index = run_index
                result.parent_trial_id = group_id
                result.provider = task.provider.provider
                result.model = task.provider.model
                result.estimated_cost_usd = task.provider.pricing.estimate(result.input_tokens, result.output_tokens)
                result.harness_version = cfg.harness_version
                result.environment_type = task.environment_type
                result.grader_type = task.grader_type
            except Exception as exc:
                result = TrialResult(task_id=task.id, trial_id=trial_id, agent_id=task.agent, seed=seed,
                                     status="error", error=str(exc), failure_category="runner_error", attempt=attempt,
                                     run_index=run_index, parent_trial_id=group_id, provider=task.provider.provider,
                                     model=task.provider.model, harness_version=cfg.harness_version,
                                     termination_reason="runner_error")
            finally:
                import shutil
                shutil.rmtree(workspace, ignore_errors=True)
            attempts.append(result)
            if result.success or result.failure_category not in cfg.retry.retryable_categories:
                break
            logger.warning(
                "retrying trial after %s", result.failure_category,
                extra={"event": "trial_retry", "task_id": task.id, "run_index": run_index,
                       "attempt": attempt, "failure_category": result.failure_category},
            )
            if cfg.retry.backoff_seconds:
                time.sleep(cfg.retry.backoff_seconds)
        return attempts

    results: list[TrialResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        for attempts in pool.map(execute, jobs):
            results.extend(attempts)
            for result in attempts:
                manifest.add_trial(result)
    write_report(results, output_dir, dataset=cfg.dataset)
    manifest.write()
    summary = summarize(results)
    logger.info(
        "run finished",
        extra={"event": "run_finished", "run_id": run_id, "total": summary.total,
               "successful": summary.successful, "success_rate": summary.success_rate,
               "total_attempts": summary.total_attempts},
    )
    return results
