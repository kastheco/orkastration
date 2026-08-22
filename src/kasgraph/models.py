"""Typed contracts shared by graph control, persistence, and Orca."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GitObjectId = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FindingId = Annotated[str, StringConstraints(pattern=r"^(?:finding|ci-finding)-[a-z0-9-]+$")]


class RoleName(StrEnum):
    """Ordered roles in one execution lane."""

    WORKER = "worker"
    INITIAL_REVIEWER = "initial_reviewer"
    FIXER = "fixer"
    RE_REVIEWER = "re_reviewer"


ROLE_ORDER = (
    RoleName.WORKER,
    RoleName.INITIAL_REVIEWER,
    RoleName.FIXER,
    RoleName.RE_REVIEWER,
)


class LanePhase(StrEnum):
    """Locally observed lane lifecycle."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"


class StagePhase(StrEnum):
    """Locally observed Orca Task lifecycle."""

    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class LaneProposal(BaseModel):
    """One owner-reviewable lane authored by the conversational supervisor."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    issue_id: str = Field(min_length=1, max_length=80)
    repo_selector: str = Field(min_length=1, max_length=256)
    dependencies: list[str] = Field(max_length=32)
    prompt: str = Field(min_length=1, max_length=12_000)
    stop_condition: str = Field(min_length=1, max_length=2_000)


class SupervisorPlan(BaseModel):
    """A typed Codex proposal awaiting explicit owner acceptance."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4_000)
    rationale: str = Field(min_length=1, max_length=8_000)
    next_action: Literal["propose_lanes", "needs_owner", "wait", "complete"]
    owner_question: str | None = Field(max_length=2_000)
    lanes: list[LaneProposal] = Field(max_length=32)

    @model_validator(mode="after")
    def valid_action_and_independent_lanes(self) -> SupervisorPlan:
        """Reject ambiguous graphs before they enter the local ledger."""

        if self.next_action == "propose_lanes" and not self.lanes:
            raise ValueError("propose_lanes requires at least one lane")
        if self.next_action == "needs_owner" and not self.owner_question:
            raise ValueError("needs_owner requires owner_question")
        if self.next_action != "propose_lanes" and self.lanes:
            raise ValueError("only propose_lanes may include lanes")
        names = [lane.name for lane in self.lanes]
        if len(names) != len(set(names)):
            raise ValueError("lane names must be unique")
        issues = [lane.issue_id for lane in self.lanes]
        if len(issues) != len(set(issues)):
            raise ValueError("issue IDs must be unique")
        selected = set(issues)
        for lane in self.lanes:
            overlap = selected.intersection(lane.dependencies)
            if overlap:
                raise ValueError(
                    f"lane {lane.name} depends on another selected lane: {sorted(overlap)}"
                )
        return self


class WorkflowContract(BaseModel):
    """Strict base for data exchanged between workflow agents and Kasgraph."""

    model_config = ConfigDict(extra="forbid")


class ReviewRevision(WorkflowContract):
    """Exact source identity frozen before the initial review."""

    base_sha: GitObjectId
    head_sha: GitObjectId
    diff_sha256: Sha256Digest


class FindingLocation(WorkflowContract):
    """One source location supporting a review claim."""

    path: str = Field(min_length=1, max_length=1_024)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered_lines(self) -> FindingLocation:
        """Reject backwards source ranges."""

        if self.end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        return self


class FindingEvidence(WorkflowContract):
    """A concrete location and the failure claim it proves."""

    location: FindingLocation
    claim: str = Field(min_length=1, max_length=4_000)


class AllowedWriteScope(WorkflowContract):
    """Portable path boundary plus optional adapter-enforced symbols."""

    paths: list[str] = Field(min_length=1, max_length=128)
    symbols: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def unique_entries(self) -> AllowedWriteScope:
        """Keep deterministic boundaries free of duplicate entries."""

        if len(self.paths) != len(set(self.paths)):
            raise ValueError("allowed write paths must be unique")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("allowed write symbols must be unique")
        return self


class ValidationRequirement(WorkflowContract):
    """One deterministic check required by a finding contract."""

    command: str = Field(min_length=1, max_length=2_000)
    expected: str = Field(min_length=1, max_length=2_000)


