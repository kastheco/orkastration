"""Typed contracts shared by graph control, persistence, and Orca."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GitObjectId = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
TranscribedGitObjectId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
"""A sha as an agent wrote it down, on a field the supervisor resolves from Git anyway.

An agent is not the authority on any sha here: the supervisor chose the base and Git
holds the head, and both are overwritten before anything reads them. Enforcing the exact
object-id shape at parse therefore buys nothing and costs the whole report, because a
report that fails to parse takes the commit it was describing with it. Run 40c115ec lost
a correct, scoped, fully green fix over the seven characters `56f39ff`. Accept what was
written and let Git settle what it meant.
"""
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FindingId = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:finding|ci-finding|worker-decision|publication-conflict)-[a-z0-9-]+$"
    ),
]


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


TERMINAL_LANE_PHASES = frozenset({LanePhase.BLOCKED, LanePhase.COMPLETE, LanePhase.FAILED})
"""Lane phases nothing in the graph moves a lane out of on its own.

A blocked lane is as finished as a complete one from the graph's side: only an
owner reopening a finding restarts it. Naming the set once keeps a caller asking
whether a run still has work from having to restate which phases those are.
"""


class StagePhase(StrEnum):
    """Locally observed Orca Task lifecycle."""

    PENDING = "pending"
    READY = "ready"
    STARTING = "starting"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class StageKind(StrEnum):
    """Dynamic workflow work item scheduled inside one lane."""

    WORKER = "worker"
    INITIAL_REVIEWER = "initial_reviewer"
    FIXER = "fixer"
    RE_REVIEWER = "re_reviewer"
    ESCALATION = "escalation"


class AttemptKind(StrEnum):
    """Which configured profile owns a fixer attempt."""

    PRIMARY = "primary"
    FALLBACK = "fallback"


class FindingPhase(StrEnum):
    """Persisted convergence state for one frozen finding."""

    PENDING_FIX = "pending_fix"
    FIXING = "fixing"
    PENDING_RE_REVIEW = "pending_re_review"
    RE_REVIEWING = "re_reviewing"
    PENDING_COMPOSITE = "pending_composite"
    PENDING_ESCALATION = "pending_escalation"
    ESCALATING = "escalating"
    RESOLVED = "resolved"
    DEFERRED = "deferred"
    BLOCKED = "blocked"


class FindingReason(StrEnum):
    """Persisted reason for fallback or escalation routing."""

    CAPABILITY_FALLBACK = "capability_fallback"
    SCOPE_ESCAPE = "scope_escape"
    AMBIGUOUS_RESULT = "ambiguous_result"
    ROUNDS_EXHAUSTED = "rounds_exhausted"
    INTEGRATION_CONFLICT = "integration_conflict"
    VALIDATION_FAILED = "validation_failed"
    WORKER_DECISION = "worker_decision"


class LaneProposal(BaseModel):
    """One owner-reviewable lane authored by the conversational supervisor."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    issue_id: str = Field(min_length=1, max_length=80)
    repo_selector: str = Field(min_length=1, max_length=256)
    base_ref: str = Field(min_length=1, max_length=512)
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
    """Strict base for data exchanged between workflow agents and orkastrator."""

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
        for path in self.paths:
            if (
                path.startswith("/")
                or ".." in path.split("/")
                or any(marker in path for marker in ("*", "?", "["))
            ):
                raise ValueError("allowed write paths must be relative literal boundaries")
        return self


SHELL_OPERATORS = ("&&", "||", "|", ";", ">", "<", "$(", "`")
"""Syntax only a shell resolves, which the validation runner never provides."""


class ValidationResult(WorkflowContract):
    """Observed result of one validation command."""

    command: str = Field(min_length=1, max_length=2_000)
    status: Literal["passed", "failed", "skipped"]
    output: str = Field(default="", max_length=8_000)


