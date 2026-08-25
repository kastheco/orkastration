"""SQLite dynamic-workflow ledger tests."""

import sqlite3
from pathlib import Path

import pytest

from orkastrator.models import (
    AttemptKind,
    EscalationDecision,
    FindingPhase,
    FindingReason,
    FixAttempt,
    InitialReviewReport,
    LanePhase,
    PublicationReceipt,
    StageKind,
    StagePhase,
    SupervisorPlan,
)
from orkastrator.store import IntegrationBusyError, StateStore, UnsupportedStateError
from tests.factories import initial_review_report_json, review_finding_data


def sample_proposal() -> SupervisorPlan:
    return SupervisorPlan.model_validate(
        {
            "objective": "Do work",
            "rationale": "Ready.",
            "next_action": "propose_lanes",
            "owner_question": None,
            "lanes": [
                {
                    "name": "issue-123",
                    "issue_id": "ISSUE-123",
                    "repo_selector": "id:repo",
                    "base_ref": "main",
                    "dependencies": [],
                    "prompt": "Implement ISSUE-123.",
                    "stop_condition": "Tests pass.",
                }
            ],
        }
    )


def two_lane_proposal() -> SupervisorPlan:
    raw = sample_proposal().model_dump(mode="json")
    lanes = raw["lanes"]
    assert isinstance(lanes, list)
    first = lanes[0]
    assert isinstance(first, dict)
    lanes.append({**first, "name": "issue-124", "issue_id": "ISSUE-124"})
    return SupervisorPlan.model_validate(raw)


