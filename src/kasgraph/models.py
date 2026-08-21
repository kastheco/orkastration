"""Typed contracts shared by graph control, persistence, and Orca."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
