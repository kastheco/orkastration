"""Typed test-data factories shared across orkastrator contract suites."""

from __future__ import annotations

import json


def graph_config_data(*, max_parallel_lanes: int = 2) -> dict[str, object]:
    """Return a complete, minimal v2 graph configuration mapping."""

    profile = {"agent": "codex", "model": "gpt-test", "strength": "high"}
    return {
        "version": 2,
        "max_parallel_lanes": max_parallel_lanes,
        "max_parallel_workers": 4,
        "review_cycle": {
            "initial_scope": "lane_changeset",
            "freeze_findings_after_initial_review": True,
            "max_fix_rounds_per_finding": 2,
            "re_review_scope": [
                "finding_contract",
                "relevant_original_context",
                "fixer_diff",
                "validation_evidence",
            ],
            "new_findings_during_re_review": {
                "introduced_by_fix": "accept",
                "otherwise": "defer_to_next_review_run",
            },
            "scope": {
                "escape": "stop_and_escalate",
                "reject_out_of_scope_diff": True,
                "required_boundary": "paths",
                "symbols": "enforce_when_adapter_available",
            },
            "parallel_fixers": {
                "max_per_lane": 2,
                "workspace": "isolated_from_review_revision",
                "require_disjoint_write_scopes": True,
                "on_overlap": "serialize",
                "integration": "serial",
            },
            "escalation": profile,
        },
        "roles": {
            "worker": profile,
            "initial_reviewer": profile,
            "fixer": profile,
            "re_reviewer": profile,
        },
        "publication": {
            "authorized_by": "graph_acceptance",
            "scope": "accepted_run",
            "branch": {"create": True, "push": True, "force_push": False},
            "pull_request": {
                "create_or_update": True,
                "initial_state": "draft",
                "mark_ready_after_final_gate": True,
            },
            "merge": False,
            "deploy": False,
        },
        "final_gate": {
            "type": "ci",
            "provider": "auto",
            "repository": "lane_repository",
            "run_on": "integrated_fix_set",
            "require_remote": True,
            "require_all_checks": True,
            "restart_initial_review": False,
            "on_failure": {
                "create_scoped_finding": True,
                "scope_source": [
                    "failing_check",
                    "failure_output",
                    "implicated_fix_commits",
                ],
                "max_fix_rounds": 2,
                "scope_escape": "stop_and_escalate",
            },
        },
    }


def review_finding_data(
    number: int = 1, *, dependencies: list[str] | None = None
) -> dict[str, object]:
    """Return one valid frozen review finding mapping."""

    return {
        "id": f"finding-{number}",
        "review_revision": {
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "diff_sha256": "c" * 64,
        },
        "evidence": [
            {
                "location": {"path": f"src/file{number}.py", "start_line": 1, "end_line": 2},
                "claim": "The result is wrong.",
            }
        ],
        "failure_mode": "Wrong result.",
        "required_outcome": "Return the right result.",
        "allowed_write_scope": {"paths": [f"src/file{number}.py"], "symbols": []},
        "forbidden_scope": [],
        "validation": [{"command": "pytest", "expected": "passes"}],
        "dependencies": dependencies or [],
    }


def initial_review_report_json(*findings: dict[str, object]) -> str:
    """Encode one valid initial review report."""

    return json.dumps(
        {
            "review_revision": review_finding_data()["review_revision"],
            "summary": "Reviewed.",
            "findings": list(findings),
        }
    )
