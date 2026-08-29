"""Collection helpers."""

from typing import Any


def dedupe_stable(values: list[Any]) -> list[Any]:
    """Return equal values once while preserving first-seen order."""
    return list(dict.fromkeys(values))
