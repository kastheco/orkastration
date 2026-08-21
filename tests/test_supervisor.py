"""One-action supervisor and reconciliation tests."""

from pathlib import Path

from kasgraph.models import LanePhase, LaneProposal, OrcaSnapshot, OrcaWorktree, SupervisorPlan
from kasgraph.store import StateStore
from kasgraph.supervisor import Supervisor


class FakePlanner:
    def __init__(self, proposal: SupervisorPlan):
        self.proposal = proposal

    async def plan(self, objective: str, snapshot: OrcaSnapshot) -> SupervisorPlan:
        assert objective
        assert snapshot.worktrees == []
        return self.proposal


class FakeOrca:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.worktrees: list[OrcaWorktree] = []

    async def snapshot(self) -> OrcaSnapshot:
        return OrcaSnapshot(worktrees=self.worktrees)

    async def create_lane(self, lane: LaneProposal) -> tuple[str, dict[str, object]]:
        self.created.append(lane.name)
        return "repo::/tmp/issue-123", {"ok": True}


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


def supervisor(tmp_path: Path, orca: FakeOrca) -> Supervisor:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    return Supervisor(planner=FakePlanner(sample_plan()), orca=orca, store=store)


async def test_dry_run_records_but_does_not_create(tmp_path: Path) -> None:
    orca = FakeOrca()
    result = await supervisor(tmp_path, orca).run_cycle("Do work", execute=False)

    assert result.action == "would_start_lane"
    assert result.executed is False
    assert orca.created == []


async def test_execute_creates_exactly_one_lane(tmp_path: Path) -> None:
    orca = FakeOrca()
    result = await supervisor(tmp_path, orca).run_cycle("Do work", execute=True)

    assert result.executed is True
    assert result.worktree_id == "repo::/tmp/issue-123"
    assert orca.created == ["issue-123"]


async def test_reconcile_marks_missing_worktree_blocked(tmp_path: Path) -> None:
    orca = FakeOrca()
    value = supervisor(tmp_path, orca)
    await value.run_cycle("Do work", execute=True)

    lanes = await value.reconcile()

    assert lanes[0].phase is LanePhase.BLOCKED
