"""Deterministic planning-policy tests."""

import pytest

from kasgraph.models import OrcaSnapshot, OrcaWorktree, SupervisorPlan
from kasgraph.planner import PlanRejected, validate_plan


def plan(*, parallel: bool = True) -> SupervisorPlan:
    return SupervisorPlan.model_validate(
        {
            "rationale": "One lane is ready.",
            "next_action": "start_lane",
            "selected_lane_name": "issue-123",
            "lanes": [
                {
                    "name": "issue-123",
                    "issue_id": "ISSUE-123",
                    "repo_selector": "id:repo",
                    "role": "implementer",
                    "can_run_parallel": parallel,
                    "prompt": "Implement ISSUE-123.",
                    "stop_condition": "Tests pass.",
                }
            ],
        }
    )


def active_snapshot(name: str = "other") -> OrcaSnapshot:
    return OrcaSnapshot(
        worktrees=[
            OrcaWorktree(
                worktree_id="repo::/tmp/other",
                repo_id="repo",
                repo="example",
                path="/tmp/other",
                display_name=name,
                workspace_status="in-progress",
                status="working",
            )
        ]
    )


def test_parallel_lane_is_allowed_below_limit() -> None:
    validate_plan(plan(), active_snapshot(), 2)


def test_serial_lane_is_rejected_while_work_is_active() -> None:
    with pytest.raises(PlanRejected, match="serial"):
        validate_plan(plan(parallel=False), active_snapshot(), 2)


def test_duplicate_orca_name_is_rejected() -> None:
    with pytest.raises(PlanRejected, match="already uses"):
        validate_plan(plan(), active_snapshot("issue-123"), 2)
