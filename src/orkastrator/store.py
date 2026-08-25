"""SQLModel correlation ledger for dynamic orkastrator convergence workflows."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar, cast

from sqlalchemy import event, func, inspect
from sqlmodel import Session, SQLModel, col, create_engine, select

from orkastrator.db import (
    AcceptanceAuthorizationRow,
    CiFailureRow,
    CiReceiptRow,
    EscalationRow,
    EventRow,
    FindingRow,
    FixAttemptRow,
    InitialReviewRow,
    IntegrationRow,
    LaneRow,
    LegacyLaneStageRow,
    LifecycleReceiptRow,
    PublicationRow,
    ReReviewRow,
    SupervisorRunRow,
    WorkerResultRow,
    WorkflowStageRow,
)
from orkastrator.models import (
    AcceptanceAuthorization,
    AttemptKind,
    CiFailureFinding,
    CiReceipt,
    ConfigChange,
    EscalationDecision,
    FindingPhase,
    FindingReason,
    FindingRecord,
    FixAttempt,
    FixAttemptIdentity,
    InitialReviewReport,
    IntegrationRecord,
    LanePhase,
    LaneRecord,
    OrcaWorkerResult,
    PublicationReceipt,
    ReReviewResult,
    ReviewFinding,
    RunRecord,
    StageClock,
    StageKind,
    StagePhase,
    StageRecord,
    SupervisorPlan,
    ValidationResult,
    WorkerResult,
)
from orkastrator.scope import scopes_overlap


class UnsupportedStateError(RuntimeError):
    """Raised when an accepted fixed-DAG database cannot resume safely."""


class IntegrationBusyError(RuntimeError):
    """Raised when another approved commit owns a lane's integration lock."""


FindingOrigin = Literal[
    "initial_review",
    "introduced_by_fix",
    "unrelated",
    "ci_failure",
    "worker_blocked",
    "publication_conflict",
]
ContractRow = TypeVar("ContractRow", FixAttemptRow, ReReviewRow, EscalationRow)

_SETTLED_STAGE_PHASES = frozenset(
    {StagePhase.COMPLETED.value, StagePhase.FAILED.value, StagePhase.BLOCKED.value}
)
"""Stage phases `ensure_stage` will never dispatch again under the same key."""

_REOPENABLE_PHASES = (
    FindingPhase.PENDING_FIX,
    FindingPhase.PENDING_RE_REVIEW,
    FindingPhase.PENDING_ESCALATION,
)

_SETTLEABLE_PHASES = (FindingPhase.RESOLVED, FindingPhase.DEFERRED)
"""The decisions an owner may record directly on a finding.

These two are terminal by design, which is exactly why an owner has to be able to
reach them: a blocked finding is one no further agent round can settle.
"""
"""The phases a settled finding may be sent back to.

Every other phase either names work already in flight or is itself terminal, and
reopening into one would hand the graph a state no stage can advance.
"""


