"""Deterministic release smoke for the non-model planning policy."""

from __future__ import annotations

from typing import TypedDict

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected

from kasgraph.models import OrcaSnapshot, OrcaWorktree, SupervisorPlan
from kasgraph.planner import PlanRejected, validate_plan


class PolicyInput(TypedDict):
    plan: dict[str, object]
    active_count: int
    max_parallel: int


def evaluate_policy(value: PolicyInput) -> bool:
    """Return whether deterministic policy accepts the proposed plan."""

    worktrees = [
        OrcaWorktree(
            worktree_id=f"repo::/tmp/active-{index}",
            repo_id="repo",
            repo="example",
            path=f"/tmp/active-{index}",
            display_name=f"active-{index}",
            workspace_status="in-progress",
            status="working",
        )
        for index in range(value["active_count"])
    ]
    try:
        validate_plan(
            SupervisorPlan.model_validate(value["plan"]),
            OrcaSnapshot(worktrees=worktrees),
            value["max_parallel"],
        )
    except (PlanRejected, ValueError):
        return False
    return True


START_PLAN: dict[str, object] = {
    "rationale": "One independent issue is ready.",
    "next_action": "start_lane",
    "selected_lane_name": "issue-123",
    "lanes": [
        {
            "name": "issue-123",
            "issue_id": "ISSUE-123",
            "repo_selector": "id:repo",
            "role": "implementer",
            "can_run_parallel": True,
            "prompt": "Implement ISSUE-123 and stop after verification.",
            "stop_condition": "Focused tests pass and a review summary exists.",
        }
    ],
}

dataset: Dataset[PolicyInput, bool, None] = Dataset(
    name="planner_policy",
    cases=[
        Case(
            name="accept_one_lane_below_limit",
            inputs={"plan": START_PLAN, "active_count": 0, "max_parallel": 2},
            expected_output=True,
        ),
        Case(
            name="reject_at_concurrency_limit",
            inputs={"plan": START_PLAN, "active_count": 2, "max_parallel": 2},
            expected_output=False,
        ),
        Case(
            name="accept_wait_at_limit",
            inputs={
                "plan": {"rationale": "Wait for active lanes.", "next_action": "wait"},
                "active_count": 2,
                "max_parallel": 2,
            },
            expected_output=True,
        ),
    ],
    evaluators=[EqualsExpected()],
)


def main() -> None:
    """Run the local planner-policy experiment."""

    report = dataset.evaluate_sync(evaluate_policy)
    report.print(include_input=True, include_output=True)


if __name__ == "__main__":
    main()
