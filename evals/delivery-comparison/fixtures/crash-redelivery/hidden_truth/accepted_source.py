"""Delivery application logic."""

from typing import Any


def apply_delivery(state: dict[str, Any], action_id: str, amount: int) -> dict[str, Any]:
    """Apply an increment at most once for a stable action identifier."""
    applied = state.setdefault("applied", [])
    if action_id in applied:
        return state
    state["total"] = int(state.get("total", 0)) + amount
    applied.append(action_id)
    return state
