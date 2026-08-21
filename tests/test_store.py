"""SQLite state-ledger tests."""

from pathlib import Path

from kasgraph.models import LanePhase, SupervisorPlan
from kasgraph.store import StateStore


def sample_plan() -> SupervisorPlan:
    return SupervisorPlan.model_validate(
        {
            "rationale": "Ready.",
            "next_action": "start_lane",
            "selected_lane_name": "issue-123",
            "lanes": [
                {
                    "name": "issue-123",
                    "issue_id": "ISSUE-123",
                    "repo_selector": "id:repo",
                    "role": "implementer",
                    "prompt": "Implement ISSUE-123.",
                    "stop_condition": "Tests pass.",
                }
            ],
        }
    )


def test_store_records_and_updates_lane(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_plan("Do work", sample_plan())
    lane = store.lane_for_name(run_id, "issue-123")

    store.mark_lane_started(lane.lane_id, "repo::/tmp/issue-123", {"ok": True})
    started = store.lane_for_name(run_id, "issue-123")
    store.update_lane_phase(started.lane_id, LanePhase.REVIEW, {"status": "in-review"})

    final = store.lane_for_name(run_id, "issue-123")
    assert final.worktree_id == "repo::/tmp/issue-123"
    assert final.phase is LanePhase.REVIEW
    assert store.counts() == {"runs": 1, "lanes": 1}
