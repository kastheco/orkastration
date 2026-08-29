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


def read_telemetry(
    output: Path, *, trial_id: str, adapter_id: str, task_id: str, start_sequence: int = 0
) -> list[TelemetryEvent]:
    path = output / "events.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise BundleReadError(f"invalid events.jsonl: {exc}") from exc
    events: list[TelemetryEvent] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            event = TelemetryEvent.model_validate_json(line)
        except ValidationError as exc:
            raise BundleReadError(f"invalid events.jsonl line {line_number}: {exc}") from exc
        if (event.trial_id, event.adapter_id, event.task_id) != (trial_id, adapter_id, task_id):
            raise BundleReadError(f"telemetry identity mismatch at line {line_number}")
        events.append(event)
    sequences = [event.sequence for event in events]
    if sequences != list(range(start_sequence, start_sequence + len(events))):
        raise BundleReadError(
            f"telemetry sequence must be contiguous from {start_sequence}; got {sequences}"
        )
    return events