def test_store_records_dynamic_worker_and_idempotent_stage_updates(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    worker = store.stages(run_id)[0]

    assert store.run(run_id).status == "proposed"
    assert worker.role is StageKind.WORKER
    assert store.counts() == {"runs": 1, "lanes": 1, "stages": 1, "findings": 0}

    store.mark_accepted(run_id, "orca-run-1")
    store.bind_stage_task(run_id, worker.stage_id, "task-1")
    store.bind_stage_task(run_id, worker.stage_id, "task-1")
    store.record_worker_checkout(
        run_id,
        worker.stage_id,
        "repo::/tmp/issue-123",
        "a" * 40,
    )
    store.mark_stage_started(
        run_id,
        worker.stage_id,
        "dispatch-1",
        "repo::/tmp/issue-123",
        {"ok": True},
    )
    store.sync_stage(run_id, worker.stage_id, StagePhase.COMPLETED, '{"ok":true}')
    store.mark_released(run_id, worker.stage_id)
    store.mark_released(run_id, worker.stage_id)

    updated_lane = store.lanes(run_id)[0]
    updated_worker = store.stages(run_id)[0]
    assert updated_lane.phase is LanePhase.ACTIVE
    assert updated_lane.worktree_id == "repo::/tmp/issue-123"
    assert updated_lane.base_ref == "main"
    assert updated_lane.base_sha == "a" * 40
    assert updated_worker.phase is StagePhase.COMPLETED
    assert updated_worker.released is True


def test_store_freezes_initial_findings_and_rejects_changed_replay(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    report = InitialReviewReport.model_validate_json(
        initial_review_report_json(review_finding_data())
    )

    store.record_initial_review(run_id, lane.lane_id, report)
    store.record_initial_review(run_id, lane.lane_id, report)
    persisted = store.findings(run_id)
    assert [item.finding_id for item in persisted] == ["finding-1"]
    assert persisted[0].contract == report.findings[0]

    changed = report.model_copy(update={"summary": "Changed after freeze."})
    with pytest.raises(ValueError, match="already frozen"):
        store.record_initial_review(run_id, lane.lane_id, changed)


def test_stage_start_reservation_atomically_enforces_global_capacity(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "capacity.sqlite3")
    store.setup()
    run_id = store.record_proposal(two_lane_proposal())
    stages = store.stages(run_id)
    for number, stage in enumerate(stages, start=1):
        store.bind_stage_task(run_id, stage.stage_id, f"task-{number}")

    assert store.reserve_stage_start(
        run_id,
        store.stages(run_id)[0],
        max_workers=1,
        max_lanes=2,
        max_lane_fixers=1,
    )
    assert not store.reserve_stage_start(
        run_id,
        store.stages(run_id)[1],
        max_workers=1,
        max_lanes=2,
        max_lane_fixers=1,
    )
    assert store.active_worker_count() == 1

    store.reset_stage_reservation(run_id, store.stages(run_id)[0])
    assert store.active_worker_count() == 0


def test_lane_integration_reservation_is_serial(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "integration.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    report = InitialReviewReport.model_validate_json(
        initial_review_report_json(review_finding_data(1), review_finding_data(2))
    )
    store.record_initial_review(run_id, lane.lane_id, report)
    first, second = store.findings(run_id)

    first_receipt = store.begin_integration(
        run_id,
        first,
        fixer_commit_sha="d" * 40,
        source_commits=["d" * 40],
        source_finding_ids=["finding-1"],
        base_sha="b" * 40,
    )
    replayed = store.begin_integration(
        run_id,
        first,
        fixer_commit_sha="d" * 40,
        source_commits=["d" * 40],
        source_finding_ids=["finding-1"],
        base_sha="b" * 40,
    )
    assert replayed.integration_id == first_receipt.integration_id
    with pytest.raises(IntegrationBusyError, match="integrating another finding"):
        store.begin_integration(
            run_id,
            second,
            fixer_commit_sha="e" * 40,
            source_commits=["e" * 40],
            source_finding_ids=["finding-2"],
            base_sha="b" * 40,
        )

    store.finish_integration(
        run_id,
        first,
        status="integrated",
        integrated_sha="f" * 40,
        validation_results=[],
    )
    receipt = store.begin_integration(
        run_id,
        second,
        fixer_commit_sha="e" * 40,
        source_commits=["e" * 40],
        source_finding_ids=["finding-2"],
        base_sha="f" * 40,
    )
    assert receipt.status == "starting"


def test_store_rejects_duplicate_accept_unknown_rows_and_changed_bindings(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    worker = store.stages(run_id)[0]
    store.mark_accepted(run_id, "orca-run-1")
    store.bind_stage_task(run_id, worker.stage_id, "task-1")

    with pytest.raises(ValueError, match="awaiting acceptance"):
        store.mark_accepted(run_id, "orca-run-2")
    with pytest.raises(ValueError, match="already bound"):
        store.bind_stage_task(run_id, worker.stage_id, "task-2")
    with pytest.raises(KeyError, match="unknown run"):
        store.run("missing")
    with pytest.raises(KeyError, match="unknown stage"):
        store.mark_released(run_id, "missing")


@pytest.mark.parametrize("status", ["complete", "failed", "blocked"])
def test_store_sets_terminal_status_without_rewriting_lane_evidence(
    tmp_path: Path, status: str
) -> None:
    store = StateStore(tmp_path / f"{status}.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    store.set_terminal_status(run_id, status)
    store.set_terminal_status(run_id, status)
    assert store.run(run_id).status == status
    assert store.lanes(run_id)[0].phase is LanePhase.PROPOSED


def test_setup_rejects_accepted_v1_fixed_stage_state(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    store = StateStore(path)
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO lane_stages
                (stage_id, lane_id, role, phase, created_at, updated_at)
            VALUES ('legacy-stage', ?, 'worker', 'dispatched', 'now', 'now')
            """,
            (lane.lane_id,),
        )
        connection.execute(
            """
            UPDATE supervisor_runs SET orca_run_id = 'orca-old', status = 'active'
            WHERE run_id = ?
            """,
            (run_id,),
        )
        connection.execute("DELETE FROM workflow_stages")

    with pytest.raises(UnsupportedStateError, match="v1 fixed-stage"):
        store.setup()


def test_setup_rejects_proposed_v1_fixed_stage_state(tmp_path: Path) -> None:
    path = tmp_path / "legacy-proposed.sqlite3"
    store = StateStore(path)
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO lane_stages
                (stage_id, lane_id, role, phase, created_at, updated_at)
            VALUES ('legacy-stage', ?, 'worker', 'pending', 'now', 'now')
            """,
            (lane.lane_id,),
        )
        connection.execute("DELETE FROM workflow_stages")

    with pytest.raises(UnsupportedStateError, match="v1 fixed-stage"):
        store.setup()


def test_reopening_a_finding_retires_its_settled_stage_and_stale_verdict(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "reopen.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]
    stage = store.ensure_stage(
        run_id,
        lane.lane_id,
        stage_key="escalate:1",
        role=StageKind.ESCALATION,
        finding_key=finding.finding_key,
        finding_id=finding.finding_id,
        round=1,
    )
    store.sync_stage(run_id, stage.stage_id, StagePhase.COMPLETED, None)
    store.record_escalation(
        run_id,
        finding,
        stage,
        EscalationDecision(
            finding_id=finding.finding_id,
            round=1,
            reason="validation_failed",
            action="block",
            rationale="The word this adjudicator wanted did not exist yet.",
        ),
    )
    store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)

    reopened = store.reopen_finding(
        run_id,
        finding.finding_id,
        phase=FindingPhase.PENDING_ESCALATION,
        escalation_reason=FindingReason.VALIDATION_FAILED,
        note="the vocabulary gained accept_fix",
    )

    assert reopened.phase is FindingPhase.PENDING_ESCALATION
    assert reopened.escalation_reason is FindingReason.VALIDATION_FAILED
    assert store.run(run_id).status == "active"
    assert store.lanes(run_id)[0].phase is LanePhase.ACTIVE
    # The settled stage must stop answering to its key or the graph, which finds
    # stages by key, never dispatches the replacement adjudication.
    assert [
        item.stage_key for item in store.stages(run_id) if item.role is StageKind.ESCALATION
    ] != ["escalate:1"]
    # And the stale verdict must be gone, or the fresh one is rejected on the
    # frozen-contract guard rather than recorded.
    assert (
        store.ensure_stage(
            run_id,
            lane.lane_id,
            stage_key="escalate:1",
            role=StageKind.ESCALATION,
            finding_key=finding.finding_key,
            finding_id=finding.finding_id,
            round=1,
        ).phase
        is StagePhase.PENDING
    )


def test_reopening_to_an_escalation_keeps_the_committed_fix_as_evidence(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "evidence.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]
    stage = store.ensure_stage(
        run_id,
        lane.lane_id,
        stage_key="fix:1",
        role=StageKind.FIXER,
        finding_key=finding.finding_key,
        finding_id=finding.finding_id,
        round=1,
        attempt_kind=AttemptKind.PRIMARY,
    )
    store.record_fix_attempt(
        run_id,
        finding,
        stage,
        FixAttempt(
            finding_id=finding.finding_id,
            round=1,
            status="fixed",
            base_sha="b" * 40,
            commit_sha="c" * 40,
            changed_paths=["src/app.py"],
            validation_results=[],
            scope_expansion_required=None,
        ),
    )

    store.reopen_finding(
        run_id,
        finding.finding_id,
        phase=FindingPhase.PENDING_ESCALATION,
        escalation_reason=FindingReason.VALIDATION_FAILED,
        note="only the evidence was disputed",
    )

    kept = store.latest_fix_attempt(finding.finding_key)
    assert kept is not None and kept.commit_sha == "c" * 40

    # Reopening to the fix itself does discard it: that round is being redone.
    store.reopen_finding(
        run_id,
        finding.finding_id,
        phase=FindingPhase.PENDING_FIX,
        note="the fix itself was wrong",
    )
    assert store.latest_fix_attempt(finding.finding_key) is None


def test_a_finding_cannot_be_reopened_into_a_phase_no_stage_advances(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "phase.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]

    with pytest.raises(ValueError, match="cannot reopen"):
        store.reopen_finding(
            run_id, finding.finding_id, phase=FindingPhase.RESOLVED, note="wishful"
        )
    with pytest.raises(KeyError):
        store.reopen_finding(run_id, "finding-absent", phase=FindingPhase.PENDING_FIX, note="typo")


def test_settling_a_finding_records_the_decision_and_retires_its_live_stage(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "settle.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]
    stage = store.ensure_stage(
        run_id,
        lane.lane_id,
        stage_key="escalate:1",
        role=StageKind.ESCALATION,
        finding_key=finding.finding_key,
        finding_id=finding.finding_id,
        round=1,
    )
    store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)

    settled = store.settle_finding(
        run_id, finding.finding_id, phase=FindingPhase.DEFERRED, note="tracked elsewhere"
    )

    assert settled.phase is FindingPhase.DEFERRED
    assert settled.escalation_reason is None
    # An escalation still in flight would come back and re-adjudicate a finding
    # the owner has already closed, so it has to be retired with the decision.
    retired = next(item for item in store.stages(run_id) if item.stage_id == stage.stage_id)
    assert retired.processed
    assert retired.stage_key != "escalate:1"
    # The dispatch loop starts from READY and reads neither `processed` nor the
    # key, so leaving the phase alone only postponed the re-adjudication until a
    # slot opened up.
    assert retired.phase is StagePhase.BLOCKED
    assert not store.reserve_stage_start(
        run_id, retired, max_workers=4, max_lanes=4, max_lane_fixers=2
    )
    action = [
        item for item in store.events(run_id) if item["kind"] == "supervisor_hand_action"
    ][-1]
    assert action["payload"] == {
        "command": "settle",
        "target": finding.finding_id,
        "phase": "deferred",
        "outcome": "deferred",
        "note": "tracked elsewhere",
    }


def test_an_owner_settle_survives_reconciliation_until_reopened(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "sticky-settle.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]
    store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)
    store.settle_finding(
        run_id,
        finding.finding_id,
        phase=FindingPhase.DEFERRED,
        note="the conflict was resolved outside the lane",
    )

    # This is the write a later monitor reconciliation attempted in KAS-671.
    store.set_finding_state(
        run_id,
        finding.finding_key,
        phase=FindingPhase.ESCALATING,
        escalation_reason=FindingReason.INTEGRATION_CONFLICT,
    )

    held = store.findings(run_id)[0]
    assert held.phase is FindingPhase.DEFERRED
    assert held.escalation_reason is None

    store.reopen_finding(
        run_id,
        finding.finding_id,
        phase=FindingPhase.PENDING_ESCALATION,
        escalation_reason=FindingReason.INTEGRATION_CONFLICT,
        force=True,
        note="the external resolution regressed",
    )
    store.set_finding_state(
        run_id,
        finding.finding_key,
        phase=FindingPhase.ESCALATING,
        escalation_reason=FindingReason.INTEGRATION_CONFLICT,
    )
    reopened = store.findings(run_id)[0]
    assert reopened.phase is FindingPhase.ESCALATING
    assert reopened.escalation_reason is FindingReason.INTEGRATION_CONFLICT


def test_a_finding_cannot_be_settled_into_a_phase_that_is_not_a_decision(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "decision.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]

    with pytest.raises(ValueError, match="cannot settle"):
        store.settle_finding(
            run_id, finding.finding_id, phase=FindingPhase.PENDING_FIX, note="not a decision"
        )
    with pytest.raises(KeyError):
        store.settle_finding(run_id, "finding-absent", phase=FindingPhase.DEFERRED, note="typo")


def test_a_finding_cannot_be_settled_resolved_until_its_fix_is_integrated(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "resolved.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(
            initial_review_report_json(review_finding_data(1), review_finding_data(2))
        ),
    )
    first, second = store.findings(run_id)
    store.set_finding_state(run_id, first.finding_key, phase=FindingPhase.BLOCKED)
    store.set_finding_state(run_id, second.finding_key, phase=FindingPhase.BLOCKED)

    # Resolved means the fix is in the lane checkout. Saying so before it is there
    # publishes the pre-fix head under a receipt that claims otherwise.
    with pytest.raises(ValueError, match="no integrated fix"):
        store.settle_finding(
            run_id, first.finding_id, phase=FindingPhase.RESOLVED, note="looks right to me"
        )
    with pytest.raises(KeyError):
        store.settle_finding(run_id, "finding-absent", phase=FindingPhase.RESOLVED, note="typo")

    # Deferred carries no such claim, so it stays available with nothing integrated.
    assert (
        store.settle_finding(
            run_id, first.finding_id, phase=FindingPhase.DEFERRED, note="tracked elsewhere"
        ).phase
        is FindingPhase.DEFERRED
    )

    store.begin_integration(
        run_id,
        second,
        fixer_commit_sha="d" * 40,
        source_commits=["d" * 40],
        source_finding_ids=[first.finding_id],
        base_sha="b" * 40,
    )
    # A receipt only counts once it has actually moved the lane checkout.
    assert store.integrated_finding_ids(run_id, lane.lane_id) == set()
    store.finish_integration(
        run_id, second, status="integrated", integrated_sha="f" * 40, validation_results=[]
    )

    # One commit can answer several findings, so the receipt's own finding and
    # everything it names as a source are both settleable.
    assert store.integrated_finding_ids(run_id, lane.lane_id) == {
        first.finding_id,
        second.finding_id,
    }
    settled = store.settle_finding(
        run_id, second.finding_id, phase=FindingPhase.RESOLVED, note="integrated"
    )
    assert settled.phase is FindingPhase.RESOLVED


def test_a_round_level_contract_constraint_is_rebuilt_around_the_stage(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    store = StateStore(path)
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]
    stage = store.ensure_stage(
        run_id,
        lane.lane_id,
        stage_key="escalate:2",
        role=StageKind.ESCALATION,
        finding_key=finding.finding_key,
        finding_id=finding.finding_id,
        round=2,
    )
    decision = EscalationDecision(
        finding_id=finding.finding_id,
        round=2,
        reason="validation_failed",
        action="approve_unchanged",
        rationale="Verified at the committed head.",
        revised_finding=None,
    )
    store.record_escalation(run_id, finding, stage, decision)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            DROP INDEX ix_escalations_finding_key;
            ALTER TABLE escalations RENAME TO escalations_legacy;
            CREATE TABLE escalations (
                escalation_id VARCHAR NOT NULL PRIMARY KEY,
                finding_key VARCHAR NOT NULL REFERENCES findings (finding_key),
                round INTEGER NOT NULL,
                reason VARCHAR NOT NULL,
                stage_id VARCHAR NOT NULL REFERENCES workflow_stages (stage_id),
                payload_json VARCHAR NOT NULL,
                created_at DATETIME NOT NULL,
                UNIQUE (finding_key, round, reason)
            );
            CREATE INDEX ix_escalations_finding_key ON escalations (finding_key);
            INSERT INTO escalations SELECT * FROM escalations_legacy;
            DROP TABLE escalations_legacy;
            """
        )

    reopened = StateStore(path)
    reopened.setup()
    second = reopened.ensure_stage(
        run_id,
        lane.lane_id,
        stage_key="escalate:2:retry1",
        role=StageKind.ESCALATION,
        finding_key=finding.finding_key,
        finding_id=finding.finding_id,
        round=2,
    )
    reopened.record_escalation(
        run_id,
        finding,
        second,
        decision.model_copy(update={"rationale": "Looked again after the conflict."}),
    )

    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT stage_id FROM escalations ORDER BY created_at").fetchall()
    # The first verdict survived the rebuild, and the second round-mate records.
    assert [item[0] for item in rows] == [stage.stage_id, second.stage_id]


def test_a_rebuild_interrupted_before_the_copy_is_finished_on_the_next_setup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interrupted.sqlite3"
    store = StateStore(path)
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data())),
    )
    finding = store.findings(run_id)[0]
    stage = store.ensure_stage(
        run_id,
        lane.lane_id,
        stage_key="fix:1",
        role=StageKind.FIXER,
        finding_key=finding.finding_key,
        finding_id=finding.finding_id,
        round=1,
        attempt_kind=AttemptKind.PRIMARY,
    )
    store.record_fix_attempt(
        run_id,
        finding,
        stage,
        FixAttempt(
            finding_id=finding.finding_id,
            round=1,
            status="fixed",
            base_sha="b" * 40,
            commit_sha="c" * 40,
            changed_paths=["src/app.py"],
            validation_results=[],
            scope_expansion_required=None,
        ),
    )
    # A rename that landed while the copy did not leaves the rows behind a table
    # whose shape is already current, so the shape alone cannot detect it.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            DROP INDEX ix_fix_attempts_finding_key;
            ALTER TABLE fix_attempts RENAME TO fix_attempts_superseded;
            """
        )

    StateStore(path).setup()

    assert store.latest_fix_attempt(finding.finding_key) is not None
    with sqlite3.connect(path) as connection:
        left = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'fix_attempts_superseded'"
        ).fetchall()
    assert left == []


def test_a_publication_error_streak_counts_up_and_a_pass_resets_it(tmp_path: Path) -> None:
    """The streak is the whole mechanism, so it is worth pinning on its own.

    "GitHub has not answered yet" and "GitHub says this failed" arrive as the
    same exception, and telling them apart by reading the message is guesswork
    that goes stale. Consecutive failures need no taxonomy: the transient case
    clears itself on the next pass, and the real one does not.
    """

    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(two_lane_proposal())
    lane, other = store.lanes(run_id)

    assert store.note_publication_error(run_id, lane.lane_id, "required check not found") == 1
    assert store.note_publication_error(run_id, lane.lane_id, "required check not found") == 2

    # A pass that got an answer ends the streak, whatever the answer was.
    store.note_publication_progress(run_id, lane.lane_id)
    assert store.note_publication_error(run_id, lane.lane_id, "still nothing") == 1

    # And one lane's streak is not another's.
    assert store.note_publication_error(run_id, other.lane_id, "unrelated") == 1


def test_a_publication_pass_that_never_failed_records_nothing(tmp_path: Path) -> None:
    """The healthy path is every tick of every lane, so it must not write."""

    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]

    store.note_publication_progress(run_id, lane.lane_id)
    store.note_publication_progress(run_id, lane.lane_id)

    kinds = [event["kind"] for event in store.events(run_id)]
    assert "lane_publication_progress" not in kinds


def test_advancing_a_fix_base_leaves_the_review_revision_alone(tmp_path: Path) -> None:
    """The build base and the evidence anchor are different facts.

    A conflict retry has to be rebuilt on the lane head, but the review revision
    is what `_verified_review` checks the frozen diff against. Rewriting it to
    say where the retry starts would make the finding's own digest describe a
    range nobody reviewed.
    """

    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data(1))),
    )
    finding = store.findings(run_id)[0]
    assert finding.dispatch_base_sha is None
    revision = finding.effective_contract.review_revision
    assert revision is not None

    store.advance_fix_base(run_id, finding.finding_key, "e" * 40)

    moved = store.findings(run_id)[0]
    assert moved.dispatch_base_sha == "e" * 40
    assert moved.effective_contract.review_revision == revision
    advanced = [item for item in store.events(run_id) if item["kind"] == "fix_base_advanced"]
    assert advanced[-1]["payload"] == {
        "finding_id": finding.finding_id,
        "from": None,
        "to": "e" * 40,
    }

    # Advancing to where it already is writes nothing, so a replayed tick cannot
    # fill the log with a move that did not happen.
    store.advance_fix_base(run_id, finding.finding_key, "e" * 40)
    assert len([item for item in store.events(run_id) if item["kind"] == "fix_base_advanced"]) == 1