class ValidationRequirement(WorkflowContract):
    """One deterministic check required by a finding contract."""

    command: str = Field(min_length=1, max_length=2_000)
    expected: str = Field(min_length=1, max_length=2_000)
    workdir: str | None = Field(default=None, max_length=500)
    """Directory under the worktree to run the command in, in place of `cd`."""
    expect_exit: int = Field(default=0, ge=0, le=255)
    """Exit status that satisfies this check.

    An absence check passes by failing: `rg PATTERN path` exits 1 when the
    pattern is gone, which is exactly the outcome a finding about a removed
    symbol requires. Without this the reviewer can only write the command and
    say what it means in prose, and the runner reads the prose as a failure.
    """
    baseline_result: ValidationResult | None = None
    """Supervisor-observed result against the unmodified reviewed head."""

    @model_validator(mode="after")
    def _contained_workdir(self) -> ValidationRequirement:
        """Keep a requirement's working directory inside its own worktree."""

        if self.workdir is not None:
            workdir = PurePosixPath(self.workdir)
            if workdir.is_absolute() or ".." in workdir.parts:
                raise ValueError("workdir must be a relative path inside the worktree")
        return self


class ReviewFinding(WorkflowContract):
    """Frozen, issue-scoped contract emitted by the initial reviewer."""

    id: FindingId
    review_revision: ReviewRevision | None = None
    """Absent means the revision the supervisor binds this finding to.

    An agent that retypes a 64-character digest sometimes elides its middle, and
    that cost a whole finding a terminal block. The supervisor already knows the
    frozen revision, so let a contract omit it and be stamped rather than demand
    a transcription no agent is the authority on.
    """

    evidence: list[FindingEvidence] = Field(min_length=1, max_length=64)
    failure_mode: str = Field(min_length=1, max_length=4_000)
    required_outcome: str = Field(min_length=1, max_length=4_000)
    allowed_write_scope: AllowedWriteScope
    forbidden_scope: list[str] = Field(default_factory=list, max_length=128)
    validation: list[ValidationRequirement] = Field(default_factory=list, max_length=64)
    dependencies: list[FindingId] = Field(default_factory=list, max_length=64)


class InitialReviewReport(WorkflowContract):
    """One full lane review that freezes the complete initial finding set."""

    review_revision: ReviewRevision | None = None
    """Absent means the frozen worker revision the supervisor binds this to."""

    summary: str = Field(min_length=1, max_length=8_000)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def coherent_finding_graph(self) -> InitialReviewReport:
        """Require one revision, unique IDs, known dependencies, and no cycles."""

        by_id = {finding.id: finding for finding in self.findings}
        if len(by_id) != len(self.findings):
            raise ValueError("finding IDs must be unique")
        for finding in self.findings:
            named = finding.review_revision
            if (
                named is not None
                and self.review_revision is not None
                and named != self.review_revision
            ):
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


class WorkerResult(WorkflowContract):
    """Exact committed changeset and validation evidence produced by a lane worker."""

    status: Literal["committed"] = "committed"
    review_revision: ReviewRevision
    commit_sha: GitObjectId
    changed_paths: list[str] = Field(default_factory=list, max_length=256)
    validation_results: list[ValidationResult] = Field(default_factory=list, max_length=64)
    summary: str = Field(min_length=1, max_length=8_000)

    @model_validator(mode="after")
    def commit_matches_review_head(self) -> WorkerResult:
        """Pin the review input to the exact worker commit."""

        if self.commit_sha != self.review_revision.head_sha:
            raise ValueError("worker commit_sha must equal review_revision.head_sha")
        return self


class WorkerDecision(WorkflowContract):
    """One decision a lane worker cannot take from its own contract."""

    question: str = Field(min_length=1, max_length=4_000)
    options: list[str] = Field(min_length=2, max_length=8)
    consequence: str = Field(min_length=1, max_length=4_000)
    allowed_write_scope: AllowedWriteScope


