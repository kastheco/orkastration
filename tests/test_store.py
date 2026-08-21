"""SQLite graph-ledger tests."""

from pathlib import Path

import pytest

from kasgraph.models import LanePhase, RoleName, StagePhase, SupervisorPlan
from kasgraph.store import StateStore


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
                    "dependencies": [],
                    "prompt": "Implement ISSUE-123.",
                    "stop_condition": "Tests pass.",
                }
            ],
        }
    )


def test_store_records_accepts_and_updates_graph(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    lane = store.lanes(run_id)[0]

    assert store.run(run_id).status == "proposed"
    assert [stage.role for stage in store.stages(run_id)] == list(RoleName)
    assert store.counts() == {"runs": 1, "lanes": 1, "stages": 4}

    store.mark_accepted(run_id, "orca-run-1")
    store.bind_stage_task(run_id, lane.lane_id, RoleName.WORKER, "task-1")
    store.mark_stage_started(
        run_id,
        lane.lane_id,
        RoleName.WORKER,
        "dispatch-1",
        "repo::/tmp/issue-123",
        {"ok": True},
    )
    store.sync_stage(run_id, lane.lane_id, RoleName.WORKER, StagePhase.COMPLETED)
    store.mark_released(run_id, lane.lane_id, RoleName.WORKER)

    updated_lane = store.lanes(run_id)[0]
    worker = store.stages(run_id)[0]
    assert updated_lane.phase is LanePhase.ACTIVE
    assert updated_lane.worktree_id == "repo::/tmp/issue-123"
    assert worker.phase is StagePhase.COMPLETED
    assert worker.released is True


def test_store_rejects_duplicate_accept_and_unknown_run(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    store.mark_accepted(run_id, "orca-run-1")

    with pytest.raises(ValueError, match="awaiting acceptance"):
        store.mark_accepted(run_id, "orca-run-2")
    with pytest.raises(KeyError, match="unknown run"):
        store.run("missing")


@pytest.mark.parametrize("status", ["complete", "failed", "blocked"])
def test_store_sets_terminal_status(tmp_path: Path, status: str) -> None:
    store = StateStore(tmp_path / f"{status}.sqlite3")
    store.setup()
    run_id = store.record_proposal(sample_proposal())
    store.set_terminal_status(run_id, status)
    assert store.run(run_id).status == status
    assert store.lanes(run_id)[0].phase.value == status
