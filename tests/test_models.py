"""Planner contract tests."""

import pytest
from pydantic import ValidationError

from kasgraph.models import SupervisorPlan


def test_start_lane_requires_known_selected_lane() -> None:
    with pytest.raises(ValidationError, match="selected_lane_name"):
        SupervisorPlan.model_validate(
            {
                "rationale": "Start work.",
                "next_action": "start_lane",
                "selected_lane_name": "missing",
                "lanes": [],
            }
        )


def test_needs_owner_requires_question() -> None:
    with pytest.raises(ValidationError, match="owner_question"):
        SupervisorPlan.model_validate(
            {"rationale": "A product decision is missing.", "next_action": "needs_owner"}
        )
