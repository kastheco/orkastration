"""Readers for the exact adapter result and telemetry contracts."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from .models import AdapterResultBundle, TelemetryEvent


class BundleReadError(ValueError):
    """An adapter output does not satisfy the versioned protocol."""


def read_result_bundle(output: Path) -> AdapterResultBundle:
    path = output / "result.json"
    try:
        return AdapterResultBundle.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise BundleReadError(f"invalid result.json: {exc}") from exc


def read_telemetry(output: Path) -> list[TelemetryEvent]:
    path = output / "events.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise BundleReadError(f"invalid events.jsonl: {exc}") from exc
    events: list[TelemetryEvent] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            events.append(TelemetryEvent.model_validate_json(line))
        except ValidationError as exc:
            raise BundleReadError(f"invalid events.jsonl line {line_number}: {exc}") from exc
    sequences = [event.sequence for event in events]
    if sequences != list(range(len(events))):
        raise BundleReadError("telemetry sequence must be contiguous and zero-based")
    return events