class StateStore:
    """Persist workflow evidence without replacing Orca as runtime authority."""

    def __init__(self, path: Path):
        self.path = path
        self._engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        event.listen(self._engine, "connect", _enable_sqlite_foreign_keys)

    def setup(self) -> None:
        """Create current tables, add compatible columns, and reject v1 active state."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        SQLModel.metadata.create_all(self._engine)
        self._migrate_additive_columns()
        self._migrate_contract_uniqueness()
        with self._session() as session:
            active_runs = session.exec(
                select(SupervisorRunRow).where(
                    col(SupervisorRunRow.status).in_(["proposed", "active", "blocked"])
                )
            ).all()
            for run in active_runs:
                lane_ids = session.exec(
                    select(LaneRow.lane_id).where(LaneRow.run_id == run.run_id)
                ).all()
                if not lane_ids:
                    continue
                has_legacy = session.exec(
                    select(LegacyLaneStageRow.stage_id).where(
                        col(LegacyLaneStageRow.lane_id).in_(lane_ids)
                    )
                ).first()
                has_dynamic = session.exec(
                    select(WorkflowStageRow.stage_id).where(
                        col(WorkflowStageRow.lane_id).in_(lane_ids)
                    )
                ).first()
                if has_legacy is not None and has_dynamic is None:
                    raise UnsupportedStateError(
                        "v1 fixed-stage state cannot resume in the v2 dynamic scheduler; "
                        f"finish or archive run {run.run_id} with the previous version"
                    )

    def record_proposal(self, proposal: SupervisorPlan) -> str:
        run_id = str(uuid.uuid4())
        now = _now()
        with self._session() as session:
            session.add(
                SupervisorRunRow(
                    run_id=run_id,
                    objective=proposal.objective,
                    status="proposed",
                    plan_json=proposal.model_dump_json(),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            for lane in proposal.lanes:
                lane_id = str(uuid.uuid4())
                session.add(
                    LaneRow(
                        lane_id=lane_id,
                        run_id=run_id,
                        name=lane.name,
                        issue_id=lane.issue_id,
                        repo_selector=lane.repo_selector,
                        base_ref=lane.base_ref,
                        phase=LanePhase.PROPOSED.value,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
                self._insert_stage(
                    session,
                    lane_id=lane_id,
                    stage_key=f"{lane_id}:worker",
                    role=StageKind.WORKER,
                )
            session.flush()
            self._event(session, run_id, None, "proposal_recorded", proposal.model_dump())
        return run_id

    def run(self, run_id: str) -> RunRecord:
        with self._session() as session:
            row = session.get(SupervisorRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id}")
            return _run(row)

    def lanes(self, run_id: str | None = None) -> list[LaneRecord]:
        with self._session() as session:
            statement = select(LaneRow)
            if run_id is not None:
                statement = statement.where(LaneRow.run_id == run_id)
            rows = session.exec(
                statement.order_by(col(LaneRow.created_at), col(LaneRow.lane_id))
            ).all()
            return [_lane(row) for row in rows]

    def stages(self, run_id: str) -> list[StageRecord]:
        with self._session() as session:
            rows = session.exec(
                select(WorkflowStageRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(WorkflowStageRow.lane_id))
                .where(LaneRow.run_id == run_id)
                .order_by(
                    col(LaneRow.created_at),
                    col(WorkflowStageRow.created_at),
                    col(WorkflowStageRow.stage_id),
                )
            ).all()
            return [_stage(row) for row in rows]

    def foreign_reservations(self, run_id: str) -> list[tuple[str, StageRecord]]:
        """Return unconfirmed start reservations held by every other run.

        Capacity is counted across the whole store, so a reservation abandoned by a
        run nobody monitors any more would otherwise spend that budget forever.
        """

        with self._session() as session:
            rows = session.exec(
                select(LaneRow.run_id, WorkflowStageRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(WorkflowStageRow.lane_id))
                .where(LaneRow.run_id != run_id)
                .where(WorkflowStageRow.phase == StagePhase.STARTING.value)
                .where(col(WorkflowStageRow.orca_dispatch_id).is_(None))
                .order_by(col(WorkflowStageRow.created_at), col(WorkflowStageRow.stage_id))
            ).all()
            return [(owner, _stage(row)) for owner, row in rows]

    def findings(self, run_id: str, lane_id: str | None = None) -> list[FindingRecord]:
        with self._session() as session:
            statement = (
                select(FindingRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(FindingRow.lane_id))
                .where(LaneRow.run_id == run_id)
            )
            if lane_id is not None:
                statement = statement.where(FindingRow.lane_id == lane_id)
            rows = session.exec(
                statement.order_by(col(FindingRow.created_at), col(FindingRow.finding_key))
            ).all()
            return [_finding(row) for row in rows]

    def ensure_stage(
        self,
        run_id: str,
        lane_id: str,
        *,
        stage_key: str,
        role: StageKind,
        finding_key: str | None = None,
        finding_id: str | None = None,
        round: int | None = None,
        attempt_kind: AttemptKind | None = None,
    ) -> StageRecord:
        with self._session() as session:
            row = session.exec(
                select(WorkflowStageRow).where(WorkflowStageRow.stage_key == stage_key)
            ).first()
            if row is None:
                self._insert_stage(
                    session,
                    lane_id=lane_id,
                    stage_key=stage_key,
                    role=role,
                    finding_key=finding_key,
                    finding_id=finding_id,
                    round=round,
                    attempt_kind=attempt_kind,
                )
                self._event(
                    session,
                    run_id,
                    lane_id,
                    "stage_created",
                    {"stage_key": stage_key, "role": role.value},
                )
                session.flush()
                row = session.exec(
                    select(WorkflowStageRow).where(WorkflowStageRow.stage_key == stage_key)
                ).one()
            return _stage(row)

    def mark_accepted(self, run_id: str, orca_run_id: str) -> None:
        with self._session() as session:
            row = session.get(SupervisorRunRow, run_id)
            if row is None or row.status != "proposed":
                raise ValueError(f"run {run_id} is not awaiting acceptance")
            row.status = "active"
            row.orca_run_id = orca_run_id
            row.updated_at = _now()
            self._event(session, run_id, None, "proposal_accepted", {"orca_run_id": orca_run_id})

    def record_acceptance_authorization(
        self,
        run_id: str,
        authorization: AcceptanceAuthorization,
        config: object | None = None,
    ) -> None:
        if authorization.run_id != run_id:
            raise ValueError("acceptance authorization does not match its run")
        payload = authorization.model_dump_json()
        with self._session() as session:
            row = session.get(AcceptanceAuthorizationRow, run_id)
            if row is not None:
                if row.payload_json != payload:
                    raise ValueError("accepted proposal or publication policy changed")
                # A run accepted before the policy payload was stored can still
                # gain it: the digest already proves this is the same policy, so
                # writing it makes the next reauthorization readable.
                if row.config_json is None and config is not None:
                    row.config_json = json.dumps(config, sort_keys=True)
                return
            session.add(
                AcceptanceAuthorizationRow(
                    run_id=run_id,
                    payload_json=payload,
                    config_json=None if config is None else json.dumps(config, sort_keys=True),
                    created_at=_now(),
                )
            )
            self._event(session, run_id, None, "acceptance_authorized", authorization.model_dump())

    def reauthorize_acceptance(
        self,
        run_id: str,
        authorization: AcceptanceAuthorization,
        note: str,
        *,
        config: object | None = None,
        changes: list[ConfigChange] | None = None,
    ) -> None:
        """Re-freeze one run's authorization against a policy the owner changed on purpose.

        `record_acceptance_authorization` refuses to move, which is the point: a
        graph must not silently start running under a policy nobody accepted.
        But the supervisor is edited while runs are live, and a config change
        then fails every tick with nothing but an instruction to throw the run
        away. That is not a recovery, and the recovery people actually performed
        was editing this table by hand.

        So the move is supported, and separate, and audited. It carries the
        owner's reason and the digests on both sides, so `orkas show` can say
        which policy each stage ran under rather than leaving the change
        invisible.
        """

        if authorization.run_id != run_id:
            raise ValueError("acceptance authorization does not match its run")
        with self._session() as session:
            row = session.get(AcceptanceAuthorizationRow, run_id)
            if row is None:
                raise KeyError(f"run {run_id} has no frozen acceptance authorization")
            previous = AcceptanceAuthorization.model_validate_json(row.payload_json)
            if previous.proposal_sha256 != authorization.proposal_sha256:
                raise ValueError(
                    "the accepted proposal itself changed, not just the policy; "
                    "record and accept a new proposal"
                )
            row.payload_json = authorization.model_dump_json()
            if config is not None:
                row.config_json = json.dumps(config, sort_keys=True)
            self._event(
                session,
                run_id,
                None,
                "supervisor_reauthorized_policy",
                {
                    "from_config_sha256": previous.config_sha256,
                    "to_config_sha256": authorization.config_sha256,
                    "note": note[:4_000],
                    # What moved, not only that something did. A digest pair in
                    # the audit trail cannot be read back into a decision.
                    "changes": [item.model_dump() for item in changes or []],
                },
            )
            session.commit()

    def acceptance_authorization(self, run_id: str) -> AcceptanceAuthorization | None:
        with self._session() as session:
            row = session.get(AcceptanceAuthorizationRow, run_id)
            return (
                None
                if row is None
                else AcceptanceAuthorization.model_validate_json(row.payload_json)
            )

    def accepted_config(self, run_id: str) -> object | None:
        """The policy this run was accepted under, when it was recorded.

        `None` means the run predates the stored payload, which is not the same
        as "nothing changed" and must not be reported as it.
        """

        with self._session() as session:
            row = session.get(AcceptanceAuthorizationRow, run_id)
            if row is None or row.config_json is None:
                return None
            return cast(object, json.loads(row.config_json))

    def record_publication(self, run_id: str, lane_id: str, receipt: PublicationReceipt) -> None:
        payload = receipt.model_dump_json()
        now = _now()
        with self._session() as session:
            row = session.exec(
                select(PublicationRow).where(
                    PublicationRow.lane_id == lane_id,
                    PublicationRow.head_sha == receipt.head_sha,
                )
            ).first()
            if row is None:
                session.add(
                    PublicationRow(
                        publication_id=str(uuid.uuid4()),
                        lane_id=lane_id,
                        head_sha=receipt.head_sha,
                        payload_json=payload,
                        created_at=now,
                        updated_at=now,
                    )
                )
                kind = "lane_published"
            else:
                previous = PublicationReceipt.model_validate_json(row.payload_json)
                # Draft, landed, the merged head, and the merge sha are the facts
                # about a published head that may move. The first two move in one
                # direction; the two merge identities may be filled once on landing.
                # Everything else identifies the publication and must not change.
                if (
                    previous.model_copy(
                        update={
                            "draft": receipt.draft,
                            "landed": receipt.landed,
                            "merged_head_sha": receipt.merged_head_sha,
                            "merge_sha": receipt.merge_sha,
                        }
                    )
                    != receipt
                ):
                    raise ValueError("publication identity changed for an existing head")
                if previous.landed and not receipt.landed:
                    raise ValueError("a landed publication cannot be un-landed")
                if previous.merge_sha is not None and previous.merge_sha != receipt.merge_sha:
                    raise ValueError("a publication merge sha cannot change")
                if (
                    previous.merged_head_sha is not None
                    and previous.merged_head_sha != receipt.merged_head_sha
                ):
                    raise ValueError("a publication merged head cannot change")
                row.payload_json = payload
                row.updated_at = now
                if receipt.landed and not previous.landed:
                    kind = "pull_request_landed"
                elif previous.draft and not receipt.draft:
                    kind = "pull_request_ready"
                else:
                    kind = "lane_published"
            self._event(session, run_id, lane_id, kind, receipt.model_dump())

    def publications(self, run_id: str, lane_id: str | None = None) -> list[PublicationReceipt]:
        with self._session() as session:
            statement = (
                select(PublicationRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(PublicationRow.lane_id))
                .where(LaneRow.run_id == run_id)
            )
            if lane_id is not None:
                statement = statement.where(PublicationRow.lane_id == lane_id)
            rows = session.exec(
                statement.order_by(
                    col(PublicationRow.created_at), col(PublicationRow.publication_id)
                )
            ).all()
            return [PublicationReceipt.model_validate_json(row.payload_json) for row in rows]

    def record_ci_receipt(self, run_id: str, lane_id: str, receipt: CiReceipt) -> None:
        payload = receipt.model_dump_json()
        now = _now()
        with self._session() as session:
            row = session.exec(
                select(CiReceiptRow).where(
                    CiReceiptRow.lane_id == lane_id,
                    CiReceiptRow.head_sha == receipt.head_sha,
                )
            ).first()
            if row is None:
                session.add(
                    CiReceiptRow(
                        receipt_id=str(uuid.uuid4()),
                        lane_id=lane_id,
                        head_sha=receipt.head_sha,
                        payload_json=payload,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.payload_json = payload
                row.updated_at = now
            self._event(session, run_id, lane_id, "ci_observed", receipt.model_dump())

    def ci_receipts(self, run_id: str, lane_id: str | None = None) -> list[CiReceipt]:
        with self._session() as session:
            statement = (
                select(CiReceiptRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(CiReceiptRow.lane_id))
                .where(LaneRow.run_id == run_id)
            )
            if lane_id is not None:
                statement = statement.where(CiReceiptRow.lane_id == lane_id)
            rows = session.exec(
                statement.order_by(col(CiReceiptRow.created_at), col(CiReceiptRow.receipt_id))
            ).all()
            return [CiReceipt.model_validate_json(row.payload_json) for row in rows]

    def record_ci_failure(self, run_id: str, lane_id: str, failure: CiFailureFinding) -> bool:
        payload = failure.model_dump_json()
        with self._session() as session:
            row = session.exec(
                select(CiFailureRow).where(
                    CiFailureRow.lane_id == lane_id,
                    CiFailureRow.head_sha == failure.published_sha,
                )
            ).first()
            if row is not None:
                if row.payload_json != payload:
                    raise ValueError("CI failure changed for an existing published head")
                return False
            session.add(
                CiFailureRow(
                    failure_id=str(uuid.uuid4()),
                    lane_id=lane_id,
                    head_sha=failure.published_sha,
                    payload_json=payload,
                    created_at=_now(),
                )
            )
            self._event(session, run_id, lane_id, "ci_failure_frozen", failure.model_dump())
            return True

    def ci_failures(self, run_id: str, lane_id: str | None = None) -> list[CiFailureFinding]:
        with self._session() as session:
            statement = (
                select(CiFailureRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(CiFailureRow.lane_id))
                .where(LaneRow.run_id == run_id)
            )
            if lane_id is not None:
                statement = statement.where(CiFailureRow.lane_id == lane_id)
            rows = session.exec(
                statement.order_by(col(CiFailureRow.created_at), col(CiFailureRow.failure_id))
            ).all()
            return [CiFailureFinding.model_validate_json(row.payload_json) for row in rows]

    def bind_stage_task(self, run_id: str, stage_id: str, task_id: str) -> None:
        with self._session() as session:
            row = session.get(WorkflowStageRow, stage_id)
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            if row.orca_task_id is not None:
                if row.orca_task_id != task_id:
                    raise ValueError(f"stage {stage_id} is already bound")
                return
            row.orca_task_id = task_id
            row.phase = StagePhase.READY.value
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "stage_task_bound",
                {"stage_id": stage_id, "task_id": task_id},
            )

    def mark_stage_started(
        self,
        run_id: str,
        stage_id: str,
        dispatch_id: str,
        worktree_id: str,
        payload: object,
        terminal_handle: str | None = None,
        start_head_sha: str | None = None,
    ) -> None:
        with self._session() as session:
            row = session.get(WorkflowStageRow, stage_id)
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            if row.orca_dispatch_id is not None:
                if row.orca_dispatch_id != dispatch_id:
                    raise ValueError(f"stage {stage_id} already has a dispatch")
                return
            row.phase = StagePhase.DISPATCHED.value
            row.orca_dispatch_id = dispatch_id
            # Written with the dispatch rather than later, because a supervisor
            # that dies between the two loses the only record of which terminal
            # it opened and the pane leaks for the life of the machine.
            row.orca_terminal_handle = terminal_handle
            row.worktree_id = worktree_id
            # Written here rather than derived later, because the only moment
            # this is knowable is before the agent has touched the worktree.
            row.start_head_sha = start_head_sha
            row.updated_at = _now()
            if row.role == StageKind.WORKER.value:
                lane = session.get(LaneRow, row.lane_id)
                if lane is None:
                    raise KeyError(f"unknown lane {row.lane_id}")
                lane.phase = LanePhase.ACTIVE.value
                lane.worktree_id = worktree_id
                lane.updated_at = _now()
            self._event(session, run_id, row.lane_id, "stage_started", payload)

    def record_worker_checkout(
        self, run_id: str, stage_id: str, worktree_id: str, base_sha: str | None = None
    ) -> None:
        """Persist a worker checkout and, once known, its immutable build base."""

        with self._session() as session:
            row = session.get(WorkflowStageRow, stage_id)
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            if row.role != StageKind.WORKER.value:
                raise ValueError(f"stage {stage_id} is not a worker")
            if row.worktree_id is not None and row.worktree_id != worktree_id:
                raise ValueError(f"worker stage {stage_id} already has another checkout")
            lane = session.get(LaneRow, row.lane_id)
            if lane is None:
                raise KeyError(f"unknown lane {row.lane_id}")
            if lane.worktree_id is not None and lane.worktree_id != worktree_id:
                raise ValueError(f"lane {lane.lane_id} already has another checkout")
            if base_sha is not None and lane.base_sha is not None and lane.base_sha != base_sha:
                raise ValueError(f"lane {lane.lane_id} already has another frozen base")
            changed = (
                row.worktree_id != worktree_id
                or lane.worktree_id != worktree_id
                or (base_sha is not None and lane.base_sha is None)
            )
            if not changed:
                return
            row.worktree_id = worktree_id
            lane.worktree_id = worktree_id
            if base_sha is not None:
                lane.base_sha = base_sha
            row.updated_at = _now()
            lane.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "worker_checkout_recorded",
                {"stage_id": stage_id, "worktree_id": worktree_id, "base_sha": base_sha},
            )

    def reserve_stage_start(
        self,
        run_id: str,
        stage: StageRecord,
        *,
        max_workers: int,
        max_lanes: int,
        max_lane_fixers: int,
        write_paths: tuple[str, ...] = (),
    ) -> bool:
        """Atomically reserve capacity before an external worker start."""

        active = [StagePhase.STARTING.value, StagePhase.DISPATCHED.value]
        with self._session(immediate=True) as session:
            row = session.get(WorkflowStageRow, stage.stage_id)
            if row is None:
                raise KeyError(f"unknown stage {stage.stage_id}")
            if row.phase != StagePhase.READY.value:
                return False
            workers = session.exec(
                select(func.count())
                .select_from(WorkflowStageRow)
                .where(col(WorkflowStageRow.phase).in_(active))
            ).one()
            if workers >= max_workers:
                return False
            active_lanes = set(
                session.exec(
                    select(WorkflowStageRow.lane_id)
                    .where(col(WorkflowStageRow.phase).in_(active))
                    .distinct()
                ).all()
            )
            if stage.lane_id not in active_lanes and len(active_lanes) >= max_lanes:
                return False
            if stage.role is StageKind.FIXER:
                fixers = session.exec(
                    select(WorkflowStageRow).where(
                        WorkflowStageRow.lane_id == stage.lane_id,
                        WorkflowStageRow.role == StageKind.FIXER.value,
                        col(WorkflowStageRow.phase).in_(active),
                    )
                ).all()
                if len(fixers) >= max_lane_fixers:
                    return False
                for fixer in fixers:
                    if fixer.finding_key is None:
                        continue
                    finding = session.get(FindingRow, fixer.finding_key)
                    if finding is None:
                        continue
                    contract = ReviewFinding.model_validate_json(finding.effective_contract_json)
                    if scopes_overlap(write_paths, tuple(contract.allowed_write_scope.paths)):
                        return False
            elif stage.role is StageKind.WORKER and stage.lane_id in active_lanes:
                # Only a worker owns the lane exclusively. Reviewers, re-reviewers
                # and escalations each get their own Orca worktree and read a diff
                # that was already frozen to an exact base and head, so nothing
                # they do can observe or disturb another stage's work. Refusing
                # them while any stage in the lane was active made the lane strictly
                # serial: one slow fixer held up every unrelated adjudication behind
                # it, and a lane with three settled findings sat idle waiting on a
                # fix for a fourth. Total concurrency is still governed by
                # ``max_workers``, lane spread by ``max_lanes``, and overlapping
                # fixers by the write-scope check above.
                return False
            row.phase = StagePhase.STARTING.value
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                stage.lane_id,
                "stage_start_reserved",
                {"stage_id": stage.stage_id},
            )
            return True

    def reset_stage_reservation(self, run_id: str, stage: StageRecord) -> None:
        with self._session() as session:
            row = session.get(WorkflowStageRow, stage.stage_id)
            if (
                row is None
                or row.phase != StagePhase.STARTING.value
                or row.orca_dispatch_id is not None
            ):
                return
            row.phase = StagePhase.READY.value
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                stage.lane_id,
                "stage_start_reservation_reset",
                {"stage_id": stage.stage_id},
            )

    def record_stage_start_failure(
        self, run_id: str, stage: StageRecord, detail: str, *, released: bool
    ) -> None:
        """Record that one stage could not be started, and whether its slot came back.

        `_start_ready` walks every ready stage in one pass. Before this existed a
        single failing Orca call raised out of that loop, so every stage behind
        the failing one went unreached and the reserving stage sat in STARTING
        with nothing anywhere saying why. The run then read as idle rather than
        as broken, which is the failure mode that quietly stops a graph
        overnight.

        `released` is the substance, not a detail. A refused start is known not
        to have happened, so the reservation goes back and the stage is tried
        again next tick. A timed-out start has no outcome: the worker may be
        running. That one stays in STARTING, where reconciliation adopts the
        Dispatch if Orca made one and frees the slot if it did not.
        """

        with self._session() as session:
            self._event(
                session,
                run_id,
                stage.lane_id,
                "stage_start_failed",
                {
                    "stage_id": stage.stage_id,
                    "stage_key": stage.stage_key,
                    "role": stage.role.value,
                    "released": released,
                    "detail": detail[:2_000],
                },
            )

    def release_dead_dispatch(self, run_id: str, stage: StageRecord) -> None:
        """Free a stage whose supervised worker died before it reported anything.

        Orca returns a Task to READY when the worker terminal goes away, but the
        stage keeps the Dispatch id that proved a worker was started, and
        `_start_ready` skips any stage that already holds one. A crashed agent,
        or a supervisor that exited while its workers were live, would otherwise
        leave the stage unstartable for the rest of the run. A stage that did
        report a result is settled by its result, not by this, so leave it alone.
        """

        with self._session() as session:
            row = session.get(WorkflowStageRow, stage.stage_id)
            if row is None or row.phase != StagePhase.READY.value:
                return
            if row.orca_dispatch_id is None or row.result_json is not None:
                return
            row.orca_dispatch_id = None
            # The baseline belonged to the dispatch being released. The next one
            # starts from wherever this dead worker left the worktree, and
            # keeping the old value would measure the two dispatches together.
            row.start_head_sha = None
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "stage_dead_dispatch_released",
                {"stage_id": stage.stage_id},
            )

    def hold_unreported_dispatch(self, run_id: str, stage: StageRecord) -> None:
        """Keep a dead dispatch on a stage whose worktree holds unreported work.

        The counterpart to `release_dead_dispatch`, for the case that method
        reads as a crash and is not one: the worker finished, committed, and
        died before reporting. Clearing the Dispatch id there sends a fresh
        agent into a worktree that already contains the answer, and what comes
        back is an empty result that fails the lane.

        Holding it leaves the stage unstartable, which is the point. There is
        no result to settle it with and no honest one to synthesise, so the
        stage stays put and goes overdue on its own budget, which is a signal
        the owner already watches. Doing nothing visible would be worse than
        the re-dispatch, hence the event.

        Emitted once per dispatch. Every tick re-observes the same READY task
        and would otherwise write the same line again, burying the run's real
        history under a repeated one.
        """

        with self._session() as session:
            row = session.get(WorkflowStageRow, stage.stage_id)
            if row is None or row.phase != StagePhase.READY.value:
                return
            if row.orca_dispatch_id is None or row.result_json is not None:
                return
            held = session.exec(
                select(EventRow).where(
                    EventRow.run_id == run_id,
                    EventRow.kind == "stage_unreported_work_held",
                )
            ).all()
            for event in held:
                payload = json.loads(event.payload_json)
                if not isinstance(payload, dict):
                    continue
                if (payload.get("stage_id"), payload.get("dispatch_id")) == (
                    stage.stage_id,
                    row.orca_dispatch_id,
                ):
                    return
            self._event(
                session,
                run_id,
                row.lane_id,
                "stage_unreported_work_held",
                {
                    "stage_id": stage.stage_id,
                    "stage_key": row.stage_key,
                    "role": row.role,
                    "dispatch_id": row.orca_dispatch_id,
                    "worktree_id": row.worktree_id,
                },
            )

    def stage_clocks(self, run_id: str) -> dict[str, StageClock]:
        """When each stage's current dispatch started, and what has been said about it.

        Read from the event log rather than from `updated_at`, because that
        column moves for reasons that have nothing to do with the agent: a
        result syncing, a task binding, a release. The clock has to restart when
        a stage is re-dispatched, so every counter here is scoped to the latest
        `stage_start_reserved` and anything older is not this dispatch's
        history.
        """

        kinds = ["stage_start_reserved", "stage_overdue", "stage_timed_out"]
        with self._session() as session:
            rows = session.exec(
                select(EventRow)
                .where(EventRow.run_id == run_id, col(EventRow.kind).in_(kinds))
                .order_by(col(EventRow.created_at), col(EventRow.event_id))
            ).all()
        clocks: dict[str, StageClock] = {}
        timeouts: dict[str, int] = {}
        for row in rows:
            payload = json.loads(row.payload_json)
            stage_id = payload.get("stage_id")
            if not isinstance(stage_id, str):
                continue
            if row.kind == "stage_start_reserved":
                clocks[stage_id] = StageClock(
                    started_at=row.created_at, warned=False, timeouts=timeouts.get(stage_id, 0)
                )
                continue
            clock = clocks.get(stage_id)
            if clock is None:
                continue
            if row.kind == "stage_overdue":
                clocks[stage_id] = clock.model_copy(update={"warned": True})
            else:
                timeouts[stage_id] = clock.timeouts + 1
                clocks[stage_id] = clock.model_copy(update={"timeouts": timeouts[stage_id]})
        return clocks

    def note_stage_overdue(
        self, run_id: str, stage: StageRecord, minutes: int, activity: str | None = None
    ) -> None:
        """Record that one dispatched stage has passed its soft budget.

        Soft means soft: nothing is released and nothing is failed. A stage that
        is merely slow and a stage that is wedged look identical from outside,
        so `activity` carries what it was last observed doing. Without it the
        event says a stage is late and leaves the useful half unwritten.
        """

        with self._session() as session:
            self._event(
                session,
                run_id,
                stage.lane_id,
                "stage_overdue",
                {
                    "stage_id": stage.stage_id,
                    "stage_key": stage.stage_key,
                    "role": stage.role.value,
                    "minutes": minutes,
                    "activity": activity,
                },
            )

    def note_stage_timed_out(self, run_id: str, stage: StageRecord, minutes: int) -> None:
        """Record that one dispatched stage's worker was released for exceeding its budget.

        Deliberately not a result. The stage produced nothing, so it must be
        dispatched again rather than recorded as having failed on the merits;
        blurring those two turns a slow machine into a false finding.
        """

        with self._session() as session:
            self._event(
                session,
                run_id,
                stage.lane_id,
                "stage_timed_out",
                {
                    "stage_id": stage.stage_id,
                    "stage_key": stage.stage_key,
                    "role": stage.role.value,
                    "minutes": minutes,
                },
            )

    def sync_stage(
        self, run_id: str, stage_id: str, phase: StagePhase, result_json: str | None
    ) -> None:
        with self._session() as session:
            row = session.get(WorkflowStageRow, stage_id)
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            next_result = row.result_json if result_json is None else result_json
            if row.phase == phase.value and row.result_json == next_result:
                return
            row.phase = phase.value
            row.result_json = next_result
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "stage_reconciled",
                {"stage_id": stage_id, "phase": phase.value},
            )

    def record_lifecycle_receipt(
        self, run_id: str, stage: StageRecord, result: OrcaWorkerResult
    ) -> None:
        if stage.orca_task_id is None:
            raise ValueError(f"stage {stage.stage_id} has no Orca task")
        payload = result.model_dump_json(by_alias=True)
        with self._session() as session:
            existing = session.exec(
                select(LifecycleReceiptRow).where(
                    LifecycleReceiptRow.orca_task_id == stage.orca_task_id
                )
            ).first()
            if existing is not None and existing.payload_json != payload:
                raise ValueError(f"Orca task {stage.orca_task_id} changed its lifecycle result")
            if existing is None:
                session.add(
                    LifecycleReceiptRow(
                        receipt_id=str(uuid.uuid4()),
                        stage_id=stage.stage_id,
                        orca_task_id=stage.orca_task_id,
                        payload_json=payload,
                        created_at=_now(),
                    )
                )
                self._event(
                    session,
                    run_id,
                    stage.lane_id,
                    "lifecycle_receipt_recorded",
                    {"stage_id": stage.stage_id, "task_id": stage.orca_task_id},
                )
            row = session.get(WorkflowStageRow, stage.stage_id)
            if row is None:
                raise KeyError(f"unknown stage {stage.stage_id}")
            row.processed = True
            row.updated_at = _now()

    def mark_stage_processed(self, run_id: str, stage: StageRecord, reason: str) -> None:
        with self._session() as session:
            row = session.get(WorkflowStageRow, stage.stage_id)
            if row is None or row.processed:
                return
            row.processed = True
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                stage.lane_id,
                "stage_result_rejected",
                {"stage_id": stage.stage_id, "reason": reason},
            )

    def record_initial_review(self, run_id: str, lane_id: str, report: InitialReviewReport) -> None:
        payload = report.model_dump_json()
        now = _now()
        with self._session() as session:
            existing = session.exec(
                select(InitialReviewRow).where(InitialReviewRow.lane_id == lane_id)
            ).first()
            if existing is not None:
                if existing.report_json != payload:
                    raise ValueError(f"lane {lane_id} initial review is already frozen")
                return
            session.add(
                InitialReviewRow(
                    review_id=str(uuid.uuid4()),
                    lane_id=lane_id,
                    report_json=payload,
                    created_at=now,
                )
            )
            for finding in report.findings:
                self._insert_finding(session, lane_id, finding, origin="initial_review", now=now)
            self._event(
                session,
                run_id,
                lane_id,
                "initial_review_frozen",
                {"finding_ids": [finding.id for finding in report.findings]},
            )

    def initial_review(self, lane_id: str) -> InitialReviewReport | None:
        """Return the lane's frozen initial review for crash-safe replay."""

        with self._session() as session:
            row = session.exec(
                select(InitialReviewRow).where(InitialReviewRow.lane_id == lane_id)
            ).first()
            return None if row is None else InitialReviewReport.model_validate_json(row.report_json)

    def record_worker_result(self, run_id: str, lane_id: str, result: WorkerResult) -> None:
        payload = result.model_dump_json()
        with self._session() as session:
            existing = session.exec(
                select(WorkerResultRow).where(WorkerResultRow.lane_id == lane_id)
            ).first()
            if existing is not None:
                if existing.result_json != payload:
                    raise ValueError(f"lane {lane_id} worker result changed after persistence")
                return
            session.add(
                WorkerResultRow(
                    result_id=str(uuid.uuid4()),
                    lane_id=lane_id,
                    result_json=payload,
                    created_at=_now(),
                )
            )
            lane = session.get(LaneRow, lane_id)
            if lane is None:
                raise KeyError(f"unknown lane {lane_id}")
            lane.review_head_sha = result.review_revision.head_sha
            lane.integration_head_sha = result.review_revision.head_sha
            lane.updated_at = _now()
            self._event(
                session,
                run_id,
                lane_id,
                "worker_result_frozen",
                result.model_dump(mode="json"),
            )

    def worker_result(self, lane_id: str) -> WorkerResult:
        with self._session() as session:
            row = session.exec(
                select(WorkerResultRow).where(WorkerResultRow.lane_id == lane_id)
            ).first()
            if row is None:
                raise KeyError(f"lane {lane_id} has no worker result")
            return WorkerResult.model_validate_json(row.result_json)

    def add_finding(
        self,
        run_id: str,
        lane_id: str,
        finding: ReviewFinding,
        *,
        origin: FindingOrigin,
    ) -> FindingRecord:
        with self._session() as session:
            created = self._insert_finding(session, lane_id, finding, origin=origin, now=_now())
            if created:
                self._event(
                    session,
                    run_id,
                    lane_id,
                    "finding_recorded",
                    {"finding_id": finding.id, "origin": origin},
                )
            row = session.exec(
                select(FindingRow).where(
                    FindingRow.lane_id == lane_id, FindingRow.finding_id == finding.id
                )
            ).one()
            return _finding(row)

    def reopen_finding(
        self,
        run_id: str,
        finding_id: str,
        *,
        phase: FindingPhase,
        round: int | None = None,
        escalation_reason: FindingReason | None = None,
        force: bool = False,
        note: str,
    ) -> FindingRecord:
        """Send one settled finding back to an earlier phase and clear what follows it.

        A finding settles wrongly when the supervisor lacked a word for what an
        agent meant, and until now the only way back was five hand-written UPDATE
        statements against the state file. Two of those statements encode
        implementation detail nobody should have to remember: `ensure_stage` keys
        off `stage_key`, so a settled stage has to be renamed or the graph never
        dispatches a replacement, and every contract row is frozen against its own
        replay, so a stale verdict at the reopened round rejects the fresh one as
        "contract changed after persistence".

        So retire the stages at and after the reopened round, drop exactly the
        contract classes that come after `phase`, and leave everything before it
        alone - a fix that is already committed is evidence a re-adjudication may
        still need.
        """

        if phase not in _REOPENABLE_PHASES:
            raise ValueError(f"cannot reopen a finding into {phase.value}")
        retired = 0
        with self._session() as session:
            row = session.exec(
                select(FindingRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(FindingRow.lane_id))
                .where(LaneRow.run_id == run_id, FindingRow.finding_id == finding_id)
            ).first()
            if row is None:
                raise KeyError(f"run {run_id} has no finding {finding_id}")
            # A finding that settled on the merits is not what this exists for.
            # Reopen is a recovery hatch for work a supervisor bug stranded, and
            # the id it takes is typed by hand: without this, one transposed
            # character silently undoes a correctly resolved finding and sends an
            # agent to re-fix it. `blocked` is deliberately not covered, because a
            # blocked finding is precisely the case reopen exists for.
            settled = FindingPhase(row.phase)
            if settled in _SETTLEABLE_PHASES and not force:
                raise ValueError(
                    f"finding {finding_id} is {settled.value}; pass force to reopen it anyway"
                )
            target = row.round if round is None else round
            stages = session.exec(
                select(WorkflowStageRow).where(
                    WorkflowStageRow.finding_key == row.finding_key,
                    col(WorkflowStageRow.round) >= target,
                )
            ).all()
            for stage in stages:
                if stage.phase not in _SETTLED_STAGE_PHASES:
                    continue
                stage.stage_key = f"{stage.stage_key}:reopened{_now():%Y%m%d%H%M%S}"
                stage.updated_at = _now()
                retired += 1
            for contract in self._superseded_contracts(session, row.finding_key, target, phase):
                session.delete(contract)
            row.phase = phase.value
            row.round = target
            row.escalation_reason = None if escalation_reason is None else escalation_reason.value
            row.updated_at = _now()
            lane = session.get(LaneRow, row.lane_id)
            if lane is not None and lane.phase != LanePhase.ACTIVE.value:
                lane.phase = LanePhase.ACTIVE.value
                lane.updated_at = _now()
            run = session.get(SupervisorRunRow, run_id)
            if run is not None and run.status != "active":
                run.status = "active"
                run.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "finding_reopened",
                {
                    "finding_id": finding_id,
                    "phase": phase.value,
                    "round": target,
                    "reason": None if escalation_reason is None else escalation_reason.value,
                    "retired_stages": retired,
                    "from_phase": settled.value,
                    "forced": force,
                    "note": note[:4_000],
                },
            )
            return _finding(row)

    def _finding_lane_id(self, run_id: str, finding_id: str) -> str:
        """The lane one finding belongs to, or a KeyError naming what was missing."""

        with self._session() as session:
            row = session.exec(
                select(FindingRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(FindingRow.lane_id))
                .where(LaneRow.run_id == run_id, FindingRow.finding_id == finding_id)
            ).first()
            if row is None:
                raise KeyError(f"run {run_id} has no finding {finding_id}")
            return row.lane_id

    def settle_finding(
        self, run_id: str, finding_id: str, *, phase: FindingPhase, note: str
    ) -> FindingRecord:
        """Record the owner decision that a blocked finding was raised for.

        `reopen_finding` is only half of the escape hatch, because it sends work
        back to an agent. A finding blocks precisely when no further agent round
        can settle it, so the other half has to exist too, or accepting or
        dropping one means hand-written UPDATE statements against the state file -
        which is what this class exists to stop.

        Retire whatever the finding still has in flight as well. A dispatched
        escalation returning after the decision would otherwise re-adjudicate a
        finding the owner has already closed and move it back out of the phase
        they chose.
        """

        if phase not in _SETTLEABLE_PHASES:
            raise ValueError(f"cannot settle a finding into {phase.value}")
        if phase is FindingPhase.RESOLVED:
            lane_id = self._finding_lane_id(run_id, finding_id)
            if finding_id not in self.integrated_finding_ids(run_id, lane_id):
                raise ValueError(
                    f"finding {finding_id} has no integrated fix, so it cannot be settled "
                    "resolved; integrate the fixer commit into the lane checkout first, or "
                    "settle it deferred"
                )
        retired = 0
        with self._session() as session:
            row = session.exec(
                select(FindingRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(FindingRow.lane_id))
                .where(LaneRow.run_id == run_id, FindingRow.finding_id == finding_id)
            ).first()
            if row is None:
                raise KeyError(f"run {run_id} has no finding {finding_id}")
            stages = session.exec(
                select(WorkflowStageRow).where(WorkflowStageRow.finding_key == row.finding_key)
            ).all()
            for stage in stages:
                if stage.phase in _SETTLED_STAGE_PHASES or stage.processed:
                    continue
                stage.processed = True
                # Take it out of the phase the scheduler starts from as well.
                # Marking it processed and renaming its key only stops
                # `ensure_stage` recreating it; the dispatch loop starts any
                # stage that is still READY, so a retired stage sat waiting for
                # a free slot and adjudicated a finding the owner had already
                # closed the moment one opened up.
                stage.phase = StagePhase.BLOCKED.value
                stage.stage_key = f"{stage.stage_key}:settled{_now():%Y%m%d%H%M%S}"
                stage.updated_at = _now()
                retired += 1
            row.phase = phase.value
            row.escalation_reason = None
            row.updated_at = _now()
            lane = session.get(LaneRow, row.lane_id)
            if lane is not None and lane.phase != LanePhase.ACTIVE.value:
                lane.phase = LanePhase.ACTIVE.value
                lane.updated_at = _now()
            run = session.get(SupervisorRunRow, run_id)
            if run is not None and run.status != "active":
                run.status = "active"
                run.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "finding_settled",
                {
                    "finding_id": finding_id,
                    "phase": phase.value,
                    "retired_stages": retired,
                    "note": note[:4_000],
                },
            )
            return _finding(row)

    @staticmethod
    def _superseded_contracts(
        session: Session, finding_key: str, round: int, phase: FindingPhase
    ) -> list[FixAttemptRow | ReReviewRow | EscalationRow]:
        """Collect the frozen contracts a reopen at `phase` invalidates.

        Reopening to an escalation keeps the fix attempt on purpose: `accept_fix`
        settles a finding on a commit that already exists, so dropping the record of
        that commit would take away the evidence the re-adjudication has to weigh.
        """

        rows: list[FixAttemptRow | ReReviewRow | EscalationRow] = list(
            session.exec(
                select(EscalationRow).where(
                    EscalationRow.finding_key == finding_key, col(EscalationRow.round) >= round
                )
            ).all()
        )
        if phase in {FindingPhase.PENDING_FIX, FindingPhase.PENDING_RE_REVIEW}:
            rows.extend(
                session.exec(
                    select(ReReviewRow).where(
                        ReReviewRow.finding_key == finding_key, col(ReReviewRow.round) >= round
                    )
                ).all()
            )
        if phase is FindingPhase.PENDING_FIX:
            rows.extend(
                session.exec(
                    select(FixAttemptRow).where(
                        FixAttemptRow.finding_key == finding_key, col(FixAttemptRow.round) >= round
                    )
                ).all()
            )
        return rows

    def set_finding_state(
        self,
        run_id: str,
        finding_key: str,
        *,
        phase: FindingPhase,
        round: int | None = None,
        escalation_reason: FindingReason | None = None,
        effective_contract: ReviewFinding | None = None,
    ) -> None:
        with self._session() as session:
            row = session.get(FindingRow, finding_key)
            if row is None:
                raise KeyError(f"unknown finding {finding_key}")
            next_round = row.round if round is None else round
            contract_json = (
                row.effective_contract_json
                if effective_contract is None
                else effective_contract.model_dump_json()
            )
            reason = escalation_reason.value if escalation_reason is not None else None
            if (
                row.phase == phase.value
                and row.round == next_round
                and row.escalation_reason == reason
                and row.effective_contract_json == contract_json
            ):
                return
            row.phase = phase.value
            row.round = next_round
            row.escalation_reason = reason
            row.effective_contract_json = contract_json
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "finding_transitioned",
                {
                    "finding_id": row.finding_id,
                    "phase": phase.value,
                    "round": next_round,
                    "reason": reason,
                },
            )

    def advance_fix_base(self, run_id: str, finding_key: str, base_sha: str) -> None:
        """Move the commit the next fixer for this finding is dispatched from.

        Only the build base moves. `review_revision` stays exactly as the initial
        review froze it, because that is the evidence anchor `_verified_review`
        checks and rewriting it would make the finding's own diff digest describe
        a range nobody reviewed.
        """

        with self._session() as session:
            row = session.get(FindingRow, finding_key)
            if row is None:
                raise KeyError(f"unknown finding {finding_key}")
            if row.dispatch_base_sha == base_sha:
                return
            previous = row.dispatch_base_sha
            row.dispatch_base_sha = base_sha
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "fix_base_advanced",
                {"finding_id": row.finding_id, "from": previous, "to": base_sha},
            )

    def record_fix_attempt(
        self, run_id: str, finding: FindingRecord, stage: StageRecord, attempt: FixAttempt
    ) -> None:
        if stage.attempt_kind is None:
            raise ValueError("fixer stage omitted attempt kind")
        with self._session() as session:
            existing = session.exec(
                select(FixAttemptRow).where(FixAttemptRow.stage_id == stage.stage_id)
            ).first()
            created = self._record_contract(
                session,
                existing,
                FixAttemptRow(
                    attempt_id=str(uuid.uuid4()),
                    finding_key=finding.finding_key,
                    round=attempt.round,
                    attempt_kind=stage.attempt_kind.value,
                    stage_id=stage.stage_id,
                    payload_json=attempt.model_dump_json(),
                    created_at=_now(),
                ),
                "fix_attempts",
            )
            if created:
                self._event(
                    session,
                    run_id,
                    finding.lane_id,
                    "fix_attempt_recorded",
                    {
                        "finding_id": finding.finding_id,
                        "round": attempt.round,
                        "attempt_kind": stage.attempt_kind.value,
                        "status": attempt.status,
                    },
                )

    def fix_attempt_for_stage(self, stage_id: str) -> FixAttempt | None:
        """Return the supervisor-frozen attempt for one fixer stage, if any."""

        with self._session() as session:
            row = session.exec(
                select(FixAttemptRow).where(FixAttemptRow.stage_id == stage_id)
            ).first()
            return None if row is None else FixAttempt.model_validate_json(row.payload_json)

    def record_re_review(
        self, run_id: str, finding: FindingRecord, stage: StageRecord, result: ReReviewResult
    ) -> None:
        with self._session() as session:
            existing = session.exec(
                select(ReReviewRow).where(ReReviewRow.stage_id == stage.stage_id)
            ).first()
            created = self._record_contract(
                session,
                existing,
                ReReviewRow(
                    review_id=str(uuid.uuid4()),
                    finding_key=finding.finding_key,
                    round=result.round,
                    stage_id=stage.stage_id,
                    payload_json=result.model_dump_json(),
                    created_at=_now(),
                ),
                "re_reviews",
            )
            if created:
                self._event(
                    session,
                    run_id,
                    finding.lane_id,
                    "re_review_recorded",
                    {
                        "finding_id": finding.finding_id,
                        "round": result.round,
                        "verdict": result.verdict,
                    },
                )

    def re_review(self, finding_key: str, round: int) -> ReReviewResult | None:
        with self._session() as session:
            row = session.exec(
                select(ReReviewRow)
                .where(ReReviewRow.finding_key == finding_key, ReReviewRow.round == round)
                .order_by(col(ReReviewRow.created_at).desc())
            ).first()
            return None if row is None else ReReviewResult.model_validate_json(row.payload_json)

    def re_review_for_stage(self, stage_id: str) -> ReReviewResult | None:
        """The verdict one re-review stage recorded, if it has recorded one.

        Distinct from `re_review`, which answers for a round. A round can be run
        twice - a fix rebased after a conflict and re-reviewed again - and those
        two verdicts describe different commits, so "has this stage already spoken
        and is it saying something else now" cannot be asked of the round.
        """

        with self._session() as session:
            row = session.exec(select(ReReviewRow).where(ReReviewRow.stage_id == stage_id)).first()
            return None if row is None else ReReviewResult.model_validate_json(row.payload_json)

    def record_supervisor_answer(
        self, run_id: str, lane_id: str | None, message_id: str, body: str
    ) -> None:
        """Record that the supervisor answered a blocked agent.

        Without this the only trace that a supervisor redirected a lane lives in
        Orca's message log, so `show` cannot reconstruct why the lane changed
        direction. The body is kept whole on purpose: a direction that shaped
        what landed is exactly the thing an audit needs to read back.
        """

        with self._session() as session:
            self._event(
                session,
                run_id,
                lane_id,
                "supervisor_answered",
                {"message_id": message_id, "body": body},
            )
            session.commit()

    def record_escalation(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        decision: EscalationDecision,
    ) -> None:
        with self._session() as session:
            existing = session.exec(
                select(EscalationRow).where(EscalationRow.stage_id == stage.stage_id)
            ).first()
            created = self._record_contract(
                session,
                existing,
                EscalationRow(
                    escalation_id=str(uuid.uuid4()),
                    finding_key=finding.finding_key,
                    round=decision.round,
                    reason=decision.reason,
                    stage_id=stage.stage_id,
                    payload_json=decision.model_dump_json(),
                    created_at=_now(),
                ),
                "escalations",
            )
            if created:
                self._event(
                    session,
                    run_id,
                    finding.lane_id,
                    "escalation_recorded",
                    {
                        "finding_id": finding.finding_id,
                        "round": decision.round,
                        "reason": decision.reason,
                        "action": decision.action,
                    },
                )

    def integration_conflicts(self, finding_key: str) -> int:
        """Count how many times this finding's fix has actually failed to land.

        The integrations ledger is unique on (finding_key, round), so a fix that
        conflicts repeatedly inside one round leaves a single row and cannot be
        counted from there. The adjudications can, and `_adjudications` explains
        why they are counted from the event log rather than from the ledger.
        """

        return len(
            self._adjudications(finding_key, reason=FindingReason.INTEGRATION_CONFLICT.value)
        )

    def latest_fix_attempt(self, finding_key: str) -> FixAttempt | None:
        with self._session() as session:
            rows = list(
                session.exec(
                    select(FixAttemptRow)
                    .where(FixAttemptRow.finding_key == finding_key)
                    .order_by(col(FixAttemptRow.round).desc())
                ).all()
            )
            if not rows:
                return None
            rows.sort(
                key=lambda row: (row.round, row.attempt_kind == "fallback", row.created_at),
                reverse=True,
            )
            return FixAttempt.model_validate_json(rows[0].payload_json)

    def fix_attempt_count(self, finding_key: str, round: int) -> int:
        """Count the fix attempts this finding actually recorded at one round."""

        with self._session() as session:
            return len(
                session.exec(
                    select(FixAttemptRow).where(
                        FixAttemptRow.finding_key == finding_key,
                        FixAttemptRow.round == round,
                    )
                ).all()
            )

    def escalation_action_count(
        self, finding_key: str, round: int, reason: str, action: str
    ) -> int:
        """Count how often one trigger has already drawn the same adjudicated verdict."""

        return len(self._adjudications(finding_key, round=round, reason=reason, action=action))

    def _adjudications(
        self,
        finding_key: str,
        *,
        round: int | None = None,
        reason: str | None = None,
        action: str | None = None,
    ) -> list[dict[str, object]]:
        """Every adjudication this finding has drawn, from the append-only log.

        Not from the escalations ledger. A reopen deletes every escalation row at
        or past the reopened round, which is correct for the frozen contract - a
        re-adjudication has to be free to record its own verdict under the same
        stage. It is wrong for a bound. The bounds above exist to stop a verdict
        that settles nothing from being asked for again, and reading them from a
        table the reopen empties means the owner unsticking a finding silently
        hands the graph an unlimited budget for the exact verdict that stuck it.
        Run 1f13dd37 did this three times: thirteen accept_fix adjudications on
        one finding, of which the ledger retained one.

        Events are never deleted, so the count survives the reopen. They carry
        `finding_id` rather than `finding_key`, and that is unique per lane, so
        scoping the scan to the finding's own lane is what makes the two the same
        subject.
        """

        with self._session() as session:
            finding = session.get(FindingRow, finding_key)
            if finding is None:
                raise KeyError(f"unknown finding {finding_key}")
            rows = session.exec(
                select(EventRow).where(
                    EventRow.lane_id == finding.lane_id,
                    EventRow.kind == "escalation_recorded",
                )
            ).all()
        matched: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row.payload_json)
            if not isinstance(payload, dict):
                continue
            if payload.get("finding_id") != finding.finding_id:
                continue
            if round is not None and payload.get("round") != round:
                continue
            if reason is not None and payload.get("reason") != reason:
                continue
            if action is not None and payload.get("action") != action:
                continue
            matched.append(payload)
        return matched

    def fix_attempt(self, finding_key: str, round: int) -> FixAttempt | None:
        with self._session() as session:
            rows = list(
                session.exec(
                    select(FixAttemptRow).where(
                        FixAttemptRow.finding_key == finding_key,
                        FixAttemptRow.round == round,
                    )
                ).all()
            )
            if not rows:
                return None
            rows.sort(
                key=lambda row: (row.attempt_kind == "fallback", row.created_at), reverse=True
            )
            return FixAttempt.model_validate_json(rows[0].payload_json)

    def record_scope_check(
        self,
        run_id: str,
        finding: FindingRecord,
        *,
        declared_paths: list[str],
        actual_paths: list[str],
        accepted: bool,
    ) -> None:
        self._append_event(
            run_id,
            finding.lane_id,
            "fixer_scope_checked",
            {
                "finding_id": finding.finding_id,
                "round": finding.round,
                "declared_paths": declared_paths,
                "actual_paths": actual_paths,
                "accepted": accepted,
            },
        )

    def record_unprovable_findings(
        self,
        run_id: str,
        lane_id: str,
        *,
        origin: str,
        dropped: list[tuple[str, str]],
    ) -> None:
        """Record findings set aside because this runner cannot prove them.

        These were reported by a reviewer that read the changeset, so they are
        not noise. They are just not actionable under a shell-free runner, and
        the alternative to writing them down is discarding them silently.
        """

        self._append_event(
            run_id,
            lane_id,
            "findings_unprovable",
            {
                "origin": origin,
                "dropped": [
                    {"finding_id": finding_id, "reason": reason}
                    for finding_id, reason in dropped
                ],
            },
        )

    def integration(self, finding_key: str, round: int) -> IntegrationRecord | None:
        with self._session() as session:
            row = session.exec(
                select(IntegrationRow).where(
                    IntegrationRow.finding_key == finding_key, IntegrationRow.round == round
                )
            ).first()
            return None if row is None else _integration(row)

    def integrated_finding_ids(self, run_id: str, lane_id: str) -> set[str]:
        """Every finding in one lane whose fix actually reached the lane checkout.

        A finding is resolved by an integration receipt, and one receipt can carry
        several findings: the fixer commit it integrates may answer predecessors
        the adjudicator folded into it. So membership is the union of the receipt's
        own finding and everything it names as a source, and only an `integrated`
        receipt counts - `conflict` and `validation_failed` mean the lane checkout
        never moved.
        """

        integrated: set[str] = set()
        with self._session() as session:
            rows = session.exec(
                select(IntegrationRow, FindingRow)
                .join(FindingRow, col(FindingRow.finding_key) == col(IntegrationRow.finding_key))
                .join(LaneRow, col(LaneRow.lane_id) == col(IntegrationRow.lane_id))
                .where(
                    LaneRow.run_id == run_id,
                    IntegrationRow.lane_id == lane_id,
                    IntegrationRow.status == "integrated",
                )
            ).all()
        for integration, finding in rows:
            integrated.add(finding.finding_id)
            integrated.update(_integration(integration).source_finding_ids)
        return integrated

    def integrations(self, run_id: str) -> list[IntegrationRecord]:
        with self._session() as session:
            rows = session.exec(
                select(IntegrationRow)
                .join(LaneRow, col(LaneRow.lane_id) == col(IntegrationRow.lane_id))
                .where(LaneRow.run_id == run_id)
                .order_by(col(IntegrationRow.created_at), col(IntegrationRow.integration_id))
            ).all()
            return [_integration(row) for row in rows]

    def begin_integration(
        self,
        run_id: str,
        finding: FindingRecord,
        *,
        fixer_commit_sha: str,
        source_commits: list[str],
        source_finding_ids: list[str],
        base_sha: str,
    ) -> IntegrationRecord:
        with self._session(immediate=True) as session:
            existing = session.exec(
                select(IntegrationRow).where(
                    IntegrationRow.finding_key == finding.finding_key,
                    IntegrationRow.round == finding.round,
                )
            ).first()
            if existing is not None:
                record = _integration(existing)
                if record.fixer_commit_sha != fixer_commit_sha or record.base_sha != base_sha:
                    raise ValueError("integration identity changed after reservation")
                if (
                    record.source_commits != source_commits
                    or record.source_finding_ids != source_finding_ids
                ):
                    raise ValueError("integration sources changed after reservation")
                return record
            active = session.exec(
                select(IntegrationRow).where(
                    IntegrationRow.lane_id == finding.lane_id,
                    IntegrationRow.status == "starting",
                )
            ).first()
            if active is not None:
                raise IntegrationBusyError(f"lane {finding.lane_id} is integrating another finding")
            row = IntegrationRow(
                integration_id=str(uuid.uuid4()),
                finding_key=finding.finding_key,
                lane_id=finding.lane_id,
                round=finding.round,
                fixer_commit_sha=fixer_commit_sha,
                source_commits_json=json.dumps(source_commits),
                source_finding_ids_json=json.dumps(source_finding_ids),
                base_sha=base_sha,
                status="starting",
                validation_json="[]",
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(row)
            self._event(
                session,
                run_id,
                finding.lane_id,
                "integration_started",
                {"finding_id": finding.finding_id, "commit_sha": fixer_commit_sha},
            )
            session.flush()
            return _integration(row)

    def reopen_integration(
        self,
        run_id: str,
        finding: FindingRecord,
        *,
        attempt: FixAttemptIdentity | None = None,
    ) -> IntegrationRecord | None:
        """Reopen a settled integration whose verdict no longer describes the fix.

        A conflict and a failed validation are facts about one attempt against one
        lane head, not about the fix itself. When an adjudicator reads the commit
        and accepts it anyway, replaying the stored verdict makes that acceptance a
        no-op: the caller returns before it reaches Git, so a repaired validation
        contract or a moved lane head is never exercised and the finding escalates
        on the same frozen output until it blocks. Put the receipt back to
        ``starting`` so the next pass actually re-checks it. An integrated receipt
        is never reopened, because that one is genuinely terminal.

        `attempt` re-points the slot at a commit that did not exist when the
        verdict was written. A conflict retry stays in its own round on purpose -
        a lane head moving is not the fixer thrashing - and this table is unique
        on (finding, round), so the retry has no slot of its own and would
        otherwise be refused as "sources changed after persistence" for having
        done exactly what it was sent to do. The identity it replaces goes into
        the event, so the superseded attempt is still on the record.
        """

        with self._session(immediate=True) as session:
            row = session.exec(
                select(IntegrationRow).where(
                    IntegrationRow.finding_key == finding.finding_key,
                    IntegrationRow.round == finding.round,
                )
            ).first()
            if row is None or row.status not in {"conflict", "validation_failed"}:
                return None
            active = session.exec(
                select(IntegrationRow).where(
                    IntegrationRow.lane_id == finding.lane_id,
                    IntegrationRow.status == "starting",
                )
            ).first()
            if active is not None:
                raise IntegrationBusyError(f"lane {finding.lane_id} is integrating another finding")
            superseded: dict[str, object] = {"previous_status": row.status}
            if attempt is not None and attempt.fixer_commit_sha != row.fixer_commit_sha:
                superseded["previous_commit_sha"] = row.fixer_commit_sha
                superseded["previous_base_sha"] = row.base_sha
                row.fixer_commit_sha = attempt.fixer_commit_sha
                row.base_sha = attempt.base_sha
                row.source_commits_json = json.dumps(attempt.source_commits)
                row.source_finding_ids_json = json.dumps(attempt.source_finding_ids)
                row.integrated_sha = None
            row.status = "starting"
            row.validation_json = "[]"
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                finding.lane_id,
                "integration_reopened",
                {"finding_id": finding.finding_id, **superseded},
            )
            session.flush()
            return _integration(row)

    def finish_integration(
        self,
        run_id: str,
        finding: FindingRecord,
        *,
        status: Literal["integrated", "conflict", "validation_failed"],
        integrated_sha: str | None,
        validation_results: list[ValidationResult],
    ) -> None:
        with self._session() as session:
            row = session.exec(
                select(IntegrationRow).where(
                    IntegrationRow.finding_key == finding.finding_key,
                    IntegrationRow.round == finding.round,
                )
            ).first()
            if row is None or row.status != "starting":
                if row is None or row.status != status:
                    raise ValueError("integration changed after settlement")
                return
            row.status = status
            row.integrated_sha = integrated_sha
            row.validation_json = json.dumps(
                [item.model_dump(mode="json") for item in validation_results]
            )
            row.updated_at = _now()
            if status in {"integrated", "validation_failed"}:
                if integrated_sha is None:
                    raise ValueError("applied integration receipt requires a head SHA")
                lane = session.get(LaneRow, finding.lane_id)
                if lane is None:
                    raise KeyError(f"unknown lane {finding.lane_id}")
                lane.integration_head_sha = integrated_sha
                lane.updated_at = _now()
            self._event(
                session,
                run_id,
                finding.lane_id,
                "integration_settled",
                {
                    "finding_id": finding.finding_id,
                    "status": status,
                    "integrated_sha": integrated_sha,
                },
            )

    def mark_released(self, run_id: str, stage_id: str) -> None:
        with self._session() as session:
            row = session.get(WorkflowStageRow, stage_id)
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            if row.released:
                return
            row.released = True
            row.updated_at = _now()
            self._event(
                session,
                run_id,
                row.lane_id,
                "worker_released",
                {"stage_id": stage_id},
            )

    def set_lane_phase(self, run_id: str, lane_id: str, phase: LanePhase) -> None:
        with self._session() as session:
            row = session.get(LaneRow, lane_id)
            if row is None:
                raise KeyError(f"unknown lane {lane_id}")
            if row.phase == phase.value:
                return
            row.phase = phase.value
            row.updated_at = _now()
            self._event(session, run_id, lane_id, "lane_transitioned", {"phase": phase.value})

    def block_lane(self, run_id: str, lane_id: str, reason: str) -> None:
        """Record that this lane is blocked, once per thing that blocks it.

        A block is not an event that keeps happening. Every monitor tick
        re-derives the same block from the same unchanged stage and asked for it
        to be recorded again, so run 88360089 carries 214 `lane_blocked` rows
        describing three situations. The log is where the run is reconstructed
        afterwards, and at that ratio the reconstruction is mostly noise.

        Write when the lane enters the phase, and again whenever the reason
        changes, because a lane blocked for a new cause has genuinely had
        something happen to it. Repeating an unchanged reason has not.
        """

        reason = reason[:4_000]
        with self._session() as session:
            row = session.get(LaneRow, lane_id)
            if row is None:
                raise KeyError(f"unknown lane {lane_id}")
            entering = row.phase != LanePhase.BLOCKED.value
            if entering:
                row.phase = LanePhase.BLOCKED.value
                row.updated_at = _now()
                self._event(
                    session,
                    run_id,
                    lane_id,
                    "lane_transitioned",
                    {"phase": LanePhase.BLOCKED.value},
                )
            elif self._latest_block_reason(session, run_id, lane_id) == reason:
                return
            self._event(session, run_id, lane_id, "lane_blocked", {"reason": reason})

    @staticmethod
    def _latest_block_reason(session: Session, run_id: str, lane_id: str) -> str | None:
        """Return the reason on this lane's most recent block, if it has one."""

        row = session.exec(
            select(EventRow)
            .where(
                EventRow.run_id == run_id,
                EventRow.lane_id == lane_id,
                EventRow.kind == "lane_blocked",
            )
            .order_by(col(EventRow.created_at).desc(), col(EventRow.event_id).desc())
        ).first()
        if row is None:
            return None
        payload = json.loads(row.payload_json)
        return payload.get("reason") if isinstance(payload, dict) else None

    def note_publication_error(self, run_id: str, lane_id: str, detail: str) -> int:
        """Record one failed publication pass, and return how many in a row that is.

        "GitHub has not answered yet" and "GitHub says this failed" are different
        facts, and only the second is the lane's problem. Telling them apart by
        reading GitHub's error strings is guesswork that goes stale, so count
        them instead: one failed observation is not knowledge, several in a row
        is. That needs no taxonomy and cannot be wrong about a message nobody
        has seen yet.

        The streak is read from the append-only log and cleared by
        `note_publication_progress`, so it survives a restart and cannot drift
        out of step with a column somebody forgot to reset.
        """

        with self._session() as session:
            self._event(
                session, run_id, lane_id, "lane_publication_error", {"detail": detail[:4_000]}
            )
            session.commit()
            return self._publication_error_streak(session, run_id, lane_id)

    def note_publication_progress(self, run_id: str, lane_id: str) -> None:
        """Record that this lane's publication pass got an answer, ending any streak."""

        with self._session() as session:
            if self._publication_error_streak(session, run_id, lane_id) == 0:
                return
            self._event(session, run_id, lane_id, "lane_publication_progress", {})

    @staticmethod
    def _publication_error_streak(session: Session, run_id: str, lane_id: str) -> int:
        rows = session.exec(
            select(EventRow)
            .where(
                EventRow.run_id == run_id,
                EventRow.lane_id == lane_id,
                col(EventRow.kind).in_(["lane_publication_error", "lane_publication_progress"]),
            )
            .order_by(col(EventRow.created_at).desc(), col(EventRow.event_id).desc())
        ).all()
        streak = 0
        for row in rows:
            if row.kind != "lane_publication_error":
                break
            streak += 1
        return streak

    def resume_lane(self, run_id: str, lane_id: str, note: str) -> None:
        """Lift a lane block, and clear the run status that block wrote.

        Both halves, because either one alone leaves the owner somewhere they
        cannot get out of. A lane left BLOCKED is skipped by publication; a run
        row left `blocked` reads as stopped even once every lane is healthy.
        """

        with self._session() as session:
            lane = session.get(LaneRow, lane_id)
            if lane is None:
                raise KeyError(f"unknown lane {lane_id}")
            if lane.phase != LanePhase.BLOCKED.value:
                raise ValueError(f"lane {lane.name} is {lane.phase}, not blocked")
            failed_lane_stages = session.exec(
                select(WorkflowStageRow).where(
                    WorkflowStageRow.lane_id == lane_id,
                    WorkflowStageRow.finding_id.is_(None),
                    WorkflowStageRow.phase == StagePhase.FAILED.value,
                )
            ).all()
            for stage in failed_lane_stages:
                # Keep the failed attempt as evidence, but move its key out of
                # the scheduler's live namespace. `resume` is the explicit
                # supervisor recovery action for a lane-level stage failure.
                stage_key = stage.stage_key
                stage.stage_key = f"{stage_key}:resumed{_now():%Y%m%d%H%M%S%f}"
                stage.updated_at = _now()
                self._insert_stage(
                    session,
                    lane_id=lane_id,
                    stage_key=stage_key,
                    role=StageKind(stage.role),
                    attempt_kind=(
                        None if stage.attempt_kind is None else AttemptKind(stage.attempt_kind)
                    ),
                )
                session.flush()
                replacement = session.exec(
                    select(WorkflowStageRow).where(WorkflowStageRow.stage_key == stage_key)
                ).one()
                replacement.phase = StagePhase.READY.value
                replacement.updated_at = _now()
                self._event(
                    session,
                    run_id,
                    lane_id,
                    "stage_created",
                    {"stage_key": stage_key, "role": stage.role},
                )
            lane.phase = LanePhase.ACTIVE.value
            lane.updated_at = _now()
            run = session.get(SupervisorRunRow, run_id)
            if run is not None and run.status in {"blocked", "failed"}:
                run.status = "active"
                run.updated_at = _now()
            self._event(
                session,
                run_id,
                lane_id,
                "supervisor_resumed_lane",
                {"lane": lane.name, "note": note[:4_000]},
            )

    def set_terminal_status(self, run_id: str, status: str) -> None:
        if status not in {"complete", "failed", "blocked"}:
            raise ValueError(f"invalid terminal status {status}")
        with self._session() as session:
            row = session.get(SupervisorRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id}")
            if row.status == status:
                return
            row.status = status
            row.updated_at = _now()
            self._event(session, run_id, None, "run_terminal", {"status": status})

    def events(self, run_id: str) -> list[dict[str, object]]:
        with self._session() as session:
            rows = session.exec(
                select(EventRow)
                .where(EventRow.run_id == run_id)
                .order_by(col(EventRow.created_at), col(EventRow.event_id))
            ).all()
            return [{"kind": row.kind, "payload": json.loads(row.payload_json)} for row in rows]

    def counts(self) -> dict[str, int]:
        with self._session() as session:
            return {
                "runs": session.exec(select(func.count()).select_from(SupervisorRunRow)).one(),
                "lanes": session.exec(select(func.count()).select_from(LaneRow)).one(),
                "stages": session.exec(select(func.count()).select_from(WorkflowStageRow)).one(),
                "findings": session.exec(select(func.count()).select_from(FindingRow)).one(),
            }

    def active_worker_count(self) -> int:
        with self._session() as session:
            return session.exec(
                select(func.count())
                .select_from(WorkflowStageRow)
                .where(
                    col(WorkflowStageRow.phase).in_(
                        [StagePhase.STARTING.value, StagePhase.DISPATCHED.value]
                    )
                )
            ).one()

    def active_lane_ids(self) -> set[str]:
        with self._session() as session:
            return set(
                session.exec(
                    select(WorkflowStageRow.lane_id)
                    .where(
                        col(WorkflowStageRow.phase).in_(
                            [StagePhase.STARTING.value, StagePhase.DISPATCHED.value]
                        )
                    )
                    .distinct()
                ).all()
            )

    @staticmethod
    def _record_contract(
        session: Session,
        existing: ContractRow | None,
        row: ContractRow,
        table_name: str,
    ) -> bool:
        """Freeze one stage's contract against its own replay.

        The subject is the stage, not the round. A round can legitimately be run
        twice - a fix rebased after a conflict, an adjudication of a fix that
        conflicted again - and each run records its own verdict. What must never
        change is what a single stage said once it has been persisted.
        """

        if existing is not None:
            if existing.payload_json != row.payload_json:
                raise ValueError(f"{table_name} contract changed after persistence")
            return False
        session.add(row)
        return True

    def _append_event(self, run_id: str, lane_id: str | None, kind: str, payload: object) -> None:
        with self._session() as session:
            self._event(session, run_id, lane_id, kind, payload)

    @staticmethod
    def _insert_stage(
        session: Session,
        *,
        lane_id: str,
        stage_key: str,
        role: StageKind,
        finding_key: str | None = None,
        finding_id: str | None = None,
        round: int | None = None,
        attempt_kind: AttemptKind | None = None,
    ) -> None:
        now = _now()
        session.add(
            WorkflowStageRow(
                stage_id=str(uuid.uuid4()),
                stage_key=stage_key,
                lane_id=lane_id,
                role=role.value,
                finding_key=finding_key,
                finding_id=finding_id,
                round=round,
                attempt_kind=attempt_kind.value if attempt_kind is not None else None,
                phase=StagePhase.PENDING.value,
                created_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    def _insert_finding(
        session: Session,
        lane_id: str,
        finding: ReviewFinding,
        *,
        origin: FindingOrigin,
        now: datetime,
    ) -> bool:
        payload = finding.model_dump_json()
        existing = session.exec(
            select(FindingRow).where(
                FindingRow.lane_id == lane_id, FindingRow.finding_id == finding.id
            )
        ).first()
        if existing is not None:
            if existing.contract_json != payload or existing.origin != origin:
                raise ValueError(f"finding {finding.id} changed after it was frozen")
            return False
        phase = FindingPhase.DEFERRED if origin == "unrelated" else FindingPhase.PENDING_FIX
        session.add(
            FindingRow(
                finding_key=str(uuid.uuid4()),
                lane_id=lane_id,
                finding_id=finding.id,
                origin=origin,
                contract_json=payload,
                effective_contract_json=payload,
                phase=phase.value,
                round=1,
                created_at=now,
                updated_at=now,
            )
        )
        return True

    @contextmanager
    def _session(self, *, immediate: bool = False) -> Iterator[Session]:
        with Session(self._engine, expire_on_commit=False) as session:
            if immediate:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    _CONTRACT_TABLES = ("fix_attempts", "re_reviews", "escalations")
    """Tables whose contract identity moved from the round to the stage."""

    def _migrate_contract_uniqueness(self) -> None:
        """Rebuild contract tables that still make a round unique.

        A round can legitimately be run twice, so the older unique keys rejected
        an honest second verdict as a changed contract and stranded the finding.
        Rebuild in place: the rows are kept, only the constraint changes.
        """

        inspector = inspect(self._engine)
        for table in self._CONTRACT_TABLES:
            if not inspector.has_table(table):
                continue
            superseded = f"{table}_superseded"
            constraints = inspector.get_unique_constraints(table)
            rebuilt = all(item["column_names"] == ["stage_id"] for item in constraints)
            if rebuilt and not inspector.has_table(superseded):
                continue
            columns = [item["name"] for item in inspector.get_columns(table)]
            names = ", ".join(columns)
            if not rebuilt:
                indexes = [item["name"] for item in inspector.get_indexes(table) if item["name"]]
                with self._engine.begin() as connection:
                    # A renamed table keeps its indexes, and create_all would
                    # then refuse to build the replacement's own.
                    for index in indexes:
                        connection.exec_driver_sql(f"DROP INDEX {index}")
                    connection.exec_driver_sql(f"ALTER TABLE {table} RENAME TO {superseded}")
                SQLModel.metadata.create_all(self._engine)
            # Finish a rebuild that was interrupted between the rename and the
            # copy, and never overwrite a row the rebuilt table already holds.
            with self._engine.begin() as connection:
                connection.exec_driver_sql(
                    f"INSERT INTO {table} ({names}) SELECT {names} FROM {superseded} old "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {table} new "
                    f"WHERE new.stage_id = old.stage_id)"
                )
                connection.exec_driver_sql(f"DROP TABLE {superseded}")

    def _migrate_additive_columns(self) -> None:
        """Keep existing v2 databases readable without destructive migration."""

        columns = {
            "supervisor_runs": (("orca_run_id", "TEXT"),),
            "lanes": (
                ("base_ref", "TEXT NOT NULL DEFAULT 'HEAD'"),
                ("base_sha", "TEXT"),
                ("review_head_sha", "TEXT"),
                ("integration_head_sha", "TEXT"),
            ),
            "workflow_stages": (
                ("worktree_id", "TEXT"),
                ("orca_terminal_handle", "TEXT"),
                ("start_head_sha", "TEXT"),
            ),
            "findings": (("dispatch_base_sha", "TEXT"),),
            "acceptance_authorizations": (("config_json", "TEXT"),),
            "integrations": (
                ("source_commits_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("source_finding_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ),
        }
        inspector = inspect(self._engine)
        with self._engine.begin() as connection:
            for table, additions in columns.items():
                if not inspector.has_table(table):
                    continue
                existing = {item["name"] for item in inspector.get_columns(table)}
                for name, declaration in additions:
                    if name not in existing:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )

    @staticmethod
    def _event(
        session: Session,
        run_id: str,
        lane_id: str | None,
        kind: str,
        payload: object,
    ) -> None:
        session.add(
            EventRow(
                event_id=str(uuid.uuid4()),
                run_id=run_id,
                lane_id=lane_id,
                kind=kind,
                payload_json=json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), default=str
                ),
                created_at=_now(),
            )
        )


