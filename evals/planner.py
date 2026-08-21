"""Deterministic contract eval for subscription-backed planner output."""

from __future__ import annotations

from typing import TypedDict

from pydantic import ValidationError
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected

from kasgraph.models import SupervisorPlan


class ContractInput(TypedDict):
    plan: dict[str, object]


def validates_supervisor_plan(value: ContractInput) -> bool:
    """Return whether a candidate satisfies the exact planner contract."""

    try:
        SupervisorPlan.model_validate(value["plan"])
    except ValidationError:
        return False
    return True


LANE: dict[str, object] = {
    "name": "issue-123",
    "issue_id": "ISSUE-123",
    "repo_selector": "id:repo",
    "dependencies": [],
    "prompt": "Implement ISSUE-123 and stop after verification.",
    "stop_condition": "Focused and broad relevant checks pass.",
}

dataset: Dataset[ContractInput, bool, None] = Dataset(
    name="supervisor_plan_contract",
    cases=[
        Case(
            name="accept_independent_lane_proposal",
            inputs={
                "plan": {
                    "objective": "Ship ready work.",
                    "rationale": "The lane is unblocked.",
                    "next_action": "propose_lanes",
                    "owner_question": None,
                    "lanes": [LANE],
                }
            },
            expected_output=True,
        ),
        Case(
            name="reject_needs_owner_without_question",
            inputs={
                "plan": {
                    "objective": "Ship ready work.",
                    "rationale": "A product choice is missing.",
                    "next_action": "needs_owner",
                    "owner_question": None,
                    "lanes": [],
                }
            },
            expected_output=False,
        ),
        Case(
            name="accept_wait_without_lanes",
            inputs={
                "plan": {
                    "objective": "Ship ready work.",
                    "rationale": "Every issue is blocked.",
                    "next_action": "wait",
                    "owner_question": None,
                    "lanes": [],
                }
            },
            expected_output=True,
        ),
    ],
    evaluators=[EqualsExpected()],
)


def main() -> None:
    """Run the local planner contract eval."""

    report = dataset.evaluate_sync(validates_supervisor_plan)
    report.print(include_input=True, include_output=True)


if __name__ == "__main__":
    main()