class WorkerBlocked(WorkflowContract):
    """A lane worker that stopped on a decision its contract does not answer.

    The graph routes a decision nobody in the lane can take, so a worker that
    needs one ends its stage with this instead of waiting on a human. Asking
    out of band leaves the stage dispatched and the decision uncontracted.
    """

    status: Literal["blocked"]
    base_sha: GitObjectId
    head_sha: GitObjectId
    summary: str = Field(min_length=1, max_length=8_000)
    decision: WorkerDecision


class ScopeExpansionRequest(WorkflowContract):
    """Explicit scope the fixer could not safely avoid expanding."""

    paths: list[str] = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4_000)


class FixAttempt(WorkflowContract):
    """Structured outcome from one bounded finding fixer attempt."""

    finding_id: FindingId
    round: int = Field(ge=1, le=2)
    status: Literal["fixed", "blocked_scope", "capability_mismatch"]
    base_sha: TranscribedGitObjectId
    commit_sha: TranscribedGitObjectId | None
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


class ReReviewFinding(WorkflowContract):
    """A finding noticed during re-review with an explicit convergence disposition."""

    origin: Literal["introduced_by_fix", "unrelated"]
    finding: ReviewFinding


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
    new_findings: list[ReReviewFinding] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def new_findings_match_verdict(self) -> ReReviewResult:
        """Only regression verdicts may introduce a finding into this review run."""

        ids = [item.finding.id for item in self.new_findings]
        if len(ids) != len(set(ids)):
            raise ValueError("new re-review finding IDs must be unique")
        if self.finding_id in ids:
            raise ValueError("a re-review finding must not reuse the original finding ID")
        introduced = [item for item in self.new_findings if item.origin == "introduced_by_fix"]
        if introduced and self.verdict not in {
            "regression_introduced_by_fix",
            "interaction_failure",
        }:
            raise ValueError("introduced findings require a regression or interaction verdict")
        return self


class EscalationDecision(WorkflowContract):
    """Bounded adjudication after scope escape, ambiguity, or round exhaustion."""

    finding_id: FindingId
    round: int = Field(ge=1, le=2)
    reason: Literal[
        "scope_escape",
        "ambiguous_result",
        "rounds_exhausted",
        "integration_conflict",
        "validation_failed",
        "worker_decision",
    ]
    action: Literal["accept_fix", "approve_unchanged", "approve_scope_revision", "defer", "block"]
    """Four outcomes an adjudicator reaches, plus the one it used to be forced into.

    accept_fix settles the finding on the fix already committed: the escalation
    was about the evidence, not the work, and the adjudicator verified the work
    itself. approve_unchanged says the finding stands exactly as frozen and wants
    another attempt. Both used to come out as block, because an adjudicator that
    agreed with a finding had no other word available, so it killed live work in
    a rationale that approved it.
    """

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


class AcceptanceAuthorization(WorkflowContract):
    """Exact proposal and policy identity authorized by graph acceptance."""

    run_id: str = Field(min_length=1, max_length=128)
    proposal_sha256: Sha256Digest
    config_sha256: Sha256Digest


class ConfigChange(WorkflowContract):
    """One changed leaf between the accepted policy and the policy on disk.

    Values are rendered as compact JSON rather than kept typed, because this
    exists to be read by an owner deciding whether to authorize the change.
    `final_gate.advisory_checks: [] -> ["conformance"]` is a sentence somebody
    can judge; two digests are not.
    """

    path: str = Field(min_length=1, max_length=512)
    before: str = Field(max_length=2_048)
    after: str = Field(max_length=2_048)


class PolicyReauthorization(WorkflowContract):
    """What `reauthorize` was asked to do, what it read, and whether it did it."""

    authorization: AcceptanceAuthorization
    changes: list[ConfigChange] = Field(default_factory=list, max_length=512)
    # False when the accepted run predates the stored config payload, so the
    # change cannot be read - only the digests differ. Saying so is the point:
    # an empty `changes` list would otherwise claim nothing moved.
    comparable: bool = True
    applied: bool = False


