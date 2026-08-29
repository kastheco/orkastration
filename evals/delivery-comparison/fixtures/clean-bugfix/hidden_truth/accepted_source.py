"""Small text helpers."""


def normalize_label(value: str) -> str:
    """Trim a label and collapse internal whitespace to one space."""
    return " ".join(value.split())
