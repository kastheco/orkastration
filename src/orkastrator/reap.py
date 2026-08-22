"""Reclaim agent terminals a previous supervisor left behind.

Releasing a settled stage closes the pane orkastrator opened for it, but only
for stages dispatched after the handle was recorded on the row, and only when
the supervisor lived long enough to write it. A stage dispatched before that,
or by a supervisor that died between `terminal create` and `mark_stage_started`,
settles with no local record that anything was opened, and the agent tree stays
resident with nobody responsible for it.

Orca still knows. `worker-list` maps every Dispatch in a Run to the terminal it
is attached to, and orkastrator never attaches a worker to a terminal it did not
create, so a handle on one of its own dispatches is unambiguously its own pane.
The owner's sessions, the coordinator terminal and worktree setup panes are not
dispatches of this run and are never named.

The safety rule is the stage, not the terminal. A pane is only a candidate when
its stage is both released and processed: orkastrator has consumed that result,
so nothing is still reading it. Everything else is held, whatever Orca says
about it, which is why this can run against a live run without touching the
stage in flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orkastrator.models import LaneRecord, StageRecord
from orkastrator.orca import OpenTerminals

__all__ = ["ReapPlan", "ReapTarget", "build_plan", "render"]


@dataclass(frozen=True, slots=True)
class ReapTarget:
    """One settled stage's pane, named by the work it belonged to."""

    stage_id: str
    lane: str
    role: str
    dispatch_id: str
    terminal_handle: str


@dataclass(frozen=True, slots=True)
class ReapPlan:
    """What a sweep would close, what it is holding, and why."""

    run_id: str
    close: tuple[ReapTarget, ...] = ()
    # Stages orkastrator has not finished with. Reported as a count rather than
    # a list because the interesting question is whether the number is zero.
    held: int = 0
    # Settled dispatches whose pane Orca no longer lists. Already reclaimed,
    # either by `release_worker` or because the tree exited on its own.
    already_closed: int = 0
    # False when Orca truncated its terminal listing, which means a handle this
    # plan treated as closed may still be open. Under-reaps, never over-reaps.
    listing_complete: bool = True
    closed: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "close": [
                {
                    "stage_id": target.stage_id,
                    "lane": target.lane,
                    "role": target.role,
                    "dispatch_id": target.dispatch_id,
                    "terminal_handle": target.terminal_handle,
                }
                for target in self.close
            ],
            "held": self.held,
            "already_closed": self.already_closed,
            "listing_complete": self.listing_complete,
            "closed": list(self.closed),
        }


def build_plan(
    *,
    run_id: str,
    lanes: list[LaneRecord],
    stages: list[StageRecord],
    attached: dict[str, str],
    terminals: OpenTerminals,
) -> ReapPlan:
    """Decide which panes are this run's to close, from rows and Orca's own map.

    Pure, so the decision can be read and tested without a daemon. `attached`
    is Orca's dispatch-to-terminal map; `terminals` is what it still lists.
    """

    lane_names = {lane.lane_id: lane.name for lane in lanes}
    close: list[ReapTarget] = []
    held = already_closed = 0
    for stage in stages:
        if stage.orca_dispatch_id is None:
            continue
        if not (stage.released and stage.processed):
            # The stage in flight lands here, which is the whole reason this is
            # keyed on the ledger rather than on what Orca reports as idle.
            held += 1
            continue
        handle = attached.get(stage.orca_dispatch_id)
        if handle is None or handle not in terminals.handles:
            already_closed += 1
            continue
        close.append(
            ReapTarget(
                stage_id=stage.stage_id,
                lane=lane_names.get(stage.lane_id, stage.lane_id),
                role=str(stage.role.value),
                dispatch_id=stage.orca_dispatch_id,
                terminal_handle=handle,
            )
        )
    return ReapPlan(
        run_id=run_id,
        close=tuple(close),
        held=held,
        already_closed=already_closed,
        listing_complete=terminals.complete,
    )


def render(plan: ReapPlan) -> str:
    """Render the plan so an owner can read which panes it thinks are theirs."""

    acted = bool(plan.closed)
    heading = "closed" if acted else "to close"
    count = len(plan.closed) if acted else len(plan.close)
    lines = [
        f"run {plan.run_id}",
        "",
        f"  {heading:<14}  {count}",
        f"  held in flight  {plan.held}",
        f"  already closed  {plan.already_closed}",
    ]
    if not plan.listing_complete:
        lines += [
            "",
            "  Orca truncated its terminal listing, so some panes counted as already",
            "  closed may still be open. Re-run to reclaim what this pass could not see.",
        ]
    if plan.close:
        lines += ["", "panes"]
        lines += [
            f"  {target.terminal_handle}  {target.lane}:{target.role}"
            f"  dispatch={target.dispatch_id}"
            for target in plan.close
        ]
    if not acted and plan.close:
        lines += ["", "Nothing was closed. Re-run with --confirm to act."]
    return "\n".join(lines)