class PublicationReceipt(WorkflowContract):
    """Auditable external mutations authorized by one accepted run."""

    run_id: str = Field(min_length=1, max_length=128)
    lane: str = Field(min_length=1, max_length=64)
    remote_url: str = Field(min_length=1, max_length=2_048)
    base_branch: str = Field(min_length=1, max_length=512)
    branch: str = Field(min_length=1, max_length=512)
    pull_request_url: str = Field(min_length=1, max_length=2_048)
    head_sha: GitObjectId
    draft: bool
    # The branch reached the base branch, which is the outcome the lane was
    # working toward. A landed receipt is terminal: there is nothing left to
    # push, edit, or observe, because the head's checks belong to the base
    # branch's history now. Defaults false so receipts written before this
    # field existed still read.
    landed: bool = False
    # The branch revision GitHub says was merged. This normally equals
    # `head_sha`, but remains separate because a maintainer may advance the
    # pull-request branch after orkastrator publishes its frozen revision.
    merged_head_sha: GitObjectId | None = None
    # The merge commit created when orkastrator lands the lane. Older receipts
    # and externally merged pull requests can be landed without this value, but
    # an automated landing always records it.
    merge_sha: GitObjectId | None = None

    @model_validator(mode="after")
    def merge_commit_requires_landing(self) -> PublicationReceipt:
        if self.merge_sha is not None and not self.landed:
            raise ValueError("a publication merge sha requires a landed receipt")
        if self.merged_head_sha is not None and not self.landed:
            raise ValueError("a publication merged head requires a landed receipt")
        return self


class CiReceipt(WorkflowContract):
    """Final-gate result pinned to the published head revision."""

    provider: Literal["github"]
    head_sha: GitObjectId
    status: Literal["pending", "passed", "failed"]
    checks: list[CiCheckResult] = Field(default_factory=list, max_length=256)


