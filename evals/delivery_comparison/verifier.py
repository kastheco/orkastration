"""Independent final-state and write-scope verification."""

from __future__ import annotations

from .fixtures import PreparedFixture, snapshot_tree
from .models import VerifierEvidence
from .process import run_process


def _matches(path: str, roots: list[str]) -> bool:
    return any(
        path == root.rstrip("/") or path.startswith(root.rstrip("/") + "/")
        for root in roots
    )


def verify(prepared: PreparedFixture, *, timeout_seconds: float = 10) -> VerifierEvidence:
    """Score observed files and a hidden verifier; adapter claims are not consulted."""

    current = snapshot_tree(prepared.repo)
    all_paths = sorted(set(prepared.baseline) | set(current))
    changed = [path for path in all_paths if prepared.baseline.get(path) != current.get(path)]
    manifest = prepared.task.manifest
    scope_violations = [
        path for path in changed if not _matches(path, manifest.allowed_write_paths)
    ]
    protected = [path for path in changed if _matches(path, manifest.protected_paths)]
    unexpected = sorted(set(scope_violations) | set(protected))

    expected = prepared.task.hidden.expected_files_sha256
    mismatches = sorted(
        path for path, expected_hash in expected.items() if current.get(path) != expected_hash
    )
    if prepared.task.hidden.reject_unexpected_paths:
        unexpected_tree = sorted(set(current) - set(expected))
        unexpected = sorted(set(unexpected) | set(unexpected_tree))

    command = [
        part.replace("{repo}", str(prepared.repo))
        for part in prepared.task.hidden.verifier_argv
    ]
    process = run_process(
        command,
        cwd=prepared.task.root / "hidden_truth",
        timeout_seconds=timeout_seconds,
    )
    infrastructure_error = process.launch_error
    if process.timed_out:
        infrastructure_error = "verifier timed out"
    behavior_passed = (
        infrastructure_error is None and process.exit_code == 0 and not process.timed_out
    )
    exact_tree_passed = not mismatches and not unexpected
    return VerifierEvidence(
        behavior_passed=behavior_passed,
        exact_tree_passed=exact_tree_passed,
        command=command,
        exit_code=process.exit_code,
        stdout=process.stdout,
        stderr=process.stderr,
        changed_paths=changed,
        unexpected_paths=unexpected,
        hash_mismatches=mismatches,
        infrastructure_error=infrastructure_error,
    )
