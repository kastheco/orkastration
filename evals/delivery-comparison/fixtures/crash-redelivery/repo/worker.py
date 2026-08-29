"""Delivery application logic."""

from typing import Any


def apply_delivery(state: dict[str, Any], action_id: str, amount: int) -> dict[str, Any]:
    """Apply a delivered increment and record its action identifier."""
    state["total"] = int(state.get("total", 0)) + amount
    state.setdefault("applied", []).append(action_id)
    return state