class ReviewFinding(WorkflowContract):
    """Frozen, issue-scoped contract emitted by the initial reviewer."""

    id: FindingId
    review_revision: ReviewRevision
    evidence: list[FindingEvidence] = Field(min_length=1, max_length=64)
    failure_mode: str = Field(min_length=1, max_length=4_000)
    required_outcome: str = Field(min_length=1, max_length=4_000)
    allowed_write_scope: AllowedWriteScope
    forbidden_scope: list[str] = Field(default_factory=list, max_length=128)
    validation: list[ValidationRequirement] = Field(default_factory=list, max_length=64)
    dependencies: list[FindingId] = Field(default_factory=list, max_length=64)


class InitialReviewReport(WorkflowContract):
    """One full lane review that freezes the complete initial finding set."""

    review_revision: ReviewRevision
    summary: str = Field(min_length=1, max_length=8_000)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def coherent_finding_graph(self) -> InitialReviewReport:
        """Require one revision, unique IDs, known dependencies, and no cycles."""

        by_id = {finding.id: finding for finding in self.findings}
        if len(by_id) != len(self.findings):
            raise ValueError("finding IDs must be unique")
        for finding in self.findings:
            if finding.review_revision != self.review_revision:
                raise ValueError(f"finding {finding.id} uses a different review revision")
            unknown = set(finding.dependencies).difference(by_id)
            if unknown:
                raise ValueError(
                    f"finding {finding.id} depends on unknown finding {sorted(unknown)}"
                )
            if finding.id in finding.dependencies:
                raise ValueError(f"finding {finding.id} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(finding_id: str) -> None:
            if finding_id in visited:
                return
            if finding_id in visiting:
                raise ValueError("finding dependencies must be acyclic")
            visiting.add(finding_id)
            for dependency in by_id[finding_id].dependencies:
                visit(dependency)
            visiting.remove(finding_id)
            visited.add(finding_id)

        for finding_id in by_id:
            visit(finding_id)
        return self


class ValidationResult(WorkflowContract):
    """Observed result of one fixer validation command."""

    command: str = Field(min_length=1, max_length=2_000)
    status: Literal["passed", "failed", "skipped"]
    output: str = Field(default="", max_length=8_000)


class ScopeExpansionRequest(WorkflowContract):
    """Explicit scope the fixer could not safely avoid expanding."""

    paths: list[str] = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4_000)


class FixAttempt(WorkflowContract):
    """Structured outcome from one bounded finding fixer attempt."""

    finding_id: FindingId
    round: int = Field(ge=1, le=2)
    status: Literal["fixed", "blocked_scope", "capability_mismatch"]
    base_sha: GitObjectId
    commit_sha: GitObjectId | None
    changed_paths: list[str] = Field(default_factory=list, max_length=128)
    validation_results: list[ValidationResult] = Field(default_factory=list, max_length=64)
    scope_expansion_required: ScopeExpansionRequest | None

    @model_validator(mode="after")
    def status_matches_artifacts(self) -> FixAttempt:
        """Prevent a status from claiming artifacts it did not produce."""

        if self.status == "fixed" and self.commit_sha is None:
            raise ValueError("a fixed attempt requires commit_sha")
        if self.status == "blocked_scope" and self.scope_expansion_required is None:
            raise ValueError("blocked_scope requires scope_expansion_required")
        if self.status == "capability_mismatch" and (
            self.commit_sha is not None or self.changed_paths
        ):
            raise ValueError("capability_mismatch cannot include a fixer commit or changed paths")
        return self


class ReReviewResult(WorkflowContract):
    """Closed verdict set for one exact fixer commit and finding round."""

    finding_id: FindingId
    round: int = Field(ge=1, le=2)
    reviewed_commit_sha: GitObjectId
    verdict: Literal[
        "resolved",
        "still_open",
        "regression_introduced_by_fix",
        "interaction_failure",
        "blocked",
    ]
    rationale: str = Field(min_length=1, max_length=4_000)
    evidence: list[FindingEvidence] = Field(default_factory=list, max_length=64)


class EscalationDecision(WorkflowContract):
    """Bounded adjudication after scope escape, ambiguity, or round exhaustion."""

    finding_id: FindingId
    round: int = Field(ge=1, le=2)
    reason: Literal["scope_escape", "ambiguous_result", "rounds_exhausted"]
    action: Literal["approve_scope_revision", "defer", "block"]
    rationale: str = Field(min_length=1, max_length=4_000)
    revised_finding: ReviewFinding | None = None

    @model_validator(mode="after")
    def revised_contract_matches_action(self) -> EscalationDecision:
        """Require a replacement contract only for an approved scope revision."""

        if (self.action == "approve_scope_revision") != (self.revised_finding is not None):
            raise ValueError("approve_scope_revision requires exactly one revised_finding")
        return self


