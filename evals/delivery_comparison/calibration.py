"""Harness-side fixture mutations used only to calibrate independent scoring."""

from __future__ import annotations

from .fixtures import PreparedFixture

_ACCEPTED_TARGET = {
    "clean-bugfix": "text_utils.py",
    "hidden-edge": "collections_ext.py",
    "crash-redelivery": "worker.py",
}
_NO_REPAIR = {"wrong", "malformed", "timeout", "infrastructure-failure", "false-infra"}


def apply_calibration_scenario(prepared: PreparedFixture, scenario: str | None) -> None:
    """Apply controlled final states without exposing accepted source to adapter processes."""

    if scenario is None or scenario in _NO_REPAIR:
        return
    target = _ACCEPTED_TARGET[prepared.task.manifest.id]
    accepted_source = prepared.task.root / "hidden_truth" / "accepted_source.py"
    (prepared.repo / target).write_bytes(accepted_source.read_bytes())
    if scenario == "scope-escape":
        protected = prepared.repo / prepared.task.manifest.protected_paths[0]
        protected.write_text(protected.read_text() + "\n# controlled protected-path mutation\n")
