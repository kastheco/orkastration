"""SQLite dynamic-workflow ledger tests."""

import sqlite3
from pathlib import Path

import pytest

from orkastrator.models import (
    InitialReviewReport,
    LanePhase,
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
