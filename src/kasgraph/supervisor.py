"""One-action supervisor cycle and read-only reconciliation."""

from __future__ import annotations

from typing import Protocol

from kasgraph.models import (
    CycleResult,
    LanePhase,
    LaneProposal,
    LaneRecord,
    OrcaSnapshot,
    OrcaWorktree,
    SupervisorPlan,
)
from kasgraph.planner import Planner
from kasgraph.store import StateStore


class OrcaController(Protocol):
    """Narrow Orca operations used by a supervisor cycle."""

    async def snapshot(self) -> OrcaSnapshot:
        """Read current worktree state."""

    async def create_lane(self, lane: LaneProposal) -> tuple[str, dict[str, object]]:
        """Create or reuse one lane."""


class Supervisor:
    """Coordinate one bounded plan/validate/record/execute cycle."""

    def __init__(self, *, planner: Planner, orca: OrcaController, store: StateStore):
        self._planner = planner
        self._orca = orca
        self._store = store

    async def plan(self, objective: str) -> tuple[OrcaSnapshot, SupervisorPlan]:
        """Return a current snapshot and typed plan without persistence or mutation."""

        snapshot = await self._orca.snapshot()
        plan = await self._planner.plan(objective, snapshot)
        return snapshot, plan

    async def run_cycle(self, objective: str, *, execute: bool) -> CycleResult:
        """Persist one plan and optionally execute its single selected lane start."""

        snapshot = await self._orca.snapshot()
        plan = await self._planner.plan(objective, snapshot)
        run_id = self._store.record_plan(objective, plan)
        lane = plan.selected_lane()
        if lane is None:
            return CycleResult(
                run_id=run_id,
                executed=False,
                action=plan.next_action,
                plan=plan,
            )
        if not execute:
            return CycleResult(
                run_id=run_id,
                executed=False,
                action="would_start_lane",
                plan=plan,
            )
        record = self._store.lane_for_name(run_id, lane.name)
        worktree_id, payload = await self._orca.create_lane(lane)
        self._store.mark_lane_started(record.lane_id, worktree_id, payload)
        return CycleResult(
            run_id=run_id,
            executed=True,
            action="started_lane",
            plan=plan,
            worktree_id=worktree_id,
        )

    async def reconcile(self) -> list[LaneRecord]:
        """Update local observations from authoritative Orca worktree state."""

        snapshot = await self._orca.snapshot()
        by_id = {worktree.worktree_id: worktree for worktree in snapshot.worktrees}
        for lane in self._store.lanes():
            if lane.worktree_id is None:
                continue
            worktree = by_id.get(lane.worktree_id)
            phase = _phase(worktree)
            self._store.update_lane_phase(
                lane.lane_id,
                phase,
                {
                    "worktree_id": lane.worktree_id,
                    "orca_status": worktree.status if worktree else "missing",
                    "workspace_status": worktree.workspace_status if worktree else "missing",
                },
            )
        return self._store.lanes()


def _phase(worktree: OrcaWorktree | None) -> LanePhase:
    if worktree is None:
        return LanePhase.BLOCKED
    if worktree.workspace_status == "completed":
        return LanePhase.COMPLETE
    if worktree.workspace_status == "in-review":
        return LanePhase.REVIEW
    if worktree.status in {"active", "working"}:
        return LanePhase.ACTIVE
    return LanePhase.BLOCKED
