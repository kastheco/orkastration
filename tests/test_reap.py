"""The sweep decides which panes are orkastrator's to close.

Getting that wrong in the closing direction kills an agent mid-turn, so the
predicate is pinned here rather than left to the daemon's idea of idle.
"""

from __future__ import annotations

from datetime import UTC, datetime

from orkastrator.models import LaneRecord, StageKind, StagePhase, StageRecord
from orkastrator.orca import OpenTerminals
from orkastrator.reap import build_plan, render

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _lane(lane_id: str, name: str) -> LaneRecord:
    return LaneRecord(
        lane_id=lane_id,
        run_id="run",
        name=name,
        issue_id="KAS-1",
        repo_selector="path:/tmp/repo",
        base_ref="main",
        phase="complete",
        worktree_id=None,
        review_head_sha=None,
        integration_head_sha=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _stage(
    stage_id: str,
    lane_id: str,
    *,
    dispatch_id: str | None,
    released: bool = True,
    processed: bool = True,
) -> StageRecord:
    return StageRecord(
        stage_id=stage_id,
        stage_key=stage_id,
        lane_id=lane_id,
        role=StageKind.WORKER,
        finding_id=None,
        round=None,
        attempt_kind=None,
        phase=StagePhase.COMPLETED,
        orca_task_id=None,
        orca_dispatch_id=dispatch_id,
        worktree_id=None,
        result_json=None,
        processed=processed,
        released=released,
        created_at=NOW,
        updated_at=NOW,
    )


def _terminals(*handles: str, complete: bool = True) -> OpenTerminals:
    return OpenTerminals(handles=frozenset(handles), complete=complete)


def test_a_stage_still_in_flight_is_held_however_orca_describes_its_terminal():
    """The predicate is the ledger, not the daemon: this is the whole safety rule."""

    stages = [
        _stage("settled", "lane", dispatch_id="ctx-1"),
        _stage("running", "lane", dispatch_id="ctx-2", released=False, processed=False),
        _stage("unprocessed", "lane", dispatch_id="ctx-3", processed=False),
    ]

    plan = build_plan(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=stages,
        attached={"ctx-1": "term-1", "ctx-2": "term-2", "ctx-3": "term-3"},
        terminals=_terminals("term-1", "term-2", "term-3"),
    )

    assert [target.dispatch_id for target in plan.close] == ["ctx-1"]
    assert plan.held == 2
    assert plan.close[0].terminal_handle == "term-1"
    assert plan.close[0].lane == "alpha"


def test_a_pane_orca_no_longer_lists_counts_as_reclaimed_rather_than_missing():
    plan = build_plan(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=[
            _stage("gone", "lane", dispatch_id="ctx-1"),
            _stage("unknown", "lane", dispatch_id="ctx-2"),
        ],
        attached={"ctx-1": "term-1"},
        terminals=_terminals(),
    )

    assert plan.close == ()
    # One had a handle Orca has forgotten; one Orca never had a handle for.
    assert plan.already_closed == 2


def test_a_stage_that_was_never_dispatched_is_not_counted_at_all():
    plan = build_plan(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=[_stage("never", "lane", dispatch_id=None, released=False, processed=False)],
        attached={},
        terminals=_terminals(),
    )

    assert (plan.close, plan.held, plan.already_closed) == ((), 0, 0)


def test_a_truncated_listing_is_reported_rather_than_quietly_under_reaping():
    plan = build_plan(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=[_stage("settled", "lane", dispatch_id="ctx-1")],
        attached={"ctx-1": "term-1"},
        terminals=_terminals(complete=False),
    )

    assert plan.listing_complete is False
    rendered = render(plan)
    assert "truncated" in rendered


def test_a_dry_run_says_it_closed_nothing_and_names_what_it_would():
    plan = build_plan(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=[_stage("settled", "lane", dispatch_id="ctx-1")],
        attached={"ctx-1": "term-1"},
        terminals=_terminals("term-1"),
    )

    rendered = render(plan)
    assert "to close        1" in rendered
    assert "term-1  alpha:worker  dispatch=ctx-1" in rendered
    assert "Re-run with --confirm to act." in rendered


def test_a_completed_sweep_reports_what_it_closed_not_what_it_would_have():
    plan = build_plan(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=[_stage("settled", "lane", dispatch_id="ctx-1")],
        attached={"ctx-1": "term-1"},
        terminals=_terminals("term-1"),
    )
    from dataclasses import replace

    rendered = render(replace(plan, closed=("term-1",)))

    assert "closed          1" in rendered
    assert "--confirm" not in rendered
