"""Strict contracts for the offline delivery comparison harness."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1"


class StrictModel(BaseModel):
    """Base model which rejects accidental protocol expansion."""

    model_config = ConfigDict(extra="forbid")


class AdapterManifest(StrictModel):
    id: str
    protocol_version: Literal["1"] = "1"
    description: str
    ready: bool
    argv: list[str]
    environment: dict[str, str] = Field(default_factory=dict)
    workflow_requirement: str

    @model_validator(mode="after")
    def require_argv_when_ready(self) -> AdapterManifest:
        if self.ready and not self.argv:
            raise ValueError("ready adapters require argv")
        return self


class TaskManifest(StrictModel):
    id: str
    capability: Literal["clean_bugfix", "hidden_edge_repair", "crash_redelivery"]
    instruction: str
    setup_argv: list[str]
    reset_strategy: Literal["fresh_copy_and_git_init"]
    visible_test_argv: list[str]
    allowed_write_paths: list[str]
    protected_paths: list[str]
    expected_final_behavior: list[str]
    accepted_equivalent_outcomes: list[str]
    fault_point: str | None = None


class HiddenTaskTruth(StrictModel):
    verifier_argv: list[str]
    expected_files_sha256: dict[str, str]
    reject_unexpected_paths: bool = True


class AdapterMetrics(StrictModel):
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    supervisor_turns: int = Field(default=0, ge=0)
    human_interruptions: int = Field(default=0, ge=0)
    reviewer_calls: int = Field(default=0, ge=0)
    fixer_calls: int = Field(default=0, ge=0)


class AdapterResultBundle(StrictModel):
    schema_version: Literal["1"] = "1"
    trial_id: str
    adapter_id: str
    task_id: str
    status: Literal["completed", "failed", "crashed"]
    summary: str
    metrics: AdapterMetrics = Field(default_factory=AdapterMetrics)
    infrastructure_error: str | None = None


class TelemetryEvent(StrictModel):
    schema_version: Literal["1"] = "1"
    sequence: int = Field(ge=0)
    event: Literal[
        "dispatch",
        "action",
        "commit",
        "ack",
        "crash",
        "redelivery",
        "lost_committed_work",
        "review",
        "repair",
    ]
    action_id: str | None = None
    detail: str = ""


class ProcessEvidence(StrictModel):
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    wall_time_seconds: float
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    launch_error: str | None = None


class VerifierEvidence(StrictModel):
    behavior_passed: bool
    exact_tree_passed: bool
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    changed_paths: list[str]
    unexpected_paths: list[str]
    hash_mismatches: list[str]
    infrastructure_error: str | None = None


class TrialResult(StrictModel):
    schema_version: Literal["1"] = "1"
    trial_id: str
    task_id: str
    adapter_id: str
    success: bool
    classification: Literal[
        "success",
        "agent_failure",
        "adapter_crash",
        "adapter_timeout",
        "adapter_protocol_failure",
        "infrastructure_failure",
    ]
    verifier: VerifierEvidence
    wall_time_seconds: float
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    supervisor_turns: int
    human_interruptions: int
    reviewer_calls: int
    fixer_calls: int
    duplicate_action_ids: list[str]
    lost_committed_work: bool
    crash_recovery: bool
    scope_violations: list[str]
    infrastructure_error: str | None
    adapter_process: ProcessEvidence
    bundle_error: str | None = None


class AggregateMetrics(StrictModel):
    adapter_id: str
    trials: int
    successes: int
    median_wall_time_seconds: float
    median_model_calls: float
    median_tokens: float
    median_cost_usd: float
    median_supervisor_turns: float
    total_human_interruptions: int
    total_reviewer_calls: int
    total_fixer_calls: int
    duplicate_action_trials: int
    lost_work_trials: int
    crash_recovery_trials: int
    scope_violation_trials: int
    infrastructure_failure_trials: int


class AdapterDelta(StrictModel):
    adapter_a: str
    adapter_b: str
    success_delta: int
    median_wall_time_seconds_delta: float
    median_supervisor_turns_delta: float
    crash_recovery_trials_delta: int


class ComparisonReport(StrictModel):
    schema_version: Literal["1"] = "1"
    trials: list[TrialResult]
    aggregates: list[AggregateMetrics]
    deltas: list[AdapterDelta]
