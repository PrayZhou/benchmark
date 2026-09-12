from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PricingConfig(BaseModel):
    input_per_million: float | None = None
    output_per_million: float | None = None
    cached_input_per_million: float | None = None

    def estimate(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.input_per_million is None or self.output_per_million is None:
            return None
        return input_tokens / 1_000_000 * self.input_per_million + output_tokens / 1_000_000 * self.output_per_million


class ProviderConfig(BaseModel):
    provider: str = "mock"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: float = 60.0
    max_output_tokens: int = 1024
    pass_seed: bool = True
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    extra: dict[str, Any] = Field(default_factory=dict)


class RetryPolicy(BaseModel):
    max_attempts: int = Field(1, ge=1)
    backoff_seconds: float = Field(0.0, ge=0)
    # ``error`` covers agent/model failures (e.g. an intermittent gateway 4xx)
    # that the harness can plausibly recover from on a fresh attempt.
    retryable_categories: list[str] = Field(
        default_factory=lambda: ["runner_error", "environment_error", "timeout", "error"]
    )


class RunConfig(BaseModel):
    runs_per_task: int = Field(1, ge=1)
    concurrency: int = Field(1, ge=1)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    harness_version: str = "0.1.0"
    dataset: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Bounds that keep a hostile or merely chatty task from filling the disk.
    artifact_max_files: int = Field(2000, ge=1)
    artifact_max_file_bytes: int = Field(16 * 1024 * 1024, ge=1)
    artifact_max_total_bytes: int = Field(128 * 1024 * 1024, ge=1)
    trace_max_chars: int = Field(20_000, ge=1)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    instruction: str
    environment: dict[str, Any] = Field(default_factory=dict)
    grader: dict[str, Any] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=lambda: Limits())
    seed: int = 0
    agent: str = "scripted"
    cli: dict[str, Any] = Field(default_factory=dict)
    environment_type: str = "docker"
    grader_type: str = "command"
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Limits(BaseModel):
    max_steps: int = Field(20, ge=1)
    timeout_seconds: float = Field(120.0, gt=0)


class TraceEvent(BaseModel):
    type: Literal["observation", "action", "tool_result", "error", "termination"]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    step: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    status: Literal["completed", "timeout", "error"]
    final_message: str | None = None
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    provider: str | None = None
    model: str | None = None
    estimated_cost_usd: float | None = None


class Finding(BaseModel):
    """One behavioral violation or one confidence caveat.

    ``kind`` is a string rather than a ``Literal`` so task-declared kinds are
    expressible; the built-in vocabulary is documented in ``docs/adapters.md``.
    """

    kind: str
    severity: Literal["low", "medium", "high"] = "medium"
    source: Literal["policy", "judge", "grader"] = "grader"
    evidence: str
    step: int | None = None


class GraderResult(BaseModel):
    success: bool
    score: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    failure_category: str | None = None
    violations: list[Finding] = Field(default_factory=list)
    uncertainties: list[Finding] = Field(default_factory=list)


class TrialResult(BaseModel):
    task_id: str
    trial_id: str
    agent_id: str
    seed: int
    status: str
    success: bool = False
    score: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    failure_category: str | None = None
    evidence: list[str] = Field(default_factory=list)
    violations: list[Finding] = Field(default_factory=list)
    uncertainties: list[Finding] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    artifact_index_path: str | None = None
    attempt: int = 1
    run_index: int = 0
    parent_trial_id: str | None = None
    provider: str | None = None
    model: str | None = None
    estimated_cost_usd: float | None = None
    trace_path: str | None = None
    termination_reason: str | None = None
    harness_version: str = "0.1.0"
    environment_type: str | None = None
    grader_type: str | None = None


class RunSummary(BaseModel):
    """Aggregate over trial *groups*, not raw attempts.

    A retried trial must count once, otherwise every retry dilutes the
    success rate. ``total`` is the number of groups; ``total_attempts``
    keeps the retry volume visible.
    """

    total: int
    completed: int
    successful: int
    success_rate: float
    average_score: float
    average_duration_seconds: float
    failures_by_category: dict[str, int] = Field(default_factory=dict)
    total_attempts: int = 0
    retried_groups: int = 0
    violations_by_kind: dict[str, int] = Field(default_factory=dict)
    uncertain_trials: int = 0
