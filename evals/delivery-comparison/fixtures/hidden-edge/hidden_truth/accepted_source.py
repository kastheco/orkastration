"""Collection helpers."""

from typing import Any


def dedupe_stable(values: list[Any]) -> list[Any]:
    """Return equal values once while preserving first-seen order."""
    result: list[Any] = []
    for value in values:
        if not any(value == existing for existing in result):
            result.append(value)
    return result