def test_reopening_a_finding_that_settled_on_the_merits_needs_force(tmp_path: Path) -> None:
    """Reopen takes a hand-typed id, and one transposed character must not undo
    a finding an agent got right."""

    store = StateStore(tmp_path / "reopen-settled.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data(1))),
    )
    finding = store.findings(run_id)[0]
    store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.RESOLVED)

    with pytest.raises(ValueError, match="pass force"):
        store.reopen_finding(
            run_id, finding.finding_id, phase=FindingPhase.PENDING_FIX, note="typo"
        )
    assert store.findings(run_id)[0].phase is FindingPhase.RESOLVED

    reopened = store.reopen_finding(
        run_id,
        finding.finding_id,
        phase=FindingPhase.PENDING_FIX,
        force=True,
        note="the fix regressed something the review missed",
    )

    assert reopened.phase is FindingPhase.PENDING_FIX
    event = [item for item in store.events(run_id) if item["kind"] == "finding_reopened"][-1]
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["from_phase"] == "resolved"
    assert payload["forced"] is True
    action = [
        item for item in store.events(run_id) if item["kind"] == "supervisor_hand_action"
    ][-1]
    assert action["payload"] == {
        "command": "reopen",
        "target": finding.finding_id,
        "phase": "pending_fix",
        "outcome": "pending_fix",
        "note": "the fix regressed something the review missed",
    }


