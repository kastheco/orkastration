"""Typed contracts shared by the planner, store, and Orca adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelTier(StrEnum):
    """Provider-independent model effort tier."""

    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"


class LanePhase(StrEnum):
    """Locally observed lane lifecycle."""

    PROPOSED = "proposed"
    STARTING = "starting"
    ACTIVE = "active"
    REVIEW = "review"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"


class LaneProposal(BaseModel):
    """One proposed Orca-owned agent lane."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    issue_id: str = Field(min_length=1, max_length=80)
    repo_selector: str = Field(min_length=1, max_length=256)
    agent_id: Literal["codex", "claude", "omp", "pi", "grok"] = "codex"
    role: Literal["implementer", "reviewer", "researcher"]
    model_tier: ModelTier = ModelTier.STANDARD
    can_run_parallel: bool = False
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    prompt: str = Field(min_length=1, max_length=8_000)
    stop_condition: str = Field(min_length=1, max_length=1_000)


class SupervisorPlan(BaseModel):
    """A proposal from the model; deterministic code still validates it."""

    model_config = ConfigDict(extra="forbid")

    rationale: str = Field(min_length=1, max_length=4_000)
    next_action: Literal["start_lane", "wait", "needs_owner", "complete"]
    selected_lane_name: str | None = None
    owner_question: str | None = Field(default=None, max_length=2_000)
    lanes: list[LaneProposal] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def selected_lane_matches_action(self) -> SupervisorPlan:
        """Require exactly the fields needed by the selected action."""

        names = [lane.name for lane in self.lanes]
        if len(names) != len(set(names)):
            raise ValueError("lane names must be unique")
        if self.next_action == "start_lane":
            if self.selected_lane_name is None:
                raise ValueError("start_lane requires selected_lane_name")
            if self.selected_lane_name not in names:
                raise ValueError("selected_lane_name must identify a proposed lane")
        elif self.selected_lane_name is not None:
            raise ValueError("selected_lane_name is valid only for start_lane")
        if self.next_action == "needs_owner" and not self.owner_question:
            raise ValueError("needs_owner requires owner_question")
        return self

    def selected_lane(self) -> LaneProposal | None:
        """Return the selected proposal, when the action starts a lane."""

        if self.selected_lane_name is None:
            return None
        return next(lane for lane in self.lanes if lane.name == self.selected_lane_name)


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
    """Read-only Orca state supplied to the planner."""

    model_config = ConfigDict(extra="forbid")

    worktrees: list[OrcaWorktree]
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def active_count(self) -> int:
        """Count worktrees currently doing or awaiting active work."""

        active_statuses = {"active", "working"}
        active_workspace_statuses = {"in-progress", "in-review"}
        return sum(
            1
            for worktree in self.worktrees
            if worktree.status in active_statuses
            and worktree.workspace_status in active_workspace_statuses
        )


class LaneRecord(BaseModel):
    """A persisted local correlation between a proposal and an Orca lane."""

    model_config = ConfigDict(extra="forbid")

    lane_id: str
    run_id: str
    name: str
    issue_id: str
    repo_selector: str
    agent_id: str
    phase: LanePhase
    worktree_id: str | None
    created_at: datetime
    updated_at: datetime


class CycleResult(BaseModel):
    """Machine-readable result from one supervisor cycle."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    executed: bool
    action: str
    plan: SupervisorPlan
    worktree_id: str | None = None
