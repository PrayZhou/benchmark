from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from .interfaces import AgentAdapter, Environment, Grader
from .manifest import ArtifactStore
from .schemas import AgentResult, TaskSpec, TraceEvent, TrialResult
from .trace import JsonlTraceRecorder

logger = logging.getLogger(__name__)


def run_trial(
    task: TaskSpec,
    environment: Environment,
    agent: AgentAdapter,
    grader: Grader,
    trace_dir: Path,
    trial_id: str | None = None,
    artifact_store: ArtifactStore | None = None,
    trace_max_chars: int = 20_000,
) -> TrialResult:
    trial_id = trial_id or str(uuid.uuid4())
    recorder = JsonlTraceRecorder(trace_dir / f"{trial_id}.jsonl", max_payload_chars=trace_max_chars)
    started = time.monotonic()
    logger.debug("trial started", extra={"event": "trial_start", "trial_id": trial_id, "task_id": task.id})
    agent_result = AgentResult(status="error", error="agent did not finish")
    last_result: dict | None = None
    termination_reason: str | None = None

    try:
        environment.reset(task)
        deadline = started + task.limits.timeout_seconds
        for step in range(task.limits.max_steps):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                agent_result = AgentResult(status="timeout", steps=step, duration_seconds=time.monotonic() - started)
                termination_reason = "timeout"
                recorder.record(TraceEvent(type="termination", step=step, payload={"reason": termination_reason}))
                break
            observation = environment.observe()
            if step > 0 and last_result is not None:
                observation = {**observation, "last_result": last_result}
            recorder.record(TraceEvent(type="observation", step=step, payload=observation))
            action = agent.act(observation)
            recorder.record(TraceEvent(type="action", step=step, payload=action))
            # Pass the remaining budget down so one long command cannot run past
            # the trial deadline: the environment caps it at min(configured, remaining).
            result = environment.execute(action, timeout=remaining)
            last_result = result
            recorder.record(TraceEvent(type="tool_result", step=step, payload=result))
            if agent.finished() or environment.terminal():
                agent_result = agent.result()
                termination_reason = "agent_finished" if agent.finished() else "environment_terminal"
                recorder.record(TraceEvent(type="termination", step=step, payload={"reason": termination_reason}))
                break
        else:
            agent_result = AgentResult(
                status="timeout", steps=task.limits.max_steps,
                duration_seconds=time.monotonic() - started,
            )
            termination_reason = "max_steps"
            recorder.record(TraceEvent(
                type="termination", step=task.limits.max_steps,
                payload={"reason": termination_reason},
            ))

        grader_result = grader.evaluate(task, environment, recorder.events)
        status = "completed" if agent_result.status == "completed" else agent_result.status
        # When the agent itself errored or ran out of time, that is the root
        # cause: the grader only observed an untouched workspace. Reporting its
        # ``test_failure`` would hide the agent failure and suppress retries.
        # A policy violation outranks even that: it is never retryable, so it
        # must stay visible regardless of how the agent ended.
        failure_category: str | None
        if grader_result.failure_category == "policy_violation":
            failure_category = "policy_violation"
        elif grader_result.success:
            failure_category = grader_result.failure_category
        elif agent_result.status in {"error", "timeout"}:
            failure_category = agent_result.status
        else:
            failure_category = grader_result.failure_category or agent_result.status
        collected = (artifact_store.collect(environment.root)
                     if artifact_store is not None and hasattr(environment, "root")
                     else environment.artifacts())
        index_path = artifact_store.write_index() if artifact_store is not None else None
        logger.info(
            "trial finished",
            extra={"event": "trial_finished", "trial_id": trial_id, "task_id": task.id,
                   "status": status, "success": grader_result.success,
                   "termination_reason": termination_reason, "steps": agent_result.steps},
        )
        return TrialResult(
            task_id=task.id,
            trial_id=trial_id,
            agent_id=agent.agent_id,
            seed=task.seed,
            status=status,
            success=grader_result.success,
            score=grader_result.score,
            steps=agent_result.steps,
            tool_calls=agent_result.tool_calls,
            duration_seconds=time.monotonic() - started,
            input_tokens=agent_result.input_tokens,
            output_tokens=agent_result.output_tokens,
            error=agent_result.error,
            failure_category=failure_category,
            evidence=grader_result.evidence,
            violations=grader_result.violations,
            uncertainties=grader_result.uncertainties,
            provider=agent_result.provider,
            model=agent_result.model,
            estimated_cost_usd=agent_result.estimated_cost_usd,
            trace_path=str(trace_dir / f"{trial_id}.jsonl"),
            termination_reason=termination_reason,
            artifacts=collected,
            artifact_index_path=str(index_path) if index_path is not None else None,
        )
    except Exception as exc:
        recorder.record(TraceEvent(type="error", payload={"error": str(exc)}))
        # Log the exception type only: task/agent errors may embed credentials,
        # so the message and traceback stay out of the production log stream.
        logger.error(
            "trial crashed: %s", type(exc).__name__,
            extra={"event": "trial_error", "trial_id": trial_id, "task_id": task.id,
                   "error_type": type(exc).__name__},
        )
        return TrialResult(
            task_id=task.id,
            trial_id=trial_id,
            agent_id=agent.agent_id,
            seed=task.seed,
            status="error",
            error=str(exc),
            failure_category="runner_error",
            duration_seconds=time.monotonic() - started,
            trace_path=str(trace_dir / f"{trial_id}.jsonl"),
            termination_reason="runner_error",
        )
    finally:
        environment.cleanup()