def _run(row: SupervisorRunRow) -> RunRecord:
    return RunRecord(
        run_id=row.run_id,
        status=row.status,
        orca_run_id=row.orca_run_id,
        proposal=SupervisorPlan.model_validate_json(row.plan_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _lane(row: LaneRow) -> LaneRecord:
    return LaneRecord(
        lane_id=row.lane_id,
        run_id=row.run_id,
        name=row.name,
        issue_id=row.issue_id,
        repo_selector=row.repo_selector,
        base_ref=row.base_ref,
        base_sha=row.base_sha,
        phase=LanePhase(row.phase),
        worktree_id=row.worktree_id,
        review_head_sha=row.review_head_sha,
        integration_head_sha=row.integration_head_sha,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _stage(row: WorkflowStageRow) -> StageRecord:
    return StageRecord(
        stage_id=row.stage_id,
        stage_key=row.stage_key,
        lane_id=row.lane_id,
        role=StageKind(row.role),
        finding_id=row.finding_id,
        round=row.round,
        attempt_kind=AttemptKind(row.attempt_kind) if row.attempt_kind is not None else None,
        phase=StagePhase(row.phase),
        orca_task_id=row.orca_task_id,
        orca_dispatch_id=row.orca_dispatch_id,
        orca_terminal_handle=row.orca_terminal_handle,
        worktree_id=row.worktree_id,
        start_head_sha=row.start_head_sha,
        result_json=row.result_json,
        processed=row.processed,
        released=row.released,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _finding(row: FindingRow) -> FindingRecord:
    return FindingRecord(
        finding_key=row.finding_key,
        lane_id=row.lane_id,
        finding_id=row.finding_id,
        origin=row.origin,
        contract=ReviewFinding.model_validate_json(row.contract_json),
        effective_contract=ReviewFinding.model_validate_json(row.effective_contract_json),
        phase=FindingPhase(row.phase),
        round=row.round,
        escalation_reason=(
            FindingReason(row.escalation_reason) if row.escalation_reason is not None else None
        ),
        dispatch_base_sha=row.dispatch_base_sha,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _integration(row: IntegrationRow) -> IntegrationRecord:
    return IntegrationRecord(
        integration_id=row.integration_id,
        finding_key=row.finding_key,
        lane_id=row.lane_id,
        round=row.round,
        fixer_commit_sha=row.fixer_commit_sha,
        source_commits=[str(item) for item in json.loads(row.source_commits_json)],
        source_finding_ids=[str(item) for item in json.loads(row.source_finding_ids_json)],
        base_sha=row.base_sha,
        integrated_sha=row.integrated_sha,
        status=row.status,
        validation_results=[
            ValidationResult.model_validate(item) for item in json.loads(row.validation_json)
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
