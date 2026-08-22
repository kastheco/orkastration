"""Portable literal path-scope policy shared by scheduling and validation."""

from __future__ import annotations


def path_allowed(path: str, boundaries: list[str] | tuple[str, ...]) -> bool:
    """Match an exact file or a declared directory subtree."""

    return any(path_contains(boundary, path) for boundary in boundaries)


def scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Return whether two literal file/subtree scopes may touch the same path."""

    return any(path_contains(a, b) or path_contains(b, a) for a in left for b in right)


def path_contains(boundary: str, candidate: str) -> bool:
    """Return whether a literal boundary contains an exact path."""

    normalized = boundary.rstrip("/")
    return candidate == normalized or candidate.startswith(f"{normalized}/")
