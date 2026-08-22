"""Deterministic orkastrator contract evaluations."""

from __future__ import annotations

from typing import TypedDict

from pydantic import ValidationError
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected

from orkastrator.models import (
    InitialReviewReport,
    ReReviewResult,
    SupervisorPlan,
    WorkflowContract,
)


class ContractInput(TypedDict):
    plan: dict[str, object]


class WorkflowContractInput(TypedDict):
    contract: str
    value: dict[str, object]


def validates_supervisor_plan(value: ContractInput) -> bool:
    """Return whether a candidate satisfies the exact supervisor proposal contract."""

    try:
        SupervisorPlan.model_validate(value["plan"])
    except ValidationError:
        return False
    return True


def validates_workflow_contract(value: WorkflowContractInput) -> bool:
    """Return whether a candidate satisfies its named workflow contract."""

    contracts: dict[str, type[WorkflowContract]] = {
        "initial_review_report": InitialReviewReport,
        "re_review_result": ReReviewResult,
    }
    contract = contracts[value["contract"]]
    try:
        contract.model_validate(value["value"])
    except ValidationError:
        return False
    return True


LANE: dict[str, object] = {
    "name": "issue-123",
    "issue_id": "ISSUE-123",
    "repo_selector": "id:repo",
    "base_ref": "main",
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

REVISION: dict[str, object] = {
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
    "diff_sha256": "c" * 64,
}

workflow_dataset: Dataset[WorkflowContractInput, bool, None] = Dataset(
    name="workflow_contracts",
    cases=[
        Case(
            name="accept_frozen_initial_review",
            inputs={
                "contract": "initial_review_report",
                "value": {
                    "review_revision": REVISION,
                    "summary": "No findings.",
                    "findings": [],
                },
            },
            expected_output=True,
        ),
        Case(
            name="reject_unrelated_re_review_verdict",
            inputs={
                "contract": "re_review_result",
                "value": {
                    "finding_id": "finding-003",
                    "round": 1,
                    "reviewed_commit_sha": "d" * 40,
                    "verdict": "new_unrelated_finding",
                    "rationale": "This must be deferred to a new review run.",
                    "evidence": [],
                },
            },
            expected_output=False,
        ),
    ],
    evaluators=[EqualsExpected()],
)


def main() -> None:
    """Run the local supervisor-proposal and workflow contract evals."""

    report = dataset.evaluate_sync(validates_supervisor_plan)
    report.print(include_input=True, include_output=True)
    workflow_report = workflow_dataset.evaluate_sync(validates_workflow_contract)
    workflow_report.print(include_input=True, include_output=True)


if __name__ == "__main__":
    main()
