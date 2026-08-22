"""SQLModel correlation ledger for dynamic orkastrator convergence workflows."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

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
    EscalationDecision,
    FindingPhase,
    FindingReason,
    FindingRecord,
    FixAttempt,
    InitialReviewReport,
    IntegrationRecord,
    LanePhase,
    LaneRecord,
    OrcaWorkerResult,
    PublicationReceipt,
    ReReviewResult,
    ReviewFinding,
    RunRecord,
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
    "initial_review", "introduced_by_fix", "unrelated", "ci_failure", "worker_blocked"
]
ContractRow = TypeVar("ContractRow", FixAttemptRow, ReReviewRow, EscalationRow)


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
        self, run_id: str, authorization: AcceptanceAuthorization
    ) -> None:
        if authorization.run_id != run_id:
            raise ValueError("acceptance authorization does not match its run")
        payload = authorization.model_dump_json()
        with self._session() as session:
            row = session.get(AcceptanceAuthorizationRow, run_id)
            if row is not None:
                if row.payload_json != payload:
                    raise ValueError("accepted proposal or publication policy changed")
                return
            session.add(
                AcceptanceAuthorizationRow(run_id=run_id, payload_json=payload, created_at=_now())
            )
            self._event(session, run_id, None, "acceptance_authorized", authorization.model_dump())

    def acceptance_authorization(self, run_id: str) -> AcceptanceAuthorization | None:
        with self._session() as session:
            row = session.get(AcceptanceAuthorizationRow, run_id)
            return (
                None
                if row is None
                else AcceptanceAuthorization.model_validate_json(row.payload_json)
            )

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
                if previous.model_copy(update={"draft": receipt.draft}) != receipt:
                    raise ValueError("publication identity changed for an existing head")
                row.payload_json = payload
                row.updated_at = now
                kind = (
                    "pull_request_ready"
                    if previous.draft and not receipt.draft
                    else "lane_published"
                )
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
            row.worktree_id = worktree_id
            row.updated_at = _now()
            if row.role == StageKind.WORKER.value:
                lane = session.get(LaneRow, row.lane_id)
                if lane is None:
                    raise KeyError(f"unknown lane {row.lane_id}")
                lane.phase = LanePhase.ACTIVE.value
                lane.worktree_id = worktree_id
                lane.updated_at = _now()
            self._event(session, run_id, row.lane_id, "stage_started", payload)

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
            elif stage.lane_id in active_lanes:
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

    def record_fix_attempt(
        self, run_id: str, finding: FindingRecord, stage: StageRecord, attempt: FixAttempt
    ) -> None:
        if stage.attempt_kind is None:
            raise ValueError("fixer stage omitted attempt kind")
        with self._session() as session:
            existing = session.exec(
                select(FixAttemptRow).where(
                    FixAttemptRow.finding_key == finding.finding_key,
                    FixAttemptRow.round == attempt.round,
                    FixAttemptRow.attempt_kind == stage.attempt_kind.value,
                )
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

    def record_re_review(
        self, run_id: str, finding: FindingRecord, stage: StageRecord, result: ReReviewResult
    ) -> None:
        with self._session() as session:
            existing = session.exec(
                select(ReReviewRow).where(
                    ReReviewRow.finding_key == finding.finding_key,
                    ReReviewRow.round == result.round,
                )
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
                select(ReReviewRow).where(
                    ReReviewRow.finding_key == finding_key, ReReviewRow.round == round
                )
            ).first()
            return None if row is None else ReReviewResult.model_validate_json(row.payload_json)

    def record_escalation(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        decision: EscalationDecision,
    ) -> None:
        with self._session() as session:
            existing = session.exec(
                select(EscalationRow).where(
                    EscalationRow.finding_key == finding.finding_key,
                    EscalationRow.round == decision.round,
                    EscalationRow.reason == decision.reason,
                )
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
            rows.sort(key=lambda row: (row.round, row.attempt_kind == "fallback"), reverse=True)
            return FixAttempt.model_validate_json(rows[0].payload_json)

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
            rows.sort(key=lambda row: row.attempt_kind == "fallback", reverse=True)
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

    def integration(self, finding_key: str, round: int) -> IntegrationRecord | None:
        with self._session() as session:
            row = session.exec(
                select(IntegrationRow).where(
                    IntegrationRow.finding_key == finding_key, IntegrationRow.round == round
                )
            ).first()
            return None if row is None else _integration(row)

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
        self.set_lane_phase(run_id, lane_id, LanePhase.BLOCKED)
        self._append_event(run_id, lane_id, "lane_blocked", {"reason": reason[:4_000]})

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

    def _migrate_additive_columns(self) -> None:
        """Keep existing v2 databases readable without destructive migration."""

        columns = {
            "supervisor_runs": (("orca_run_id", "TEXT"),),
            "lanes": (
                ("base_ref", "TEXT NOT NULL DEFAULT 'HEAD'"),
                ("review_head_sha", "TEXT"),
                ("integration_head_sha", "TEXT"),
            ),
            "workflow_stages": (("worktree_id", "TEXT"),),
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
        worktree_id=row.worktree_id,
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