class OrcaWorkerResult(BaseModel):
    """Validated lifecycle result persisted by Orca after ``worker_done``."""

    model_config = ConfigDict(extra="ignore")

    provenance: Literal["worker_report"]
    outcome: Literal["succeeded", "failed"]
    message_id: str = Field(alias="messageId", min_length=1)
    reported_by: str = Field(alias="reportedBy", min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    completed_by: str = Field(alias="completedBy", min_length=1)
    files_modified: list[str] = Field(default_factory=list, alias="filesModified")
    report_path: str | None = Field(default=None, alias="reportPath")
    completed_at: datetime = Field(alias="completedAt")


def workflow_contract_schemas() -> dict[str, dict[str, object]]:
    """Return stable names and generated JSON Schemas for agent-facing results."""

    contracts: tuple[tuple[str, type[WorkflowContract]], ...] = (
        ("worker_result", WorkerResult),
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
    base_ref: str
    base_sha: GitObjectId | None = None
    phase: LanePhase
    worktree_id: str | None
    review_head_sha: GitObjectId | None
    integration_head_sha: GitObjectId | None
    created_at: datetime
    updated_at: datetime


class StageRecord(BaseModel):
    """Persisted correlation between a lane role and an Orca Task/Dispatch."""

    model_config = ConfigDict(extra="forbid")

    stage_id: str
    stage_key: str
    lane_id: str
    role: StageKind
    finding_id: str | None
    round: int | None
    attempt_kind: AttemptKind | None
    phase: StagePhase
    orca_task_id: str | None
    orca_dispatch_id: str | None
    # Set only when orkastrator opened the agent terminal itself; that is the
    # one case where releasing the Dispatch reclaims nothing on its own.
    orca_terminal_handle: str | None = None
    worktree_id: str | None
    # The worktree head this dispatch started from. Only a stage orkastrator
    # launched itself has one; an adopted stage was already running when it was
    # found, so there is no before to record.
    start_head_sha: str | None = None
    result_json: str | None
    processed: bool
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
    role: StageKind
    task_id: str
    dispatch_id: str
    worktree_id: str


class FindingRecord(BaseModel):
    """Persisted immutable finding contract plus its current convergence state."""

    model_config = ConfigDict(extra="forbid")

    finding_key: str
    lane_id: str
    finding_id: str
    origin: Literal[
        "initial_review",
        "introduced_by_fix",
        "unrelated",
        "ci_failure",
        "worker_blocked",
        "publication_conflict",
    ]
    contract: ReviewFinding
    effective_contract: ReviewFinding
    phase: FindingPhase
    round: int
    escalation_reason: FindingReason | None
    # None means "the frozen review revision", which is where every fix starts.
    # It moves only when a conflict retry has to be rebuilt on the lane head.
    dispatch_base_sha: GitObjectId | None = None
    created_at: datetime
    updated_at: datetime


class IntegrationConflictContext(BaseModel):
    """Git evidence captured before a failed integration is aborted."""

    model_config = ConfigDict(extra="forbid")

    conflicted_paths: list[str] = Field(min_length=1, max_length=256)
    cleanly_applied_paths: list[str] = Field(max_length=256)
    conflicted_hunks: str | None = Field(default=None, min_length=1)


class IntegrationRecord(BaseModel):
    """Auditable serial integration state for one approved finding commit."""

    model_config = ConfigDict(extra="forbid")

    integration_id: str
    finding_key: str
    lane_id: str
    round: int
    fixer_commit_sha: GitObjectId
    source_commits: list[GitObjectId]
    source_finding_ids: list[FindingId]
    base_sha: GitObjectId
    integrated_sha: GitObjectId | None
    status: Literal["starting", "integrated", "conflict", "validation_failed"]
    validation_results: list[ValidationResult]
    conflict_context: IntegrationConflictContext | None = None
    created_at: datetime
    updated_at: datetime


class FixAttemptIdentity(BaseModel):
    """What a fixer commit is, and what range it carries, as one value.

    These four move together or not at all: a commit implies its base, and the
    source range and the findings it represents are derived from both. Passing
    them separately is how a receipt ends up describing one attempt's commit over
    another attempt's range.
    """

    model_config = ConfigDict(extra="forbid")

    fixer_commit_sha: GitObjectId
    base_sha: GitObjectId
    source_commits: list[GitObjectId]
    source_finding_ids: list[FindingId]


class PendingQuestion(BaseModel):
    """One unanswered agent question or escalation raised against a live stage."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    kind: Literal["question", "escalation"]
    from_handle: str
    lane: str | None
    role: StageKind | None
    dispatch_id: str | None
    asked_at: str
    subject: str
    body: str


class StageClock(BaseModel):
    """How long one stage's current dispatch has been running, and what it has been told.

    `warned` and `timeouts` are scoped to this dispatch. A re-dispatched stage
    starts a fresh clock, because the question a budget asks is about the agent
    now running, not about the stage's whole history.
    """

    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    warned: bool = False
    timeouts: int = 0


class OverdueStage(BaseModel):
    """One dispatched stage that has outlived its configured budget."""

    model_config = ConfigDict(extra="forbid")

    stage_key: str
    lane: str
    role: StageKind
    minutes: int
    budget: Literal["soft", "hard"]
    # What the stage was doing when it went overdue. Minutes alone cannot tell a
    # slow stage from a wedged one, and that is the only question an owner
    # reading an overdue line actually has. None when the worker could not be
    # read, which is not the same as "nothing to say".
    activity: str | None = None


class GraphResult(BaseModel):
    """Machine-readable result of accepting or monitoring a graph."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    orca_run_id: str
    status: str
    exit_reason: Literal["single_tick", "unanswered_question", "terminal_graph"] | None = None
    started: list[StageLaunch] = Field(default_factory=list)
    lanes: list[LaneRecord]
    stages: list[StageRecord]
    findings: list[FindingRecord]
    publications: list[PublicationReceipt] = Field(default_factory=list)
    ci: list[CiReceipt] = Field(default_factory=list)
    questions: list[PendingQuestion] = Field(default_factory=list)
    overdue: list[OverdueStage] = Field(default_factory=list)