class CiCheckResult(WorkflowContract):
    """One remote check observed for an exact published revision."""

    name: str = Field(min_length=1, max_length=512)
    status: Literal["pending", "passed", "failed", "cancelled", "skipped"]
    details_url: str | None = Field(default=None, max_length=2_048)
    output: str = Field(default="", max_length=8_000)


class CiFailureFinding(WorkflowContract):
    """Narrow finding derived from a failed check on the integrated fix set."""

    id: FindingId
    published_sha: GitObjectId
    failing_checks: list[CiCheckResult] = Field(min_length=1, max_length=64)
    implicated_fix_commits: list[GitObjectId] = Field(default_factory=list, max_length=128)
    allowed_write_scope: AllowedWriteScope
    round: int = Field(ge=1, le=2)


class PublicationReceipt(WorkflowContract):
    """Auditable external mutations authorized by one accepted run."""

    run_id: str = Field(min_length=1, max_length=128)
    lane: str = Field(min_length=1, max_length=64)
    remote_url: str = Field(min_length=1, max_length=2_048)
    branch: str = Field(min_length=1, max_length=512)
    pull_request_url: str = Field(min_length=1, max_length=2_048)
    head_sha: GitObjectId
    draft: bool


class CiReceipt(WorkflowContract):
    """Final-gate result pinned to the published head revision."""

    provider: Literal["github"]
    head_sha: GitObjectId
    status: Literal["pending", "passed", "failed"]
    checks: list[CiCheckResult] = Field(default_factory=list, max_length=256)


def workflow_contract_schemas() -> dict[str, dict[str, object]]:
    """Return stable names and generated JSON Schemas for agent-facing results."""

    contracts: tuple[tuple[str, type[WorkflowContract]], ...] = (
        ("initial_review_report", InitialReviewReport),
        ("fix_attempt", FixAttempt),
        ("re_review_result", ReReviewResult),
        ("escalation_decision", EscalationDecision),
        ("ci_failure_finding", CiFailureFinding),
        ("publication_receipt", PublicationReceipt),
        ("ci_receipt", CiReceipt),
    )
    return {name: contract.model_json_schema() for name, contract in contracts}


class OrcaWorktree(BaseModel):
    """The stable subset of an Orca worktree process snapshot."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    worktree_id: str = Field(alias="worktreeId")
    repo_id: str = Field(alias="repoId")
    repo: str
    path: str
    display_name: str = Field(alias="displayName")
    workspace_status: str = Field(alias="workspaceStatus")
    status: str
    linked_linear_issue: str | None = Field(default=None, alias="linkedLinearIssue")
    live_terminal_count: int = Field(default=0, alias="liveTerminalCount")


class OrcaSnapshot(BaseModel):
    """Read-only Orca worktree state."""

    model_config = ConfigDict(extra="forbid")

    worktrees: list[OrcaWorktree]
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LaneRecord(BaseModel):
    """Persisted local correlation for one lane."""

    model_config = ConfigDict(extra="forbid")

    lane_id: str
    run_id: str
    name: str
    issue_id: str
    repo_selector: str
    phase: LanePhase
    worktree_id: str | None
    created_at: datetime
    updated_at: datetime


class StageRecord(BaseModel):
    """Persisted correlation between a lane role and an Orca Task/Dispatch."""

    model_config = ConfigDict(extra="forbid")

    stage_id: str
    lane_id: str
    role: RoleName
    phase: StagePhase
    orca_task_id: str | None
    orca_dispatch_id: str | None
    released: bool
    created_at: datetime
    updated_at: datetime


class RunRecord(BaseModel):
    """Persisted proposal and its optional accepted Orca Run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    orca_run_id: str | None
    proposal: SupervisorPlan
    created_at: datetime
    updated_at: datetime


class ProposalReceipt(BaseModel):
    """Result of recording, but not accepting, a graph proposal."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str = "proposed"
    proposal: SupervisorPlan


class StageLaunch(BaseModel):
    """One Orca Dispatch started by acceptance or monitoring."""

    model_config = ConfigDict(extra="forbid")

    lane: str
    role: RoleName
    task_id: str
    dispatch_id: str
    worktree_id: str


class GraphResult(BaseModel):
    """Machine-readable result of accepting or monitoring a graph."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    orca_run_id: str
    status: str
    started: list[StageLaunch] = Field(default_factory=list)
    lanes: list[LaneRecord]
    stages: list[StageRecord]
