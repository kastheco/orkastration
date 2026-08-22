"""Graph proposal contract tests."""

import pytest
from pydantic import ValidationError

from kasgraph.models import (
    FixAttempt,
    InitialReviewReport,
    ReReviewResult,
    SupervisorPlan,
    workflow_contract_schemas,
)


def proposal(**lane_overrides: object) -> dict[str, object]:
    lane: dict[str, object] = {
        "name": "issue-123",
        "issue_id": "ISSUE-123",
        "repo_selector": "id:repo",
        "base_ref": "main",
        "dependencies": [],
        "prompt": "Implement the issue.",
        "stop_condition": "Tests pass.",
    }
    lane.update(lane_overrides)
    return {
        "objective": "Ship work",
        "rationale": "It is unblocked.",
        "next_action": "propose_lanes",
        "owner_question": None,
        "lanes": [lane],
    }


def test_proposal_accepts_independent_lane() -> None:
    value = SupervisorPlan.model_validate(proposal(dependencies=["ISSUE-100"]))
    assert value.lanes[0].dependencies == ["ISSUE-100"]


def test_duplicate_lane_name_is_rejected() -> None:
    raw = proposal()
    lanes = raw["lanes"]
    assert isinstance(lanes, list)
    lane = lanes[0]
    assert isinstance(lane, dict)
    lanes.append({**lane, "issue_id": "ISSUE-124"})
    with pytest.raises(ValidationError, match="lane names"):
        SupervisorPlan.model_validate(raw)


def test_selected_lanes_cannot_depend_on_each_other() -> None:
    raw = proposal(dependencies=["ISSUE-124"])
    lanes = raw["lanes"]
    assert isinstance(lanes, list)
    lanes.append(
        {
            "name": "issue-124",
            "issue_id": "ISSUE-124",
            "repo_selector": "id:repo",
            "base_ref": "main",
            "dependencies": [],
            "prompt": "Implement the dependency.",
            "stop_condition": "Tests pass.",
        }
    )
    with pytest.raises(ValidationError, match="depends on another selected lane"):
        SupervisorPlan.model_validate(raw)


def test_needs_owner_requires_question() -> None:
    with pytest.raises(ValidationError, match="owner_question"):
        SupervisorPlan.model_validate(
            {
                "objective": "Ship work",
                "rationale": "A decision is missing.",
                "next_action": "needs_owner",
                "lanes": [],
            }
        )


def finding(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "finding-003",
        "review_revision": {
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "diff_sha256": "c" * 64,
        },
        "evidence": [
            {
                "location": {"path": "src/app.py", "start_line": 10, "end_line": 12},
                "claim": "The branch drops the failure result.",
            }
        ],
        "failure_mode": "A failed operation is reported as successful.",
        "required_outcome": "Preserve and return the failed state.",
        "allowed_write_scope": {"paths": ["src/app.py"], "symbols": ["run_operation"]},
        "forbidden_scope": ["database migrations"],
        "validation": [{"command": "pytest tests/test_app.py", "expected": "passes"}],
        "dependencies": [],
    }
    value.update(overrides)
    return value


def test_initial_review_report_rejects_unknown_finding_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown finding"):
        InitialReviewReport.model_validate(
            {
                "review_revision": finding()["review_revision"],
                "summary": "One finding.",
                "findings": [finding(dependencies=["finding-999"])],
            }
        )


@pytest.mark.parametrize(
    "attempt",
    [
        {
            "finding_id": "finding-003",
            "round": 3,
            "status": "fixed",
            "base_sha": "b" * 40,
            "commit_sha": "d" * 40,
            "changed_paths": ["src/app.py"],
            "validation_results": [],
            "scope_expansion_required": None,
        },
        {
            "finding_id": "finding-003",
            "round": 1,
            "status": "fixed",
            "base_sha": "b" * 40,
            "commit_sha": None,
            "changed_paths": ["src/app.py"],
            "validation_results": [],
            "scope_expansion_required": None,
        },
    ],
)
def test_fix_attempt_enforces_round_and_commit_invariants(attempt: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FixAttempt.model_validate(attempt)


def test_re_review_result_accepts_only_closed_verdict_set() -> None:
    with pytest.raises(ValidationError):
        ReReviewResult.model_validate(
            {
                "finding_id": "finding-003",
                "round": 1,
                "reviewed_commit_sha": "d" * 40,
                "verdict": "new_unrelated_finding",
                "rationale": "Unrelated.",
                "evidence": [],
            }
        )


def test_workflow_contract_schemas_are_stable_and_strict() -> None:
    first = workflow_contract_schemas()
    second = workflow_contract_schemas()
    assert first == second
    assert first["initial_review_report"]["additionalProperties"] is False
    assert first["fix_attempt"]["title"] == "FixAttempt"