def test_reopening_a_blocked_finding_still_needs_no_force(tmp_path: Path) -> None:
    """A blocked finding is exactly the case reopen exists for."""

    store = StateStore(tmp_path / "reopen-blocked.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    store.record_initial_review(
        run_id,
        lane.lane_id,
        InitialReviewReport.model_validate_json(initial_review_report_json(review_finding_data(1))),
    )
    finding = store.findings(run_id)[0]
    store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)

    reopened = store.reopen_finding(
        run_id, finding.finding_id, phase=FindingPhase.PENDING_FIX, note="the contract bug is fixed"
    )

    assert reopened.phase is FindingPhase.PENDING_FIX


def test_a_published_head_may_land_but_never_un_land(tmp_path: Path) -> None:
    """Draft and landed are the two facts about a published head that move.

    Both move once and in one direction. Everything else on the receipt
    identifies the publication, so letting it change would quietly re-point an
    audit record at a different branch or pull request.
    """

    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]
    receipt = PublicationReceipt(
        run_id=run_id,
        lane=lane.name,
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch=f"orkastrator/{run_id[:12]}/{lane.name}",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="b" * 40,
        draft=True,
    )

    store.record_publication(run_id, lane.lane_id, receipt)
    landed = receipt.model_copy(
        update={
            "draft": False,
            "landed": True,
            "merged_head_sha": "c" * 40,
            "merge_sha": "d" * 40,
        }
    )
    store.record_publication(run_id, lane.lane_id, landed)

    assert store.publications(run_id) == [landed]
    kinds = [item["kind"] for item in store.events(run_id)]
    assert kinds.count("lane_published") == 1
    assert kinds.count("pull_request_landed") == 1

    with pytest.raises(ValueError, match="cannot be un-landed"):
        store.record_publication(run_id, lane.lane_id, receipt)
    with pytest.raises(ValueError, match="merge sha cannot change"):
        store.record_publication(
            run_id,
            lane.lane_id,
            landed.model_copy(update={"merge_sha": "e" * 40}),
        )
    with pytest.raises(ValueError, match="merged head cannot change"):
        store.record_publication(
            run_id,
            lane.lane_id,
            landed.model_copy(update={"merged_head_sha": "e" * 40}),
        )
    with pytest.raises(ValueError, match="publication identity changed"):
        store.record_publication(
            run_id,
            lane.lane_id,
            landed.model_copy(update={"pull_request_url": "https://github.com/owner/repo/pull/9"}),
        )
