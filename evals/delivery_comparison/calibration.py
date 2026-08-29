"""Harness-side fixture mutations used only to calibrate independent scoring."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .fixtures import PreparedFixture

_ACCEPTED_TARGET = {
    "clean-bugfix": "text_utils.py",
    "hidden-edge": "collections_ext.py",
    "crash-redelivery": "worker.py",
}
_NO_REPAIR = {
    "wrong",
    "malformed",
    "timeout",
    "infrastructure-failure",
    "false-infra",
    "service-infra-zero",
    "service-infra-nonzero",
}


@dataclass(frozen=True)
class EffectMutation:
    effect_count: int
    commit_count: int
    action_id: str | None
    commit_sha: str | None


def _accepted_source(prepared: PreparedFixture) -> tuple[str, bytes]:
    target = _ACCEPTED_TARGET[prepared.task.manifest.id]
    source = prepared.task.root / "hidden_truth" / "accepted_source.py"
    return target, source.read_bytes()


def apply_calibration_scenario(prepared: PreparedFixture, scenario: str | None) -> None:
    """Apply controlled final states without exposing accepted source to adapter processes."""

    if scenario is None or scenario in _NO_REPAIR:
        return
    target, content = _accepted_source(prepared)
    (prepared.repo / target).write_bytes(content)
    if scenario == "scope-escape":
        protected = prepared.repo / prepared.task.manifest.protected_paths[0]
        protected.write_text(protected.read_text() + "\n# controlled protected-path mutation\n")


def _commit(prepared: PreparedFixture, message: str, *, allow_empty: bool = False) -> str:
    subprocess.run(["git", "add", "--all"], cwd=prepared.repo, check=True)
    argv = ["git", "commit", "-qm", message]
    if allow_empty:
        argv.insert(2, "--allow-empty")
    subprocess.run(argv, cwd=prepared.repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=prepared.repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def apply_crash_effect(
    prepared: PreparedFixture, scenario: str | None, handshake_action_id: str
) -> EffectMutation:
    """Perform and Git-commit calibration effects under harness ownership."""

    if scenario is None:
        raise RuntimeError("production independent effect/ack contract is not implemented")
    if scenario in {"crash-missing-effect", "crash-fabricated-chain"}:
        return EffectMutation(0, 0, None, None)
    effect_action = (
        "wrong-effect-action" if scenario == "crash-wrong-effect-action" else handshake_action_id
    )
    target, content = _accepted_source(prepared)
    (prepared.repo / target).write_bytes(content)
    commit_sha = _commit(prepared, f"effect {effect_action}")
    if scenario == "crash-duplicate-effect":
        commit_sha = _commit(prepared, f"duplicate effect {effect_action}", allow_empty=True)
        return EffectMutation(2, 2, effect_action, commit_sha)
    return EffectMutation(1, 1, effect_action, commit_sha)
