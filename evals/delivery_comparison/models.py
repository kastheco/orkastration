"""Strict contracts for the offline delivery comparison harness."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = "1"
ComparisonMode = Literal["tuned-primary", "matched-role-ablation", "sol-high-diagnostic"]
InfrastructureCode = Literal[
    "service_unavailable",
    "rate_limited",
    "authentication_unavailable",
    "quota_exhausted",
    "containment_failure",
    "worktree_failure",
]
HostInfrastructureCode = Literal[
    "adapter_launch_failure",
    "initial_phase_launch_failure",
    "verifier_failure",
]


class StrictModel(BaseModel):
    """Base model which rejects accidental protocol expansion."""

    model_config = ConfigDict(extra="forbid")


class ContainmentSpec(StrictModel):
    backend: Literal["none"]
    filesystem_isolation: Literal[False]
    evidence: str = Field(max_length=500)


class ModelRoute(StrictModel):
    model: str = Field(min_length=1)
    thinking: Literal["off", "low", "medium", "high"]


class EvaluationBudget(StrictModel):
    max_total_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0)
    max_wall_seconds: int = Field(gt=0)


class AdapterManifest(StrictModel):
    id: str
    protocol_version: Literal["1"] = "1"
    description: str
    ready: bool
    argv: list[str]
    environment: dict[str, str] = Field(default_factory=dict)
    workflow_requirement: str
    containment: ContainmentSpec
    comparison_mode: ComparisonMode
    model_role_map: dict[str, ModelRoute]
    allowed_model_pool: list[str]
    budget: EvaluationBudget
    tuning_budget_hours: float = Field(ge=0)
    config_digest: str
    calibration_scenario: str | None = None

    @model_validator(mode="after")
    def validate_readiness_and_configuration(self) -> AdapterManifest:
        if self.ready:
            raise ValueError("live readiness is disabled: no harness-owned containment launcher")
        if not self.model_role_map:
            raise ValueError("model_role_map cannot be empty")
        routed_models = {route.model for route in self.model_role_map.values()}
        if not routed_models <= set(self.allowed_model_pool):
            raise ValueError("every routed model must be in allowed_model_pool")
        if len(self.config_digest) != 64 or not re.fullmatch(r"[0-9a-f]{64}", self.config_digest):
            raise ValueError("config_digest must be lowercase SHA-256")
        secret_terms = ("token", "secret", "password", "api_key", "apikey", "credential")
        if any(any(term in key.lower() for term in secret_terms) for key in self.environment):
            raise ValueError("adapter environment may not contain credential-shaped keys")
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


class AdapterInfrastructure(StrictModel):
    code: InfrastructureCode
    evidence: str = Field(min_length=1, max_length=500)


class AdapterResultBundle(StrictModel):
    schema_version: Literal["1"] = "1"
    trial_id: str
    adapter_id: str
    task_id: str
    status: Literal["completed", "failed", "crashed"]
    summary: str = Field(max_length=2000)
    metrics: AdapterMetrics = Field(default_factory=AdapterMetrics)
    infrastructure: AdapterInfrastructure | None = None


_ACTION_EVENTS = {
    "dispatch",
    "action",
    "commit",
    "ack",
    "crash",
    "redelivery",
    "lost_committed_work",
}


class TelemetryEvent(StrictModel):
    schema_version: Literal["1"] = "1"
    trial_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
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
    detail: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def require_action_identity(self) -> TelemetryEvent:
        if self.event in _ACTION_EVENTS:
            if not (self.action_id and self.action_id.strip()):
                raise ValueError(f"{self.event} requires nonempty action_id")
            normalized = self.action_id.strip()
            if len(normalized) > 128:
                raise ValueError(f"{self.event} action_id exceeds 128 characters")
            self.action_id = normalized
        return self


BoundedIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class DispatchHandshake(StrictModel):
    schema_version: Literal["1"] = "1"
    trial_id: str
    adapter_id: str
    task_id: str
    action_id: BoundedIdentifier


class RecoveryHandshake(StrictModel):
    schema_version: Literal["1"] = "1"
    trial_id: str
    adapter_id: str
    task_id: str
    action_id: BoundedIdentifier


ReleaseNonce = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=32, max_length=256),
]


class AckHandshake(StrictModel):
    schema_version: Literal["1"] = "1"
    trial_id: str
    adapter_id: str
    task_id: str
    action_id: BoundedIdentifier
    release_nonce: ReleaseNonce


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


class FaultInjectionEvidence(StrictModel):
    requested: bool
    handshake_valid: bool
    process_interrupted: bool
    action_id: str | None
    initial_process: ProcessEvidence | None
    error: str | None = Field(default=None, max_length=1000)


class ExternalEffectEvidence(StrictModel):
    redelivery_observed: bool
    effect_count: int = Field(ge=0)
    commit_count: int = Field(ge=0)
    action_id: str | None
    commit_sha: str | None
    release_nonce_published: bool
    ack_observed: bool
    error: str | None = Field(default=None, max_length=1000)


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
    comparison_mode: ComparisonMode
    model_role_map: dict[str, ModelRoute]
    allowed_model_pool: list[str]
    budget: EvaluationBudget
    tuning_budget_hours: float
    config_digest: str
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
    crash_chain_error: str | None
    fault_injection: FaultInjectionEvidence
    external_effect: ExternalEffectEvidence
    scope_violations: list[str]
    budget_violations: list[str]
    infrastructure_code: HostInfrastructureCode | None
    infrastructure_error: str | None
    adapter_process: ProcessEvidence
    bundle_status: Literal["completed", "failed", "crashed"] | None
    bundle_error: str | None = None


class AggregateMetrics(StrictModel):
    adapter_id: str
    comparison_mode: ComparisonMode
    config_digest: str
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
    comparison_mode: ComparisonMode
    adapter_a: str
    adapter_b: str
    success_delta: int
    median_wall_time_seconds_delta: float
    median_supervisor_turns_delta: float
    crash_recovery_trials_delta: int


class ComparisonReport(StrictModel):
    schema_version: Literal["1"] = "1"
    comparison_mode: ComparisonMode
    trials: list[TrialResult]
    aggregates: list[AggregateMetrics]
    deltas: list[AdapterDelta]
