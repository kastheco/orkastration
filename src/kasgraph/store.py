"""SQLite correlation ledger for dynamic Kasgraph convergence workflows."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from kasgraph.models import (
    AttemptKind,
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
from kasgraph.scope import scopes_overlap


class UnsupportedStateError(RuntimeError):
    """Raised when an accepted fixed-DAG database cannot resume safely."""


class IntegrationBusyError(RuntimeError):
    """Raised when another approved commit owns a lane's integration lock."""


FindingOrigin = Literal["initial_review", "introduced_by_fix", "unrelated"]


class StateStore:
    """Persist workflow evidence without replacing Orca as runtime authority."""

    def __init__(self, path: Path):
        self.path = path

    def setup(self) -> None:
        """Create the v2 schema and reject unsafe accepted fixed-DAG state."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(supervisor_runs)").fetchall()
            }
            if "orca_run_id" not in columns:
                connection.execute("ALTER TABLE supervisor_runs ADD COLUMN orca_run_id TEXT")
            lane_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(lanes)").fetchall()
            }
            for name, declaration in (
                ("base_ref", "TEXT NOT NULL DEFAULT 'HEAD'"),
                ("review_head_sha", "TEXT"),
                ("integration_head_sha", "TEXT"),
            ):
                if name not in lane_columns:
                    connection.execute(f"ALTER TABLE lanes ADD COLUMN {name} {declaration}")
            stage_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(workflow_stages)").fetchall()
            }
            if "worktree_id" not in stage_columns:
                connection.execute("ALTER TABLE workflow_stages ADD COLUMN worktree_id TEXT")
            integration_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(integrations)").fetchall()
            }
            for name in ("source_commits_json", "source_finding_ids_json"):
                if name not in integration_columns:
                    connection.execute(
                        f"ALTER TABLE integrations ADD COLUMN {name} TEXT NOT NULL DEFAULT '[]'"
                    )
            accepted = connection.execute(
                """
                SELECT runs.run_id FROM supervisor_runs AS runs
                WHERE runs.status IN ('proposed', 'active', 'blocked')
                  AND EXISTS (
                      SELECT 1 FROM lane_stages
                      JOIN lanes USING (lane_id)
                      WHERE lanes.run_id = runs.run_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM workflow_stages
                      JOIN lanes USING (lane_id)
                      WHERE lanes.run_id = runs.run_id
                  )
                LIMIT 1
                """
            ).fetchone()
            if accepted is not None:
                raise UnsupportedStateError(
                    "v1 fixed-stage state cannot resume in the v2 dynamic scheduler; "
                    f"finish or archive run {accepted['run_id']} with the previous version"
                )

    def record_proposal(self, proposal: SupervisorPlan) -> str:
        """Record a proposal with only its first worker stage materialized."""

        run_id = str(uuid.uuid4())
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO supervisor_runs
                    (run_id, objective, status, plan_json, orca_run_id, created_at, updated_at)
                VALUES (?, ?, 'proposed', ?, NULL, ?, ?)
                """,
                (run_id, proposal.objective, proposal.model_dump_json(), now, now),
            )
            for lane in proposal.lanes:
                lane_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO lanes
                        (lane_id, run_id, name, issue_id, repo_selector, base_ref, agent_id, phase,
                         worktree_id, review_head_sha, integration_head_sha, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'graph', ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        lane_id,
                        run_id,
                        lane.name,
                        lane.issue_id,
                        lane.repo_selector,
                        lane.base_ref,
                        LanePhase.PROPOSED.value,
                        now,
                        now,
                    ),
                )
                self._insert_stage(
                    connection,
                    lane_id=lane_id,
                    stage_key=f"{lane_id}:worker",
                    role=StageKind.WORKER,
                )
            self._event(connection, run_id, None, "proposal_recorded", proposal.model_dump())
        return run_id

    def run(self, run_id: str) -> RunRecord:
        """Return one recorded run."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM supervisor_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id}")
        return _run(row)

    def lanes(self, run_id: str | None = None) -> list[LaneRecord]:
        """Return lanes in stable creation order."""

        query = "SELECT * FROM lanes"
        arguments: tuple[str, ...] = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            arguments = (run_id,)
        query += " ORDER BY created_at, lane_id"
        with self._connection() as connection:
            rows = connection.execute(query, arguments).fetchall()
        return [_lane(row) for row in rows]

    def stages(self, run_id: str) -> list[StageRecord]:
        """Return every dynamically materialized stage for a run."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT workflow_stages.* FROM workflow_stages JOIN lanes USING (lane_id)
                WHERE lanes.run_id = ?
                ORDER BY lanes.created_at, workflow_stages.created_at, workflow_stages.stage_id
                """,
                (run_id,),
            ).fetchall()
        return [_stage(row) for row in rows]

    def findings(self, run_id: str, lane_id: str | None = None) -> list[FindingRecord]:
        """Return persisted finding contracts in stable creation order."""

        query = """
            SELECT findings.* FROM findings JOIN lanes USING (lane_id)
            WHERE lanes.run_id = ?
        """
        arguments: tuple[str, ...] = (run_id,)
        if lane_id is not None:
            query += " AND findings.lane_id = ?"
            arguments = (run_id, lane_id)
        query += " ORDER BY findings.created_at, findings.finding_key"
        with self._connection() as connection:
            rows = connection.execute(query, arguments).fetchall()
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
        """Create one deterministic stage or return its existing row."""

        with self._connection() as connection:
            created = self._insert_stage(
                connection,
                lane_id=lane_id,
                stage_key=stage_key,
                role=role,
                finding_key=finding_key,
                finding_id=finding_id,
                round=round,
                attempt_kind=attempt_kind,
            )
            if created:
                self._event(
                    connection,
                    run_id,
                    lane_id,
                    "stage_created",
                    {"stage_key": stage_key, "role": role.value},
                )
            row = connection.execute(
                "SELECT * FROM workflow_stages WHERE stage_key = ?", (stage_key,)
            ).fetchone()
        if row is None:
            raise RuntimeError(f"stage {stage_key} was not persisted")
        return _stage(row)

    def mark_accepted(self, run_id: str, orca_run_id: str) -> None:
        """Bind an accepted proposal to its authoritative Orca Run."""

        now = _now()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE supervisor_runs SET status = 'active', orca_run_id = ?, updated_at = ?
                WHERE run_id = ? AND status = 'proposed'
                """,
                (orca_run_id, now, run_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"run {run_id} is not awaiting acceptance")
            self._event(connection, run_id, None, "proposal_accepted", {"orca_run_id": orca_run_id})

    def bind_stage_task(self, run_id: str, stage_id: str, task_id: str) -> None:
        """Bind one local stage to its Orca Task idempotently."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lane_id, orca_task_id FROM workflow_stages WHERE stage_id = ?",
                (stage_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            if row["orca_task_id"] is not None:
                if str(row["orca_task_id"]) != task_id:
                    raise ValueError(f"stage {stage_id} is already bound")
                return
            connection.execute(
                """
                UPDATE workflow_stages SET orca_task_id = ?, phase = ?, updated_at = ?
                WHERE stage_id = ?
                """,
                (task_id, StagePhase.READY.value, now, stage_id),
            )
            self._event(
                connection,
                run_id,
                str(row["lane_id"]),
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
        """Record a started Dispatch and its lane worktree."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lane_id, role, orca_dispatch_id FROM workflow_stages WHERE stage_id = ?",
                (stage_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            existing = row["orca_dispatch_id"]
            if existing is not None:
                if str(existing) != dispatch_id:
                    raise ValueError(f"stage {stage_id} already has a dispatch")
                return
            lane_id = str(row["lane_id"])
            connection.execute(
                """
                UPDATE workflow_stages SET phase = ?, orca_dispatch_id = ?, worktree_id = ?,
                    updated_at = ?
                WHERE stage_id = ?
                """,
                (StagePhase.DISPATCHED.value, dispatch_id, worktree_id, now, stage_id),
            )
            if str(row["role"]) == StageKind.WORKER.value:
                connection.execute(
                    "UPDATE lanes SET phase = ?, worktree_id = ?, updated_at = ? WHERE lane_id = ?",
                    (LanePhase.ACTIVE.value, worktree_id, now, lane_id),
                )
            self._event(connection, run_id, lane_id, "stage_started", payload)

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
        """Atomically reserve global, lane, and fixer capacity before an external start."""

        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT phase FROM workflow_stages WHERE stage_id = ?", (stage.stage_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown stage {stage.stage_id}")
            if str(current["phase"]) != StagePhase.READY.value:
                return False
            active_phases = (StagePhase.STARTING.value, StagePhase.DISPATCHED.value)
            workers = int(
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_stages WHERE phase IN (?, ?)",
                    active_phases,
                ).fetchone()[0]
            )
            if workers >= max_workers:
                return False
            active_lane_rows = connection.execute(
                "SELECT DISTINCT lane_id FROM workflow_stages WHERE phase IN (?, ?)",
                active_phases,
            ).fetchall()
            active_lanes = {str(row["lane_id"]) for row in active_lane_rows}
            if stage.lane_id not in active_lanes and len(active_lanes) >= max_lanes:
                return False
            if stage.role is StageKind.FIXER:
                lane_fixers = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM workflow_stages
                        WHERE lane_id = ? AND role = ? AND phase IN (?, ?)
                        """,
                        (
                            stage.lane_id,
                            StageKind.FIXER.value,
                            *active_phases,
                        ),
                    ).fetchone()[0]
                )
                if lane_fixers >= max_lane_fixers:
                    return False
                active_finders = connection.execute(
                    """
                    SELECT findings.effective_contract_json FROM workflow_stages
                    JOIN findings USING (finding_key)
                    WHERE workflow_stages.lane_id = ? AND workflow_stages.role = ?
                      AND workflow_stages.phase IN (?, ?)
                    """,
                    (stage.lane_id, StageKind.FIXER.value, *active_phases),
                ).fetchall()
                for row in active_finders:
                    contract = ReviewFinding.model_validate_json(
                        str(row["effective_contract_json"])
                    )
                    if scopes_overlap(write_paths, tuple(contract.allowed_write_scope.paths)):
                        return False
            elif stage.lane_id in active_lanes:
                return False
            changed = connection.execute(
                """
                UPDATE workflow_stages SET phase = ?, updated_at = ?
                WHERE stage_id = ? AND phase = ?
                """,
                (
                    StagePhase.STARTING.value,
                    now,
                    stage.stage_id,
                    StagePhase.READY.value,
                ),
            ).rowcount
            if changed:
                self._event(
                    connection,
                    run_id,
                    stage.lane_id,
                    "stage_start_reserved",
                    {"stage_id": stage.stage_id},
                )
            return changed == 1

    def reset_stage_reservation(self, run_id: str, stage: StageRecord) -> None:
        """Return a crash-orphaned local reservation to ready when Orca has no Dispatch."""

        now = _now()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE workflow_stages SET phase = ?, updated_at = ?
                WHERE stage_id = ? AND phase = ? AND orca_dispatch_id IS NULL
                """,
                (
                    StagePhase.READY.value,
                    now,
                    stage.stage_id,
                    StagePhase.STARTING.value,
                ),
            ).rowcount
            if changed:
                self._event(
                    connection,
                    run_id,
                    stage.lane_id,
                    "stage_start_reservation_reset",
                    {"stage_id": stage.stage_id},
                )

    def sync_stage(
        self,
        run_id: str,
        stage_id: str,
        phase: StagePhase,
        result_json: str | None,
    ) -> None:
        """Idempotently reconcile a stage from authoritative Orca Task state."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lane_id, phase, result_json FROM workflow_stages WHERE stage_id = ?",
                (stage_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            current_result = row["result_json"]
            next_result = current_result if result_json is None else result_json
            if str(row["phase"]) == phase.value and current_result == next_result:
                return
            connection.execute(
                """
                UPDATE workflow_stages SET phase = ?, result_json = ?, updated_at = ?
                WHERE stage_id = ?
                """,
                (phase.value, next_result, now, stage_id),
            )
            self._event(
                connection,
                run_id,
                str(row["lane_id"]),
                "stage_reconciled",
                {"stage_id": stage_id, "phase": phase.value},
            )

    def record_lifecycle_receipt(
        self, run_id: str, stage: StageRecord, result: OrcaWorkerResult
    ) -> None:
        """Persist a validated worker report and mark its stage processed once."""

        if stage.orca_task_id is None:
            raise ValueError(f"stage {stage.stage_id} has no Orca task")
        payload = result.model_dump_json(by_alias=True)
        now = _now()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM lifecycle_receipts WHERE orca_task_id = ?",
                (stage.orca_task_id,),
            ).fetchone()
            if existing is not None and str(existing["payload_json"]) != payload:
                raise ValueError(f"Orca task {stage.orca_task_id} changed its lifecycle result")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO lifecycle_receipts
                        (receipt_id, stage_id, orca_task_id, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), stage.stage_id, stage.orca_task_id, payload, now),
                )
                self._event(
                    connection,
                    run_id,
                    stage.lane_id,
                    "lifecycle_receipt_recorded",
                    {"stage_id": stage.stage_id, "task_id": stage.orca_task_id},
                )
            connection.execute(
                "UPDATE workflow_stages SET processed = 1, updated_at = ? WHERE stage_id = ?",
                (now, stage.stage_id),
            )

    def mark_stage_processed(self, run_id: str, stage: StageRecord, reason: str) -> None:
        """Settle an invalid or failed stage so restart reconciliation stays idempotent."""

        now = _now()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE workflow_stages SET processed = 1, updated_at = ?
                WHERE stage_id = ? AND processed = 0
                """,
                (now, stage.stage_id),
            ).rowcount
            if changed:
                self._event(
                    connection,
                    run_id,
                    stage.lane_id,
                    "stage_result_rejected",
                    {"stage_id": stage.stage_id, "reason": reason},
                )

    def record_initial_review(self, run_id: str, lane_id: str, report: InitialReviewReport) -> None:
        """Freeze one immutable initial review and all of its findings."""

        payload = report.model_dump_json()
        now = _now()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT report_json FROM initial_reviews WHERE lane_id = ?", (lane_id,)
            ).fetchone()
            if existing is not None and str(existing["report_json"]) != payload:
                raise ValueError(f"lane {lane_id} initial review is already frozen")
            if existing is None:
                connection.execute(
                    "INSERT INTO initial_reviews VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), lane_id, payload, now),
                )
                for finding in report.findings:
                    self._insert_finding(
                        connection, lane_id, finding, origin="initial_review", now=now
                    )
                self._event(
                    connection,
                    run_id,
                    lane_id,
                    "initial_review_frozen",
                    {"finding_ids": [finding.id for finding in report.findings]},
                )

    def record_worker_result(self, run_id: str, lane_id: str, result: WorkerResult) -> None:
        """Persist the immutable changeset identity produced by the lane worker."""

        payload = result.model_dump_json()
        now = _now()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT result_json FROM worker_results WHERE lane_id = ?", (lane_id,)
            ).fetchone()
            if existing is not None and str(existing["result_json"]) != payload:
                raise ValueError(f"lane {lane_id} worker result changed after persistence")
            if existing is None:
                connection.execute(
                    "INSERT INTO worker_results VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), lane_id, payload, now),
                )
                self._event(
                    connection,
                    run_id,
                    lane_id,
                    "worker_result_frozen",
                    result.model_dump(mode="json"),
                )
                connection.execute(
                    """
                    UPDATE lanes SET review_head_sha = ?, integration_head_sha = ?, updated_at = ?
                    WHERE lane_id = ?
                    """,
                    (
                        result.review_revision.head_sha,
                        result.review_revision.head_sha,
                        now,
                        lane_id,
                    ),
                )

    def worker_result(self, lane_id: str) -> WorkerResult:
        """Return the exact worker changeset contract for initial-review pinning."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM worker_results WHERE lane_id = ?", (lane_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"lane {lane_id} has no worker result")
        return WorkerResult.model_validate_json(str(row["result_json"]))

    def add_finding(
        self,
        run_id: str,
        lane_id: str,
        finding: ReviewFinding,
        *,
        origin: FindingOrigin,
    ) -> FindingRecord:
        """Persist an introduced or deferred finding without changing prior contracts."""

        now = _now()
        with self._connection() as connection:
            created = self._insert_finding(connection, lane_id, finding, origin=origin, now=now)
            if created:
                self._event(
                    connection,
                    run_id,
                    lane_id,
                    "finding_recorded",
                    {"finding_id": finding.id, "origin": origin},
                )
            row = connection.execute(
                "SELECT * FROM findings WHERE lane_id = ? AND finding_id = ?",
                (lane_id, finding.id),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"finding {finding.id} was not persisted")
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
        """Advance a finding while keeping its original contract immutable."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM findings WHERE finding_key = ?", (finding_key,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown finding {finding_key}")
            next_round = int(row["round"]) if round is None else round
            contract_json = (
                str(row["effective_contract_json"])
                if effective_contract is None
                else effective_contract.model_dump_json()
            )
            if (
                str(row["phase"]) == phase.value
                and int(row["round"]) == next_round
                and row["escalation_reason"]
                == (escalation_reason.value if escalation_reason is not None else None)
                and str(row["effective_contract_json"]) == contract_json
            ):
                return
            connection.execute(
                """
                UPDATE findings SET phase = ?, round = ?, escalation_reason = ?,
                    effective_contract_json = ?, updated_at = ? WHERE finding_key = ?
                """,
                (
                    phase.value,
                    next_round,
                    escalation_reason.value if escalation_reason is not None else None,
                    contract_json,
                    now,
                    finding_key,
                ),
            )
            self._event(
                connection,
                run_id,
                str(row["lane_id"]),
                "finding_transitioned",
                {
                    "finding_id": str(row["finding_id"]),
                    "phase": phase.value,
                    "round": next_round,
                    "reason": escalation_reason.value if escalation_reason is not None else None,
                },
            )

    def record_fix_attempt(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        attempt: FixAttempt,
    ) -> None:
        """Persist one primary or fallback fixer result exactly once."""

        if stage.attempt_kind is None:
            raise ValueError("fixer stage omitted attempt kind")
        created = self._record_contract(
            table="fix_attempts",
            id_column="attempt_id",
            unique_columns=("finding_key", "round", "attempt_kind"),
            unique_values=(finding.finding_key, attempt.round, stage.attempt_kind.value),
            extra_columns=("stage_id",),
            extra_values=(stage.stage_id,),
            payload=attempt.model_dump_json(),
        )
        if created:
            self._append_event(
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
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        result: ReReviewResult,
    ) -> None:
        """Persist one scoped re-review verdict exactly once."""

        created = self._record_contract(
            table="re_reviews",
            id_column="review_id",
            unique_columns=("finding_key", "round"),
            unique_values=(finding.finding_key, result.round),
            extra_columns=("stage_id",),
            extra_values=(stage.stage_id,),
            payload=result.model_dump_json(),
        )
        if created:
            self._append_event(
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
        """Return a persisted verdict so partial result application can replay safely."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM re_reviews
                WHERE finding_key = ? AND round = ?
                """,
                (finding_key, round),
            ).fetchone()
        return None if row is None else ReReviewResult.model_validate_json(str(row["payload_json"]))

    def record_escalation(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        decision: EscalationDecision,
    ) -> None:
        """Persist one bounded escalation decision exactly once."""

        created = self._record_contract(
            table="escalations",
            id_column="escalation_id",
            unique_columns=("finding_key", "round", "reason"),
            unique_values=(finding.finding_key, decision.round, decision.reason),
            extra_columns=("stage_id",),
            extra_values=(stage.stage_id,),
            payload=decision.model_dump_json(),
        )
        if created:
            self._append_event(
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
        """Return the newest persisted fixer contract for re-review context."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM fix_attempts WHERE finding_key = ?
                ORDER BY round DESC,
                    CASE attempt_kind WHEN 'fallback' THEN 1 ELSE 0 END DESC LIMIT 1
                """,
                (finding_key,),
            ).fetchone()
        return None if row is None else FixAttempt.model_validate_json(str(row["payload_json"]))

    def fix_attempt(self, finding_key: str, round: int) -> FixAttempt | None:
        """Return the effective primary or fallback attempt for one semantic round."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM fix_attempts
                WHERE finding_key = ? AND round = ?
                ORDER BY CASE attempt_kind WHEN 'fallback' THEN 1 ELSE 0 END DESC LIMIT 1
                """,
                (finding_key, round),
            ).fetchone()
        return None if row is None else FixAttempt.model_validate_json(str(row["payload_json"]))

    def record_scope_check(
        self,
        run_id: str,
        finding: FindingRecord,
        *,
        declared_paths: list[str],
        actual_paths: list[str],
        accepted: bool,
    ) -> None:
        """Persist deterministic path-scope evidence for a fixer result."""

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
        """Return serial integration state for restart reconciliation."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM integrations WHERE finding_key = ? AND round = ?",
                (finding_key, round),
            ).fetchone()
        return None if row is None else _integration(row)

    def integrations(self, run_id: str) -> list[IntegrationRecord]:
        """Return every integration receipt for one run in stable order."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT integrations.* FROM integrations JOIN lanes USING (lane_id)
                WHERE lanes.run_id = ? ORDER BY integrations.created_at, integration_id
                """,
                (run_id,),
            ).fetchall()
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
        """Reserve one approved commit for serial lane integration."""

        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM integrations WHERE finding_key = ? AND round = ?",
                (finding.finding_key, finding.round),
            ).fetchone()
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
            active = connection.execute(
                """
                SELECT finding_key FROM integrations
                WHERE lane_id = ? AND status = 'starting' LIMIT 1
                """,
                (finding.lane_id,),
            ).fetchone()
            if active is not None:
                raise IntegrationBusyError(f"lane {finding.lane_id} is integrating another finding")
            integration_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO integrations
                    (integration_id, finding_key, lane_id, round, fixer_commit_sha,
                     source_commits_json, source_finding_ids_json, base_sha, integrated_sha,
                     status, validation_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'starting', '[]', ?, ?)
                """,
                (
                    integration_id,
                    finding.finding_key,
                    finding.lane_id,
                    finding.round,
                    fixer_commit_sha,
                    json.dumps(source_commits),
                    json.dumps(source_finding_ids),
                    base_sha,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                run_id,
                finding.lane_id,
                "integration_started",
                {"finding_id": finding.finding_id, "commit_sha": fixer_commit_sha},
            )
            row = connection.execute(
                "SELECT * FROM integrations WHERE integration_id = ?", (integration_id,)
            ).fetchone()
        assert row is not None
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
        """Settle an integration and advance the auditable lane head when accepted."""

        now = _now()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE integrations SET status = ?, integrated_sha = ?, validation_json = ?,
                    updated_at = ? WHERE finding_key = ? AND round = ? AND status = 'starting'
                """,
                (
                    status,
                    integrated_sha,
                    json.dumps([item.model_dump(mode="json") for item in validation_results]),
                    now,
                    finding.finding_key,
                    finding.round,
                ),
            ).rowcount
            if not changed:
                existing = connection.execute(
                    "SELECT * FROM integrations WHERE finding_key = ? AND round = ?",
                    (finding.finding_key, finding.round),
                ).fetchone()
                if existing is None or _integration(existing).status != status:
                    raise ValueError("integration changed after settlement")
                return
            if status in {"integrated", "validation_failed"}:
                if integrated_sha is None:
                    raise ValueError("applied integration receipt requires a head SHA")
                connection.execute(
                    "UPDATE lanes SET integration_head_sha = ?, updated_at = ? WHERE lane_id = ?",
                    (integrated_sha, now, finding.lane_id),
                )
            self._event(
                connection,
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
        """Record that Orca released a settled worker terminal."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lane_id, released FROM workflow_stages WHERE stage_id = ?", (stage_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown stage {stage_id}")
            if bool(row["released"]):
                return
            connection.execute(
                "UPDATE workflow_stages SET released = 1, updated_at = ? WHERE stage_id = ?",
                (now, stage_id),
            )
            self._event(
                connection,
                run_id,
                str(row["lane_id"]),
                "worker_released",
                {"stage_id": stage_id},
            )

    def set_lane_phase(self, run_id: str, lane_id: str, phase: LanePhase) -> None:
        """Persist one derived lane phase idempotently."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT phase FROM lanes WHERE lane_id = ?", (lane_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown lane {lane_id}")
            if str(row["phase"]) == phase.value:
                return
            connection.execute(
                "UPDATE lanes SET phase = ?, updated_at = ? WHERE lane_id = ?",
                (phase.value, now, lane_id),
            )
            self._event(connection, run_id, lane_id, "lane_transitioned", {"phase": phase.value})

    def set_terminal_status(self, run_id: str, status: str) -> None:
        """Set the run terminal state without erasing per-lane evidence."""

        if status not in {"complete", "failed", "blocked"}:
            raise ValueError(f"invalid terminal status {status}")
        now = _now()
        with self._connection() as connection:
            current = connection.execute(
                "SELECT status FROM supervisor_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown run {run_id}")
            if str(current["status"]) == status:
                return
            connection.execute(
                "UPDATE supervisor_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, now, run_id),
            )
            self._event(connection, run_id, None, "run_terminal", {"status": status})

    def events(self, run_id: str) -> list[dict[str, object]]:
        """Return transition evidence for tests and diagnostics."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT kind, payload_json FROM events
                WHERE run_id = ? ORDER BY created_at, event_id
                """,
                (run_id,),
            ).fetchall()
        return [
            {"kind": str(row["kind"]), "payload": json.loads(str(row["payload_json"]))}
            for row in rows
        ]

    def counts(self) -> dict[str, int]:
        """Return small diagnostics for doctor output."""

        with self._connection() as connection:
            return {
                "runs": int(
                    connection.execute("SELECT COUNT(*) FROM supervisor_runs").fetchone()[0]
                ),
                "lanes": int(connection.execute("SELECT COUNT(*) FROM lanes").fetchone()[0]),
                "stages": int(
                    connection.execute("SELECT COUNT(*) FROM workflow_stages").fetchone()[0]
                ),
                "findings": int(connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0]),
            }

    def active_worker_count(self) -> int:
        """Count dispatched workers across every accepted local run."""

        with self._connection() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM workflow_stages
                    WHERE phase IN ('starting', 'dispatched')
                    """
                ).fetchone()[0]
            )

    def active_lane_ids(self) -> set[str]:
        """Return lanes with at least one currently dispatched worker."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT lane_id FROM workflow_stages
                WHERE phase IN ('starting', 'dispatched')
                """
            ).fetchall()
        return {str(row["lane_id"]) for row in rows}

    def _record_contract(
        self,
        *,
        table: str,
        id_column: str,
        unique_columns: tuple[str, ...],
        unique_values: tuple[object, ...],
        extra_columns: tuple[str, ...],
        extra_values: tuple[object, ...],
        payload: str,
    ) -> bool:
        where = " AND ".join(f"{column} = ?" for column in unique_columns)
        columns = (id_column, *unique_columns, *extra_columns, "payload_json", "created_at")
        placeholders = ", ".join("?" for _ in columns)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE {where}",
                unique_values,
            ).fetchone()
            if row is not None:
                if str(row["payload_json"]) != payload:
                    raise ValueError(f"{table} contract changed after persistence")
                return False
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                (str(uuid.uuid4()), *unique_values, *extra_values, payload, _now()),
            )
        return True

    def _append_event(self, run_id: str, lane_id: str | None, kind: str, payload: object) -> None:
        with self._connection() as connection:
            self._event(connection, run_id, lane_id, kind, payload)

    @staticmethod
    def _insert_stage(
        connection: sqlite3.Connection,
        *,
        lane_id: str,
        stage_key: str,
        role: StageKind,
        finding_key: str | None = None,
        finding_id: str | None = None,
        round: int | None = None,
        attempt_kind: AttemptKind | None = None,
    ) -> bool:
        now = _now()
        changed = connection.execute(
            """
            INSERT OR IGNORE INTO workflow_stages
                (stage_id, stage_key, lane_id, role, finding_key, finding_id, round,
                 attempt_kind, phase, orca_task_id, orca_dispatch_id, result_json,
                 processed, released, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 0, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                stage_key,
                lane_id,
                role.value,
                finding_key,
                finding_id,
                round,
                attempt_kind.value if attempt_kind is not None else None,
                StagePhase.PENDING.value,
                now,
                now,
            ),
        ).rowcount
        return changed == 1

    @staticmethod
    def _insert_finding(
        connection: sqlite3.Connection,
        lane_id: str,
        finding: ReviewFinding,
        *,
        origin: FindingOrigin,
        now: str,
    ) -> bool:
        payload = finding.model_dump_json()
        existing = connection.execute(
            "SELECT contract_json, origin FROM findings WHERE lane_id = ? AND finding_id = ?",
            (lane_id, finding.id),
        ).fetchone()
        if existing is not None:
            if str(existing["contract_json"]) != payload or str(existing["origin"]) != origin:
                raise ValueError(f"finding {finding.id} changed after it was frozen")
            return False
        phase = FindingPhase.DEFERRED if origin == "unrelated" else FindingPhase.PENDING_FIX
        connection.execute(
            """
            INSERT INTO findings
                (finding_key, lane_id, finding_id, origin, contract_json,
                 effective_contract_json, phase, round, escalation_reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                lane_id,
                finding.id,
                origin,
                payload,
                payload,
                phase.value,
                now,
                now,
            ),
        )
        return True

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        run_id: str,
        lane_id: str | None,
        kind: str,
        payload: object,
    ) -> None:
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                run_id,
                lane_id,
                kind,
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                _now(),
            ),
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisor_runs (
    run_id TEXT PRIMARY KEY, objective TEXT NOT NULL, status TEXT NOT NULL,
    plan_json TEXT NOT NULL, orca_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lanes (
    lane_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES supervisor_runs(run_id),
    name TEXT NOT NULL, issue_id TEXT NOT NULL, repo_selector TEXT NOT NULL,
    base_ref TEXT NOT NULL DEFAULT 'HEAD', agent_id TEXT NOT NULL DEFAULT 'graph',
    phase TEXT NOT NULL, worktree_id TEXT, review_head_sha TEXT, integration_head_sha TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id, name)
);
CREATE TABLE IF NOT EXISTS lane_stages (
    stage_id TEXT PRIMARY KEY, lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
    role TEXT NOT NULL, phase TEXT NOT NULL, orca_task_id TEXT, orca_dispatch_id TEXT,
    released INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(lane_id, role)
);
CREATE TABLE IF NOT EXISTS findings (
    finding_key TEXT PRIMARY KEY, lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
    finding_id TEXT NOT NULL, origin TEXT NOT NULL, contract_json TEXT NOT NULL,
    effective_contract_json TEXT NOT NULL, phase TEXT NOT NULL, round INTEGER NOT NULL,
    escalation_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(lane_id, finding_id)
);
CREATE TABLE IF NOT EXISTS workflow_stages (
    stage_id TEXT PRIMARY KEY, stage_key TEXT NOT NULL UNIQUE,
    lane_id TEXT NOT NULL REFERENCES lanes(lane_id), role TEXT NOT NULL,
    finding_key TEXT REFERENCES findings(finding_key), finding_id TEXT, round INTEGER,
    attempt_kind TEXT, phase TEXT NOT NULL, orca_task_id TEXT UNIQUE,
    orca_dispatch_id TEXT UNIQUE, worktree_id TEXT, result_json TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    released INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS initial_reviews (
    review_id TEXT PRIMARY KEY, lane_id TEXT NOT NULL UNIQUE REFERENCES lanes(lane_id),
    report_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_results (
    result_id TEXT PRIMARY KEY, lane_id TEXT NOT NULL UNIQUE REFERENCES lanes(lane_id),
    result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fix_attempts (
    attempt_id TEXT PRIMARY KEY, finding_key TEXT NOT NULL REFERENCES findings(finding_key),
    round INTEGER NOT NULL, attempt_kind TEXT NOT NULL,
    stage_id TEXT NOT NULL REFERENCES workflow_stages(stage_id), payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(finding_key, round, attempt_kind)
);
CREATE TABLE IF NOT EXISTS re_reviews (
    review_id TEXT PRIMARY KEY, finding_key TEXT NOT NULL REFERENCES findings(finding_key),
    round INTEGER NOT NULL, stage_id TEXT NOT NULL REFERENCES workflow_stages(stage_id),
    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(finding_key, round)
);
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY, finding_key TEXT NOT NULL REFERENCES findings(finding_key),
    round INTEGER NOT NULL, reason TEXT NOT NULL,
    stage_id TEXT NOT NULL REFERENCES workflow_stages(stage_id), payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(finding_key, round, reason)
);
CREATE TABLE IF NOT EXISTS lifecycle_receipts (
    receipt_id TEXT PRIMARY KEY, stage_id TEXT NOT NULL UNIQUE REFERENCES workflow_stages(stage_id),
    orca_task_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS integrations (
    integration_id TEXT PRIMARY KEY,
    finding_key TEXT NOT NULL REFERENCES findings(finding_key),
    lane_id TEXT NOT NULL REFERENCES lanes(lane_id), round INTEGER NOT NULL,
    fixer_commit_sha TEXT NOT NULL, base_sha TEXT NOT NULL, integrated_sha TEXT,
    source_commits_json TEXT NOT NULL DEFAULT '[]',
    source_finding_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL, validation_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(finding_key, round)
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES supervisor_runs(run_id),
    lane_id TEXT, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        orca_run_id=str(row["orca_run_id"]) if row["orca_run_id"] is not None else None,
        proposal=SupervisorPlan.model_validate_json(str(row["plan_json"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _lane(row: sqlite3.Row) -> LaneRecord:
    return LaneRecord(
        lane_id=str(row["lane_id"]),
        run_id=str(row["run_id"]),
        name=str(row["name"]),
        issue_id=str(row["issue_id"]),
        repo_selector=str(row["repo_selector"]),
        base_ref=str(row["base_ref"]),
        phase=LanePhase(str(row["phase"])),
        worktree_id=str(row["worktree_id"]) if row["worktree_id"] is not None else None,
        review_head_sha=(
            str(row["review_head_sha"]) if row["review_head_sha"] is not None else None
        ),
        integration_head_sha=(
            str(row["integration_head_sha"]) if row["integration_head_sha"] is not None else None
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _stage(row: sqlite3.Row) -> StageRecord:
    attempt = row["attempt_kind"]
    return StageRecord(
        stage_id=str(row["stage_id"]),
        stage_key=str(row["stage_key"]),
        lane_id=str(row["lane_id"]),
        role=StageKind(str(row["role"])),
        finding_id=str(row["finding_id"]) if row["finding_id"] is not None else None,
        round=int(row["round"]) if row["round"] is not None else None,
        attempt_kind=AttemptKind(str(attempt)) if attempt is not None else None,
        phase=StagePhase(str(row["phase"])),
        orca_task_id=str(row["orca_task_id"]) if row["orca_task_id"] is not None else None,
        orca_dispatch_id=(
            str(row["orca_dispatch_id"]) if row["orca_dispatch_id"] is not None else None
        ),
        worktree_id=str(row["worktree_id"]) if row["worktree_id"] is not None else None,
        result_json=str(row["result_json"]) if row["result_json"] is not None else None,
        processed=bool(row["processed"]),
        released=bool(row["released"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _finding(row: sqlite3.Row) -> FindingRecord:
    return FindingRecord(
        finding_key=str(row["finding_key"]),
        lane_id=str(row["lane_id"]),
        finding_id=str(row["finding_id"]),
        origin=str(row["origin"]),
        contract=ReviewFinding.model_validate_json(str(row["contract_json"])),
        effective_contract=ReviewFinding.model_validate_json(str(row["effective_contract_json"])),
        phase=FindingPhase(str(row["phase"])),
        round=int(row["round"]),
        escalation_reason=(
            FindingReason(str(row["escalation_reason"]))
            if row["escalation_reason"] is not None
            else None
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _integration(row: sqlite3.Row) -> IntegrationRecord:
    return IntegrationRecord(
        integration_id=str(row["integration_id"]),
        finding_key=str(row["finding_key"]),
        lane_id=str(row["lane_id"]),
        round=int(row["round"]),
        fixer_commit_sha=str(row["fixer_commit_sha"]),
        source_commits=[str(item) for item in json.loads(str(row["source_commits_json"]))],
        source_finding_ids=[str(item) for item in json.loads(str(row["source_finding_ids_json"]))],
        base_sha=str(row["base_sha"]),
        integrated_sha=(str(row["integrated_sha"]) if row["integrated_sha"] is not None else None),
        status=str(row["status"]),
        validation_results=[
            ValidationResult.model_validate(item)
            for item in json.loads(str(row["validation_json"]))
        ],
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
