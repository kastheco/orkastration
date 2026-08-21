"""Graph proposal contract tests."""

import pytest
from pydantic import ValidationError

from kasgraph.models import SupervisorPlan


def proposal(**lane_overrides: object) -> dict[str, object]:
    lane: dict[str, object] = {
        "name": "issue-123",
        "issue_id": "ISSUE-123",
        "repo_selector": "id:repo",
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
