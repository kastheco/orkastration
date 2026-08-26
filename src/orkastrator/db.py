"""SQLModel tables for the orkastrator correlation ledger."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class SupervisorRunRow(SQLModel, table=True):
    __tablename__ = "supervisor_runs"

    run_id: str = Field(primary_key=True)
    objective: str
    status: str
    plan_json: str
    orca_run_id: str | None = None
    created_at: datetime
    updated_at: datetime


class LaneRow(SQLModel, table=True):
    __tablename__ = "lanes"
    __table_args__ = (UniqueConstraint("run_id", "name"),)

    lane_id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="supervisor_runs.run_id", index=True)
    name: str
    issue_id: str
    repo_selector: str
    base_ref: str = "HEAD"
    base_sha: str | None = None
    agent_id: str = "graph"
    phase: str
    worktree_id: str | None = None
    review_head_sha: str | None = None
    integration_head_sha: str | None = None
    created_at: datetime
    updated_at: datetime


class LegacyLaneStageRow(SQLModel, table=True):
    __tablename__ = "lane_stages"
    __table_args__ = (UniqueConstraint("lane_id", "role"),)

    stage_id: str = Field(primary_key=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", index=True)
    role: str
    phase: str
    orca_task_id: str | None = None
    orca_dispatch_id: str | None = None
    released: bool = Field(default=False, sa_column_kwargs={"server_default": "0"})
    created_at: datetime
    updated_at: datetime


class FindingRow(SQLModel, table=True):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("lane_id", "finding_id"),)

    finding_key: str = Field(primary_key=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", index=True)
    finding_id: str
    origin: str
    contract_json: str
    effective_contract_json: str
    phase: str
    round: int
    escalation_reason: str | None = None
    # The commit a fixer was actually dispatched from, when that is no longer the
    # frozen review revision. A conflict retry has to be rebuilt on the lane head
    # it must land on, and the review revision is the evidence anchor rather than
    # a build base, so it must not be rewritten to say that.
    dispatch_base_sha: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkflowStageRow(SQLModel, table=True):
    __tablename__ = "workflow_stages"

    stage_id: str = Field(primary_key=True)
    stage_key: str = Field(unique=True, index=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", index=True)
    role: str
    finding_key: str | None = Field(default=None, foreign_key="findings.finding_key")
    finding_id: str | None = None
    round: int | None = None
    attempt_kind: str | None = None
    phase: str
    orca_task_id: str | None = Field(default=None, unique=True)
    orca_dispatch_id: str | None = Field(default=None, unique=True)
    # The terminal orkastrator opened for this stage, when it opened one. Orca
    # will not close a terminal it did not create, so this is the only record of
    # which panes are orkastrator's to reclaim. Nullable because a stage whose
    # agent Orca launched has none, and because rows predate the column.
    orca_terminal_handle: str | None = None
    worktree_id: str | None = None
    # The worktree head at the moment this Dispatch started, so a stage that
    # died without reporting can still be asked whether it committed anything.
    # Null for a stage adopted after the fact, where the work has already
    # happened and no honest baseline is left to read, and for rows that
    # predate the column.
    start_head_sha: str | None = None
    result_json: str | None = None
    processed: bool = False
    released: bool = False
    created_at: datetime
    updated_at: datetime


class InitialReviewRow(SQLModel, table=True):
    __tablename__ = "initial_reviews"

    review_id: str = Field(primary_key=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", unique=True)
    report_json: str
    created_at: datetime


class WorkerResultRow(SQLModel, table=True):
    __tablename__ = "worker_results"

    result_id: str = Field(primary_key=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", unique=True)
    result_json: str
    created_at: datetime


class FixAttemptRow(SQLModel, table=True):
    __tablename__ = "fix_attempts"
    __table_args__ = (UniqueConstraint("stage_id"),)

    attempt_id: str = Field(primary_key=True)
    finding_key: str = Field(foreign_key="findings.finding_key", index=True)
    round: int
    attempt_kind: str
    stage_id: str = Field(foreign_key="workflow_stages.stage_id")
    payload_json: str
    created_at: datetime


class ReReviewRow(SQLModel, table=True):
    __tablename__ = "re_reviews"
    __table_args__ = (UniqueConstraint("stage_id"),)

    review_id: str = Field(primary_key=True)
    finding_key: str = Field(foreign_key="findings.finding_key", index=True)
    round: int
    stage_id: str = Field(foreign_key="workflow_stages.stage_id")
    payload_json: str
    created_at: datetime


class EscalationRow(SQLModel, table=True):
    __tablename__ = "escalations"
    __table_args__ = (UniqueConstraint("stage_id"),)

    escalation_id: str = Field(primary_key=True)
    finding_key: str = Field(foreign_key="findings.finding_key", index=True)
    round: int
    reason: str
    stage_id: str = Field(foreign_key="workflow_stages.stage_id")
    payload_json: str
    created_at: datetime


class LifecycleReceiptRow(SQLModel, table=True):
    __tablename__ = "lifecycle_receipts"

    receipt_id: str = Field(primary_key=True)
    stage_id: str = Field(foreign_key="workflow_stages.stage_id", unique=True)
    orca_task_id: str = Field(unique=True)
    payload_json: str
    created_at: datetime


class IntegrationRow(SQLModel, table=True):
    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("finding_key", "round"),)

    integration_id: str = Field(primary_key=True)
    finding_key: str = Field(foreign_key="findings.finding_key", index=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", index=True)
    round: int
    fixer_commit_sha: str
    base_sha: str
    integrated_sha: str | None = None
    source_commits_json: str = "[]"
    source_finding_ids_json: str = "[]"
    status: str
    validation_json: str
    conflict_context_json: str | None = None
    created_at: datetime
    updated_at: datetime


class AcceptanceAuthorizationRow(SQLModel, table=True):
    __tablename__ = "acceptance_authorizations"

    run_id: str = Field(foreign_key="supervisor_runs.run_id", primary_key=True)
    payload_json: str
    # The accepted policy itself, not only its digest. A digest cannot be
    # diffed, so without this the owner authorizing a mid-run policy change is
    # shown two hex strings and asked to judge them. Nullable because runs
    # accepted before this column existed have no payload to show.
    config_json: str | None = None
    created_at: datetime


class PublicationRow(SQLModel, table=True):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("lane_id", "head_sha"),)

    publication_id: str = Field(primary_key=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", index=True)
    head_sha: str
    payload_json: str
    created_at: datetime
    updated_at: datetime


class CiReceiptRow(SQLModel, table=True):
    __tablename__ = "ci_receipts"
    __table_args__ = (UniqueConstraint("lane_id", "head_sha"),)

    receipt_id: str = Field(primary_key=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", index=True)
    head_sha: str
    payload_json: str
    created_at: datetime
    updated_at: datetime


class CiFailureRow(SQLModel, table=True):
    __tablename__ = "ci_failures"
    __table_args__ = (UniqueConstraint("lane_id", "head_sha"),)

    failure_id: str = Field(primary_key=True)
    lane_id: str = Field(foreign_key="lanes.lane_id", index=True)
    head_sha: str
    payload_json: str
    created_at: datetime


class EventRow(SQLModel, table=True):
    __tablename__ = "events"

    event_id: str = Field(primary_key=True)
    run_id: str = Field(foreign_key="supervisor_runs.run_id", index=True)
    lane_id: str | None = None
    kind: str
    payload_json: str
    created_at: datetime
