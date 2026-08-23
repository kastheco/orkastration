"""Dynamic convergence scheduling and restart tests."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from orkastrator.config import AgentProfile, GraphConfig, StageBudget, StageBudgets
from orkastrator.db import AcceptanceAuthorizationRow, EventRow, LaneRow
from orkastrator.execution import ExecutionController, _format_frozen_diff, _next_key
from orkastrator.git import GitCommandResult, GitError, LocalGit
from orkastrator.models import (
    CiCheckResult,
    CiReceipt,
    FindingPhase,
    FindingReason,
    LanePhase,
    LaneRecord,
    PublicationReceipt,
    ReReviewResult,
    ReviewRevision,
    StageKind,
    StagePhase,
    SupervisorPlan,
    ValidationRequirement,
    ValidationResult,
)
from orkastrator.orca import JsonObject, OrcaError, OrcaTimeout
from orkastrator.publication import PublicationError, PullRequestLanded
from orkastrator.store import StateStore
from tests.factories import (
    graph_config_data,
    initial_review_report_json,
    review_finding_data,
)


def config(
    *, max_parallel_lanes: int = 2, max_workers: int = 4, max_fixers: int = 2
) -> GraphConfig:
    raw = graph_config_data(max_parallel_lanes=max_parallel_lanes)
    raw["max_parallel_workers"] = max_workers
    review_cycle = raw["review_cycle"]
    assert isinstance(review_cycle, dict)
    parallel = review_cycle["parallel_fixers"]
    assert isinstance(parallel, dict)
    parallel["max_per_lane"] = max_fixers
    roles = raw["roles"]
    assert isinstance(roles, dict)
    fixer = roles["fixer"]
    assert isinstance(fixer, dict)
    fixer = dict(fixer)
    roles["fixer"] = fixer
    fixer["fallback"] = {
        "agent": "codex",
        "model": "gpt-fallback",
        "strength": "high",
        "trigger": "capability_mismatch",
    }
    return GraphConfig.model_validate(raw)


def proposal(*, lane_count: int = 1, next_action: str = "propose_lanes") -> SupervisorPlan:
    lanes = [
        {
            "name": f"issue-{number}",
            "issue_id": f"ISSUE-{number}",
            "repo_selector": "id:repo",
            "base_ref": "main",
            "dependencies": [],
            "prompt": f"Implement ISSUE-{number}.",
            "stop_condition": "Tests pass.",
        }
        for number in range(100, 100 + lane_count)
    ]
    return SupervisorPlan.model_validate(
        {
            "objective": "Do work",
            "rationale": "Ready.",
            "next_action": next_action,
            "owner_question": "Choose a scope." if next_action == "needs_owner" else None,
            "lanes": lanes if next_action == "propose_lanes" else [],
        }
    )


def worker_result() -> str:
    return json.dumps(
        {
            "review_revision": review_finding_data()["review_revision"],
            "commit_sha": "b" * 40,
            "changed_paths": ["src/file1.py"],
            "validation_results": [{"command": "pytest", "status": "passed", "output": "passed"}],
            "summary": "Implemented and verified.",
        }
    )


def worker_blocked(*, question: str = "Fix all four, or approve the two warnings as-is?") -> str:
    return json.dumps(
        {
            "status": "blocked",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "summary": "Guard restoration and upload lifetime disagree.",
            "decision": {
                "question": question,
                "options": ["fix all four", "approve the two warnings"],
                "consequence": "Approving leaves the owner unable to submit.",
                "allowed_write_scope": {"paths": ["src/file1.py"], "symbols": []},
            },
        }
    )


def fixer_sha(finding_id: str, round: int) -> str:
    number = int(finding_id.rsplit("-", maxsplit=1)[-1])
    return f"{1_000 + round * 100 + number:040x}"


def fix_attempt(
    finding_id: str = "finding-1",
    *,
    round: int = 1,
    status: str = "fixed",
    base_sha: str | None = None,
    changed_path: str | None = None,
    validation: list[dict[str, str]] | None = None,
) -> str:
    commit = fixer_sha(finding_id, round)
    base = base_sha or "b" * 40
    path_number = finding_id.rsplit("-", maxsplit=1)[-1]
    return json.dumps(
        {
            "finding_id": finding_id,
            "round": round,
            "status": status,
            "base_sha": base,
            "commit_sha": commit if status == "fixed" else None,
            "changed_paths": [changed_path or f"src/file{path_number}.py"]
            if status == "fixed"
            else [],
            "validation_results": (
                validation
                if validation is not None
                else [{"command": "pytest", "status": "passed", "output": "passed"}]
            ),
            "scope_expansion_required": (
                {"paths": ["src/shared.py"], "reason": "Required."}
                if status == "blocked_scope"
                else None
            ),
        }
    )


def re_review(
    verdict: str,
    *,
    finding_id: str = "finding-1",
    round: int = 1,
    new_findings: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "finding_id": finding_id,
            "round": round,
            "reviewed_commit_sha": fixer_sha(finding_id, round),
            "verdict": verdict,
            "rationale": "Checked the exact fix.",
            "evidence": [],
            "new_findings": new_findings or [],
        }
    )


def escalation(
    reason: str,
    action: str = "block",
    *,
    round: int = 1,
    finding_id: str = "finding-1",
    revised_finding: dict[str, object] | None = None,
) -> str:
    return json.dumps(
        {
            "finding_id": finding_id,
            "round": round,
            "reason": reason,
            "action": action,
            "rationale": "Bounded adjudication.",
            "revised_finding": revised_finding,
        }
    )


class FakeOrca:
    def __init__(self) -> None:
        self.tasks_by_id: dict[str, JsonObject] = {}
        self.dependencies: dict[str, list[str]] = {}
        self.starts: list[dict[str, object]] = []
        self.releases: list[str] = []
        self.released_terminals: list[str | None] = []
        self.turns: list[str] = []
        self.unreadable_turns = False
        self.runs_by_id: dict[str, JsonObject] = {}
        self.bound_runs: list[str] = []
        self.unreadable_dispatches: set[str] = set()
        self.messages_by_run: dict[str, list[JsonObject]] = {}
        self.replies: list[tuple[str, str, str]] = []
        self.fail_after_run_create = False
        self.fail_after_task_create = False
        self.fail_after_worker_start = False
        self.refuse_starts: set[str] = set()
        self.timeout_starts: set[str] = set()

    async def create_run(self, objective: str) -> tuple[str, JsonObject]:
        assert objective.startswith("Do work\n\norkastrator run: ")
        run_id = f"orca-run-{len(self.runs_by_id) + 1}"
        self.runs_by_id[run_id] = {"id": run_id, "objective": objective}
        if self.fail_after_run_create:
            self.fail_after_run_create = False
            raise RuntimeError("crash after remote run creation")
        return run_id, {"ok": True}

    async def runs(self) -> list[JsonObject]:
        return list(self.runs_by_id.values())

    async def use_run(self, orca_run_id: str) -> JsonObject:
        self.bound_runs.append(orca_run_id)
        return {"ok": True}

    async def messages(self, orca_run_id: str, limit: int = 200) -> list[JsonObject]:
        return list(self.messages_by_run.get(orca_run_id, []))

    async def reply(self, orca_run_id: str, message_id: str, body: str) -> JsonObject:
        self.replies.append((orca_run_id, message_id, body))
        return {"ok": True}

    async def create_task(self, spec: str, dependencies: list[str]) -> tuple[str, JsonObject]:
        task_id = f"task-{len(self.tasks_by_id) + 1}"
        self.dependencies[task_id] = dependencies
        self.tasks_by_id[task_id] = {
            "id": task_id,
            "status": "ready" if not dependencies else "pending",
            "spec": spec,
            "result": None,
        }
        self._refresh_ready()
        if self.fail_after_task_create:
            self.fail_after_task_create = False
            raise RuntimeError("crash after remote task creation")
        return task_id, {"ok": True}

    async def tasks(self, orca_run_id: str) -> list[JsonObject]:
        assert orca_run_id.startswith("orca-run-")
        self._refresh_ready()
        return list(self.tasks_by_id.values())

    async def start_worker(self, **kwargs: object) -> tuple[str, str, str | None, JsonObject]:
        task_id = str(kwargs["task_id"])
        if task_id in self.refuse_starts:
            # Refused outright: no Dispatch exists and none will.
            raise OrcaError(f"Orca command failed with rc=1: task_not_startable {task_id}")
        self.tasks_by_id[task_id]["status"] = "dispatched"
        requested = kwargs.get("worktree_id")
        if requested is not None:
            worktree_id = str(requested)
        elif kwargs.get("parent_worktree_id") is not None:
            worktree_id = f"repo::/tmp/{kwargs['lane_name']}"
        else:
            worktree_id = "repo::/tmp/issue"
        dispatch_id = f"dispatch-{len(self.starts) + 1}"
        self.starts.append({**kwargs, "dispatch_id": dispatch_id, "worktree_id": worktree_id})
        if task_id in self.timeout_starts:
            # The dangerous half: Orca made the Dispatch and then lost the reply.
            self.timeout_starts.discard(task_id)
            raise OrcaTimeout("Orca command timed out after 600s")
        if self.fail_after_worker_start:
            self.fail_after_worker_start = False
            raise RuntimeError("crash after remote worker start")
        terminal_handle = f"term-{dispatch_id}"
        self.starts[-1]["terminal_handle"] = terminal_handle
        return dispatch_id, worktree_id, terminal_handle, {"ok": True, "dispatchId": dispatch_id}

    async def release_worker(
        self, dispatch_id: str, terminal_handle: str | None = None
    ) -> JsonObject:
        self.releases.append(dispatch_id)
        self.released_terminals.append(terminal_handle)
        return {"ok": True}

    async def worker_turns(self, dispatch_id: str, limit: int = 50) -> list[str]:
        if self.unreadable_turns:
            raise OrcaError("worker-read failed")
        return list(self.turns)

    async def worker_dispatch(self, task_id: str) -> tuple[str, str | None] | None:
        if task_id in self.unreadable_dispatches:
            raise OrcaError("dispatch-show failed")
        for start in self.starts:
            if start["task_id"] == task_id:
                return str(start["dispatch_id"]), str(start["worktree_id"])
        return None

    def complete_dispatched(self, body: str | None = None) -> None:
        resolved_body = worker_result() if body is None else body
        for task_id, task in self.tasks_by_id.items():
            if task["status"] != "dispatched":
                continue
            task["status"] = "completed"
            task["result"] = json.dumps(
                {
                    "provenance": "worker_report",
                    "outcome": "succeeded",
                    "messageId": f"message-{task_id}",
                    "reportedBy": "terminal-1",
                    "subject": "done",
                    "body": resolved_body,
                    "completedBy": "terminal-1",
                    "filesModified": [],
                    "reportPath": None,
                    "completedAt": datetime.now(UTC).isoformat(),
                }
            )
        self._refresh_ready()

    def abandon_dispatched(self) -> None:
        """Send every dispatched Task back to ready with no result, as Orca does
        when the supervised worker's terminal goes away without reporting."""

        for task in self.tasks_by_id.values():
            if task["status"] == "dispatched":
                task["status"] = "ready"

    def complete_task(self, task_id: str, body: str) -> None:
        task = self.tasks_by_id[task_id]
        task["status"] = "completed"
        task["result"] = json.dumps(
            {
                "provenance": "worker_report",
                "outcome": "succeeded",
                "messageId": f"message-{task_id}",
                "reportedBy": "terminal-1",
                "subject": "done",
                "body": body,
                "completedBy": "terminal-1",
                "filesModified": [],
                "reportPath": None,
                "completedAt": datetime.now(UTC).isoformat(),
            }
        )
        self._refresh_ready()

    def _refresh_ready(self) -> None:
        for task_id, dependencies in self.dependencies.items():
            if self.tasks_by_id[task_id]["status"] != "pending":
                continue
            if all(self.tasks_by_id[item]["status"] == "completed" for item in dependencies):
                self.tasks_by_id[task_id]["status"] = "ready"


class FakeGit(LocalGit):
    def __init__(self) -> None:
        self.heads: dict[str, str] = {"repo::/tmp/issue": "b" * 40}
        self.integrated: dict[str, str] = {}
        self.changed_override: list[str] | None = None
        self.head_override: str | None = None
        self.ancestor_override: bool | None = None
        self.lane_head_override: str | None = None
        self.conflict = False
        # Conflict for one named fix rather than for the whole run, which is what a
        # lane head moving under one finding actually looks like.
        self.conflict_commits: set[str] = set()
        self.crash_after_cherry_pick = False
        self.integration_count = 0
        self.commit_count_override: int | None = None
        self.commit_chain_override: list[str] | None = None
        self.validation_calls: list[list[str]] = []
        self.clean_override: bool | None = None
        self.lane_clean_override: bool | None = None
        self.in_progress = False
        self.pre_sequence_head: str | None = None
        self.crash_mid_sequence = False
        self.active_sequence_commits: list[str] | None = None
        self.abort_calls = 0
        self.diff_sha256_override: str | None = None
        self.rendered_diff_override: str | None = None
        self.render_diff_calls: list[tuple[str, str, str, tuple[str, ...]]] = []
        self.lane_validation_fails = False

    async def head(self, worktree_id: str) -> str:
        if worktree_id == "repo::/tmp/issue" and self.lane_head_override is not None:
            return self.lane_head_override
        if self.head_override is not None and "finding-" in worktree_id:
            return self.head_override
        if worktree_id in self.heads:
            return self.heads[worktree_id]
        for number in range(1, 10):
            if f"finding-{number}" in worktree_id:
                return fixer_sha(f"finding-{number}", 2 if "r2" in worktree_id else 1)
        # A worktree no test has given a head to is one nothing has committed
        # in, which is a fresh checkout sitting on the base `resolve_ref`
        # reports. Every stage start now reads its worktree head to record the
        # baseline it will later be measured against, so this is reached for
        # any lane worktree a test never set up, and raising there would fail
        # tests that have nothing to do with what is in the worktree.
        return "a" * 40

    async def changed_paths(
        self,
        worktree_id: str,
        base_sha: str,
        head_sha: str,
        paths: Sequence[str] = (),
    ) -> list[str]:
        if self.changed_override is not None and "finding-" in worktree_id:
            return self.changed_override
        if paths:
            return sorted(paths)
        for number in range(1, 10):
            if f"finding-{number}" in worktree_id:
                return (
                    ["src/file1.py"] if "ci-finding-" in worktree_id else [f"src/file{number}.py"]
                )
        return ["src/file1.py"]

    async def render_diff(
        self,
        worktree_id: str,
        base_sha: str,
        head_sha: str,
        paths: Sequence[str] = (),
    ) -> str:
        self.render_diff_calls.append((worktree_id, base_sha, head_sha, tuple(paths)))
        if self.rendered_diff_override is not None:
            return self.rendered_diff_override
        path = paths[0] if paths else "src/file1.py"
        return f"diff --git a/{path} b/{path}\n+frozen {path}\n"

    async def resolve_ref(self, worktree_id: str, ref: str) -> str:
        return "a" * 40

    async def diff_sha256(self, worktree_id: str, base_sha: str, head_sha: str) -> str:
        if self.diff_sha256_override is not None and worktree_id == "repo::/tmp/issue":
            return self.diff_sha256_override
        return "c" * 64

    async def is_ancestor(self, worktree_id: str, base_sha: str, head_sha: str) -> bool:
        if self.ancestor_override is not None and "finding-" in worktree_id:
            return self.ancestor_override
        return True

    async def commit_count(self, worktree_id: str, base_sha: str, head_sha: str) -> int:
        return self.commit_count_override if self.commit_count_override is not None else 1

    async def commits_between(self, worktree_id: str, base_sha: str, head_sha: str) -> list[str]:
        return self.commit_chain_override or [head_sha]

    async def is_clean(self, worktree_id: str) -> bool:
        if worktree_id == "repo::/tmp/issue" and self.lane_clean_override is not None:
            return self.lane_clean_override
        if self.clean_override is not None and "finding-" in worktree_id:
            return self.clean_override
        return not self.in_progress if worktree_id == "repo::/tmp/issue" else True

    async def cherry_pick(self, worktree_id: str, commit_sha: str) -> GitCommandResult:
        return await self.cherry_pick_many(worktree_id, [commit_sha])

    async def cherry_pick_many(self, worktree_id: str, commit_shas: list[str]) -> GitCommandResult:
        self.pre_sequence_head = self.heads[worktree_id]
        if self.conflict or self.conflict_commits.intersection(commit_shas):
            self.in_progress = True
            self.active_sequence_commits = list(commit_shas)
            return GitCommandResult(1, "", "conflict")
        if self.crash_mid_sequence:
            self.crash_mid_sequence = False
            self.in_progress = True
            self.active_sequence_commits = list(commit_shas)
            self.heads[worktree_id] = "8" * 40
            raise RuntimeError("crash mid cherry-pick sequence")
        self.integration_count += 1
        integrated_sha = f"{self.integration_count + 100:040x}"
        self.heads[worktree_id] = integrated_sha
        self.integrated[commit_shas[-1]] = integrated_sha
        if self.crash_after_cherry_pick:
            self.crash_after_cherry_pick = False
            raise RuntimeError("crash after cherry-pick")
        return GitCommandResult(0, "", "")

    async def abort_cherry_pick(self, worktree_id: str) -> None:
        self.abort_calls += 1
        self.in_progress = False
        self.active_sequence_commits = None
        if self.pre_sequence_head is not None:
            self.heads[worktree_id] = self.pre_sequence_head

    async def cherry_pick_in_progress_commits(self, worktree_id: str) -> list[str] | None:
        return self.active_sequence_commits if self.in_progress else None

    async def find_cherry_pick(self, worktree_id: str, commit_sha: str) -> str | None:
        return self.integrated.get(commit_sha)

    async def validate(
        self, worktree_id: str, requirements: list[ValidationRequirement]
    ) -> list[ValidationResult]:
        self.validation_calls.append([item.command for item in requirements])
        status = (
            "failed"
            if self.lane_validation_fails and worktree_id == "repo::/tmp/issue"
            else "passed"
        )
        return [
            ValidationResult(command="pytest", status=status, output=status) for _ in requirements
        ]


class FakePublisher:
    def __init__(self, statuses: list[str] | None = None) -> None:
        self.statuses = list(statuses or ["passed"])
        self.publish_calls: list[tuple[str, str | None]] = []
        self.ready_calls: list[str] = []

    async def publish(
        self,
        *,
        run_id: str,
        lane: LaneRecord,
        head_sha: str,
        previous: PublicationReceipt | None,
    ) -> PublicationReceipt:
        self.publish_calls.append((head_sha, previous.head_sha if previous else None))
        return PublicationReceipt(
            run_id=run_id,
            lane=lane.name,
            remote_url="git@github.com:example/repo.git",
            base_branch="main",
            branch=f"orkastrator/{run_id[:12]}/{lane.name}",
            pull_request_url="https://github.com/example/repo/pull/1",
            head_sha=head_sha,
            draft=True,
        )

    async def checks(self, receipt: PublicationReceipt) -> CiReceipt:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        checks = []
        if status == "failed":
            checks = [
                CiCheckResult(name="tests", status="failed", output="tests/test_ci.py failed")
            ]
        return CiReceipt(provider="github", head_sha=receipt.head_sha, status=status, checks=checks)

    async def mark_ready(self, receipt: PublicationReceipt) -> PublicationReceipt:
        self.ready_calls.append(receipt.head_sha)
        return receipt.model_copy(update={"draft": False})


def controller(
    tmp_path: Path,
    orca: FakeOrca,
    *,
    graph_config: GraphConfig | None = None,
    git: FakeGit | None = None,
    publisher: FakePublisher | None = None,
) -> tuple[ExecutionController, StateStore]:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    return (
        ExecutionController(
            config=graph_config or config(),
            orca=orca,
            store=store,
            git=git or FakeGit(),
            publisher=publisher or FakePublisher(),
        ),
        store,
    )


async def advance_to_initial_review(
    value: ExecutionController, orca: FakeOrca, run_id: str
) -> None:
    orca.complete_dispatched()
    result = await value.monitor(run_id)
    assert [launch.role for launch in result.started] == [StageKind.INITIAL_REVIEWER]


async def advance_to_fixer(
    value: ExecutionController, orca: FakeOrca, run_id: str, *findings: dict[str, object]
) -> None:
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json(*findings))
    await value.monitor(run_id)


async def test_accept_materializes_only_worker_and_starts_first_wave(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    result = await value.accept(value.propose(proposal()).run_id)

    assert result.status == "active"
    assert [stage.role for stage in result.stages] == [StageKind.WORKER]
    assert [launch.role for launch in result.started] == [StageKind.WORKER]
    assert len(orca.tasks_by_id) == 1


async def test_no_findings_completes_after_initial_review(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)

    orca.complete_dispatched(initial_review_report_json())
    result = await value.monitor(run_id)

    assert result.status == "complete"
    assert result.findings == []
    assert [stage.role for stage in result.stages] == [
        StageKind.WORKER,
        StageKind.INITIAL_REVIEWER,
    ]


async def test_initial_review_spec_contains_the_lane_scoped_frozen_diff(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)

    await advance_to_initial_review(value, orca, run_id)

    task = next(item for item in orca.tasks_by_id.values() if item["status"] == "dispatched")
    spec = str(task["spec"])
    assert "Frozen diff input (supervisor-rendered; do not re-derive it)" in spec
    assert "Complete file index (1 files):\n- src/file1.py" in spec
    assert "+frozen src/file1.py" in spec
    assert git.render_diff_calls[-1] == (
        "repo::/tmp/issue",
        "a" * 40,
        "b" * 40,
        ("src/file1.py",),
    )


async def test_unrenderable_frozen_diff_does_not_abort_the_monitor_tick(
    tmp_path: Path,
) -> None:
    class MissingWorktreeGit(FakeGit):
        async def render_diff(
            self,
            worktree_id: str,
            base_sha: str,
            head_sha: str,
            paths: Sequence[str] = (),
        ) -> str:
            if worktree_id == "repo::/tmp/missing-lane":
                raise GitError("worktree path does not exist: /tmp/missing-lane")
            return await super().render_diff(worktree_id, base_sha, head_sha, paths)

    orca = FakeOrca()
    git = MissingWorktreeGit()
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal(lane_count=2)).run_id
    await value.accept(run_id)

    first_lane = store.lanes(run_id)[0]
    with Session(store._engine) as session:
        row = session.get(LaneRow, first_lane.lane_id)
        assert row is not None
        row.worktree_id = "repo::/tmp/missing-lane"
        session.commit()

    orca.complete_dispatched()
    result = await value.monitor(run_id)

    stages = {stage.lane_id: stage for stage in store.stages(run_id)}
    reviewer_stages = [stage for stage in stages.values() if stage.role is StageKind.INITIAL_REVIEWER]
    assert len(reviewer_stages) == 2
    assert next(stage for stage in reviewer_stages if stage.lane_id == first_lane.lane_id).orca_task_id is None
    assert next(stage for stage in reviewer_stages if stage.lane_id != first_lane.lane_id).orca_dispatch_id is not None
    assert [launch.role for launch in result.started] == [StageKind.INITIAL_REVIEWER]
    failures = [
        event["payload"]
        for event in store.events(run_id)
        if event["kind"] == "stage_start_failed"
    ]
    assert failures
    assert failures[-1]["released"] is True
    assert "worktree path does not exist" in failures[-1]["detail"]


async def test_fixer_and_reviewer_specs_scope_the_diff_to_the_finding(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data(2))

    fixer_task = next(item for item in orca.tasks_by_id.values() if item["status"] == "dispatched")
    assert "Complete file index (1 files):\n- src/file2.py" in str(fixer_task["spec"])
    assert git.render_diff_calls[-1][3] == ("src/file2.py",)

    orca.complete_dispatched(fix_attempt(finding_id="finding-2"))
    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.RE_REVIEWER
    rereview_spec = str(orca.tasks_by_id[result.started[0].task_id]["spec"])
    assert "Complete file index (1 files):\n- src/file2.py" in rereview_spec
    assert git.render_diff_calls[-1][3] == ("src/file2.py",)


async def test_over_budget_frozen_diff_is_chunked_by_file_without_truncation(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    graph = config().model_copy(update={"frozen_diff_budget_bytes": 65})
    value, _ = controller(tmp_path, orca, graph_config=graph, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    git.rendered_diff_override = (
        "diff --git a/src/file1.py b/src/file1.py\n+first complete record\n"
        "diff --git a/src/file2.py b/src/file2.py\n+second complete record\n"
    )
    finding = review_finding_data()
    finding["allowed_write_scope"] = {
        "paths": ["src/file1.py", "src/file2.py"],
        "symbols": [],
    }

    orca.complete_dispatched(initial_review_report_json(finding))
    result = await value.monitor(run_id)

    spec = str(orca.tasks_by_id[result.started[0].task_id]["spec"])
    assert "Complete file index (2 files):\n- src/file1.py\n- src/file2.py" in spec
    assert "Frozen diff chunk 1/2" in spec
    assert "Frozen diff chunk 2/2" in spec
    assert spec.count("+first complete record") == 1
    assert spec.count("+second complete record") == 1
    assert "no content is truncated" in spec


def test_non_utf8_frozen_diff_is_explicit_and_lossless() -> None:
    rendered = b"diff --git a/src/latin1.py b/src/latin1.py\n+value = 'caf\xe9'\n"
    revision = ReviewRevision(base_sha="a" * 40, head_sha="b" * 40, diff_sha256="c" * 64)

    spec = _format_frozen_diff(
        revision=revision,
        rendered=rendered,
        paths=["src/latin1.py"],
        budget_bytes=65_536,
    )

    assert f"rendered_bytes: {len(rendered)}" in spec
    assert "src/latin1.py: invalid UTF-8 bytes 0xe9" in spec
    assert "encoding=\"base64\"" in spec
    encoded = spec.split("<frozen_diff_chunk encoding=\"base64\">", 1)[1].split(
        "</frozen_diff_chunk>", 1
    )[0]
    assert base64.b64decode(encoded) == rendered


async def test_initial_review_must_match_worker_changeset_revision(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    report = json.loads(initial_review_report_json())
    report["review_revision"]["head_sha"] = "e" * 40

    orca.complete_dispatched(json.dumps(report))
    result = await value.monitor(run_id)

    assert result.status == "failed"
    assert result.findings == []


def _message(
    message_id: str,
    kind: str,
    *,
    from_handle: str,
    thread_id: str | None = None,
    created_at: str = "2026-08-22T10:16:56Z",
) -> JsonObject:
    return {
        "id": message_id,
        "type": kind,
        "from_handle": from_handle,
        "to_handle": "run:orca-run-1",
        "thread_id": thread_id if thread_id is not None else message_id,
        "subject": "Question",
        "body": "Choose fix or approve.",
        "created_at": created_at,
    }


async def test_monitor_reports_an_unanswered_question_against_its_stage(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    accepted = await value.accept(run_id)
    dispatch_id = accepted.started[0].dispatch_id
    orca.messages_by_run["orca-run-1"] = [
        _message("msg-1", "question", from_handle=f"dispatch:{dispatch_id}"),
        _message("msg-2", "heartbeat", from_handle=f"dispatch:{dispatch_id}"),
    ]

    result = await value.monitor(run_id)

    assert [(q.message_id, q.kind, q.lane, q.role) for q in result.questions] == [
        ("msg-1", "question", "issue-100", StageKind.WORKER)
    ]
    assert result.questions[0].dispatch_id == dispatch_id


async def test_monitor_drops_a_question_once_it_has_a_reply(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    accepted = await value.accept(run_id)
    dispatch_id = accepted.started[0].dispatch_id
    orca.messages_by_run["orca-run-1"] = [
        _message("msg-1", "question", from_handle=f"dispatch:{dispatch_id}"),
        _message("msg-2", "status", from_handle="run:orca-run-1", thread_id="msg-1"),
    ]

    result = await value.monitor(run_id)

    assert result.questions == []


async def test_answering_sends_the_reply_and_records_that_the_supervisor_directed_the_lane(
    tmp_path: Path,
) -> None:
    """The only trace of a supervisor's direction used to live in Orca's log.

    A lane that changes course because it was told to should be explicable from
    the supervisor's own ledger, not from a message store `show` cannot read.
    """

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    accepted = await value.accept(run_id)
    dispatch_id = accepted.started[0].dispatch_id
    orca.messages_by_run["orca-run-1"] = [
        _message("msg-1", "question", from_handle=f"dispatch:{dispatch_id}"),
    ]

    question = await value.answer(run_id, "msg-1", "restore the exclusion")

    assert question.lane == "issue-100"
    assert orca.replies == [("orca-run-1", "msg-1", "restore the exclusion")]
    recorded = [event for event in store.events(run_id) if event["kind"] == "supervisor_answered"]
    assert recorded == [
        {
            "kind": "supervisor_answered",
            "payload": {"message_id": "msg-1", "body": "restore the exclusion"},
        }
    ]


async def test_answering_an_unknown_message_sends_nothing(tmp_path: Path) -> None:
    """A typo must not direct an agent in some other run."""

    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    accepted = await value.accept(run_id)
    orca.messages_by_run["orca-run-1"] = [
        _message("msg-1", "question", from_handle=f"dispatch:{accepted.started[0].dispatch_id}"),
    ]

    with pytest.raises(ValueError, match="not an unanswered question"):
        await value.answer(run_id, "msg-typo", "body")

    assert orca.replies == []


async def test_an_already_answered_question_cannot_collect_a_second_answer(
    tmp_path: Path,
) -> None:
    """An agent reading a thread cannot tell which of two directions is current."""

    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    accepted = await value.accept(run_id)
    dispatch_id = accepted.started[0].dispatch_id
    orca.messages_by_run["orca-run-1"] = [
        _message("msg-1", "question", from_handle=f"dispatch:{dispatch_id}"),
        _message("msg-2", "status", from_handle="run:orca-run-1", thread_id="msg-1"),
    ]

    with pytest.raises(ValueError, match="not an unanswered question"):
        await value.answer(run_id, "msg-1", "second direction")

    assert orca.replies == []


async def test_questions_carries_the_body_the_monitor_line_only_counts(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    accepted = await value.accept(run_id)
    orca.messages_by_run["orca-run-1"] = [
        _message("msg-1", "question", from_handle=f"dispatch:{accepted.started[0].dispatch_id}"),
    ]

    pending = await value.questions(run_id)

    assert [(item.message_id, item.body) for item in pending] == [
        ("msg-1", "Choose fix or approve.")
    ]


async def test_monitor_reports_an_escalation_it_cannot_attribute(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    orca.messages_by_run["orca-run-1"] = [
        _message("msg-9", "escalation", from_handle="term_unknown"),
    ]

    result = await value.monitor(run_id)

    assert [(q.message_id, q.lane, q.role, q.dispatch_id) for q in result.questions] == [
        ("msg-9", None, None, None)
    ]


async def test_initial_review_normalizes_a_differently_computed_diff_digest(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    finding = review_finding_data()
    report = json.loads(initial_review_report_json(finding))
    report["review_revision"]["diff_sha256"] = "d" * 64
    report["findings"][0]["review_revision"]["diff_sha256"] = "d" * 64

    orca.complete_dispatched(json.dumps(report))
    result = await value.monitor(run_id)

    assert result.status == "active"
    assert [record.finding_id for record in result.findings] == ["finding-1"]
    assert store.findings(run_id)[0].contract.review_revision.diff_sha256 == "c" * 64


async def test_initial_review_rejects_a_diff_the_lane_checkout_cannot_reproduce(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    git.diff_sha256_override = "f" * 64
    orca.complete_dispatched(initial_review_report_json())

    result = await value.monitor(run_id)

    assert result.status == "failed"


async def test_initial_review_requires_the_clean_frozen_lane_head(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    git.lane_clean_override = False
    orca.complete_dispatched(initial_review_report_json())

    result = await value.monitor(run_id)

    assert result.status == "failed"


async def test_initial_review_rejects_lane_head_drift(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    git.lane_head_override = "9" * 40
    orca.complete_dispatched(initial_review_report_json())

    result = await value.monitor(run_id)

    assert result.status == "failed"


async def test_worker_revision_must_match_the_actual_lane_head(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.lane_head_override = "9" * 40
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    orca.complete_dispatched(worker_result())

    result = await value.monitor(run_id)

    assert result.status == "failed"
    assert all(stage.role is not StageKind.INITIAL_REVIEWER for stage in result.stages)


async def test_dirty_worker_checkout_cannot_freeze_a_review_revision(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.lane_clean_override = False
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    orca.complete_dispatched(worker_result())

    result = await value.monitor(run_id)

    assert result.status == "failed"


async def test_resolved_finding_converges_in_one_round(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched(fix_attempt())
    result = await value.monitor(run_id)
    assert [launch.role for launch in result.started] == [StageKind.RE_REVIEWER]
    orca.complete_dispatched(re_review("resolved"))
    result = await value.monitor(run_id)

    assert result.status == "complete"
    assert result.findings[0].phase is FindingPhase.RESOLVED
    assert result.findings[0].round == 1
    assert len(store.events(run_id)) > 10


async def test_still_open_uses_two_rounds_then_escalates(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("still_open"))
    result = await value.monitor(run_id)
    assert result.findings[0].round == 2
    assert result.started[0].role is StageKind.FIXER

    orca.complete_dispatched(fix_attempt(round=2))
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("still_open", round=2))
    result = await value.monitor(run_id)
    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].round == 2

    orca.complete_dispatched(escalation("rounds_exhausted", round=2))
    result = await value.monitor(run_id)
    assert result.status == "blocked"
    assert not any(stage.round == 3 for stage in result.stages)


async def test_capability_mismatch_uses_fallback_without_consuming_round(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched(fix_attempt(status="capability_mismatch"))
    result = await value.monitor(run_id)

    assert result.findings[0].round == 1
    assert result.started[0].role is StageKind.FIXER
    profile = orca.starts[-1]["profile"]
    assert isinstance(profile, AgentProfile)
    assert profile.model == "gpt-fallback"


async def test_scope_block_routes_to_escalation(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched(fix_attempt(status="blocked_scope"))
    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].escalation_reason == "scope_escape"


async def test_re_review_accepts_introduced_and_defers_unrelated_findings(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)

    introduced = review_finding_data(2)
    unrelated = review_finding_data(3)
    orca.complete_dispatched(
        re_review(
            "regression_introduced_by_fix",
            new_findings=[
                {"origin": "introduced_by_fix", "finding": introduced},
                {"origin": "unrelated", "finding": unrelated},
            ],
        )
    )
    result = await value.monitor(run_id)

    phases = {item.finding_id: item.phase for item in result.findings}
    assert phases == {
        "finding-1": FindingPhase.PENDING_COMPOSITE,
        "finding-2": FindingPhase.FIXING,
        "finding-3": FindingPhase.DEFERRED,
    }
    assert [launch.role for launch in result.started] == [StageKind.FIXER]


async def test_partial_re_review_discovery_replays_idempotently(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    introduced = review_finding_data(2)
    unrelated = review_finding_data(3)
    body = re_review(
        "regression_introduced_by_fix",
        new_findings=[
            {"origin": "introduced_by_fix", "finding": introduced},
            {"origin": "unrelated", "finding": unrelated},
        ],
    )
    orca.complete_dispatched(body)
    # A crash-replay finds what the supervisor persisted, which is the report
    # already restated on the revision it derived for the fixer commit.
    bound = {
        "base_sha": "b" * 40,
        "head_sha": fixer_sha("finding-1", 1),
        "diff_sha256": "c" * 64,
    }
    raw = ReReviewResult.model_validate_json(body)
    persisted = raw.model_copy(
        update={
            "reviewed_commit_sha": fixer_sha("finding-1", 1),
            "new_findings": [
                item.model_copy(
                    update={"finding": item.finding.model_copy(update={"review_revision": bound})}
                )
                for item in raw.new_findings
            ],
        }
    )
    original = store.findings(run_id)[0]
    stage = next(item for item in store.stages(run_id) if item.role is StageKind.RE_REVIEWER)
    store.record_re_review(run_id, original, stage, persisted)
    store.add_finding(
        run_id,
        original.lane_id,
        persisted.new_findings[0].finding,
        origin="introduced_by_fix",
    )

    result = await value.monitor(run_id)

    phases = {item.finding_id: item.phase for item in result.findings}
    assert phases["finding-1"] is FindingPhase.PENDING_COMPOSITE
    assert phases["finding-2"] is FindingPhase.FIXING
    assert phases["finding-3"] is FindingPhase.DEFERRED


@pytest.mark.parametrize(
    "discovered",
    [
        [review_finding_data(2, dependencies=["finding-999"])],
        [
            review_finding_data(2, dependencies=["finding-3"]),
            review_finding_data(3, dependencies=["finding-2"]),
        ],
    ],
)
async def test_invalid_introduced_dependency_graph_escalates(
    tmp_path: Path, discovered: list[dict[str, object]]
) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(
        re_review(
            "regression_introduced_by_fix",
            new_findings=[{"origin": "introduced_by_fix", "finding": item} for item in discovered],
        )
    )

    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].escalation_reason is FindingReason.AMBIGUOUS_RESULT


async def test_disjoint_finding_fanout_uses_isolated_worktrees(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca, graph_config=config(max_workers=2, max_fixers=2))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(
        value,
        orca,
        run_id,
        review_finding_data(1),
        review_finding_data(2),
        review_finding_data(3),
    )

    fixer_starts = [item for item in orca.starts if "finding-" in str(item["lane_name"])]
    assert len(fixer_starts) == 2
    assert len({str(item["worktree_id"]) for item in fixer_starts}) == 2
    assert all(item["parent_worktree_id"] == "repo::/tmp/issue" for item in fixer_starts)


async def test_overlapping_finding_scopes_are_serialized(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca, graph_config=config(max_workers=2, max_fixers=2))
    second = review_finding_data(2)
    scope = second["allowed_write_scope"]
    assert isinstance(scope, dict)
    scope["paths"] = ["src/file1.py"]
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data(1), second)

    fixer_starts = [item for item in orca.starts if "finding-" in str(item["lane_name"])]
    assert len(fixer_starts) == 1


async def test_out_of_scope_diff_is_rejected_and_escalated(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.changed_override = ["src/outside.py"]
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())

    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].escalation_reason is FindingReason.SCOPE_ESCAPE
    scope_event = next(
        item for item in store.events(run_id) if item["kind"] == "fixer_scope_checked"
    )
    payload = scope_event["payload"]
    assert isinstance(payload, dict)
    assert payload["actual_paths"] == ["src/outside.py"]


async def test_fixer_identity_is_taken_from_the_record_not_from_the_report(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    mistyped = json.loads(fix_attempt())
    mistyped["base_sha"] = "0" * 40
    mistyped["commit_sha"] = "1" * 40
    orca.complete_dispatched(json.dumps(mistyped))

    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.RE_REVIEWER
    attempt = store.latest_fix_attempt(store.findings(run_id)[0].finding_key)
    assert attempt is not None
    assert attempt.base_sha == "b" * 40
    assert attempt.commit_sha == fixer_sha("finding-1", 1)


async def test_adjudicator_may_omit_the_revision_of_a_revised_contract(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt(validation=[]))
    await value.monitor(run_id)
    revised = review_finding_data()
    del revised["review_revision"]
    revised["allowed_write_scope"] = {"paths": ["src/file1.py", "src/widened.py"], "symbols": []}
    orca.complete_dispatched(
        escalation("validation_failed", "approve_scope_revision", revised_finding=revised)
    )

    result = await value.monitor(run_id)

    finding = result.findings[0]
    assert finding.phase is FindingPhase.FIXING
    assert finding.round == 2
    contract = store.findings(run_id)[0].effective_contract
    assert contract.review_revision is not None
    assert contract.review_revision.head_sha == "b" * 40
    assert contract.allowed_write_scope.paths == ["src/file1.py", "src/widened.py"]


async def test_accepting_a_verified_fix_settles_the_finding_instead_of_refixing(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt(validation=[]))
    result = await value.monitor(run_id)
    assert result.findings[0].escalation_reason is FindingReason.VALIDATION_FAILED

    orca.complete_dispatched(escalation("validation_failed", "accept_fix"))
    result = await value.monitor(run_id)

    assert result.findings[0].phase is FindingPhase.RESOLVED
    assert store.integrations(run_id)[0].status == "integrated"


async def test_accepting_a_fix_that_was_never_committed_blocks(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt(status="capability_mismatch"))
    await value.monitor(run_id)
    orca.complete_dispatched(fix_attempt(status="capability_mismatch"))
    await value.monitor(run_id)
    orca.complete_dispatched(escalation("ambiguous_result", "accept_fix"))

    result = await value.monitor(run_id)

    assert result.findings[0].phase is FindingPhase.BLOCKED


async def test_approving_a_finding_unchanged_starts_the_next_fix_round(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt(validation=[]))
    await value.monitor(run_id)
    frozen = store.findings(run_id)[0].effective_contract
    orca.complete_dispatched(escalation("validation_failed", "approve_unchanged"))

    result = await value.monitor(run_id)

    finding = result.findings[0]
    assert finding.phase is FindingPhase.FIXING
    assert finding.round == 2
    assert store.findings(run_id)[0].effective_contract == frozen


async def test_fixer_head_off_its_assigned_base_is_rejected_before_re_review(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.head_override = "9" * 40
    git.ancestor_override = False
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())

    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].escalation_reason is FindingReason.AMBIGUOUS_RESULT


async def test_multi_commit_fixer_attempt_is_rejected_before_re_review(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.commit_count_override = 2
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())

    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].escalation_reason is FindingReason.AMBIGUOUS_RESULT


async def test_dirty_fixer_worktree_is_rejected_before_re_review(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.clean_override = False
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())

    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].escalation_reason is FindingReason.AMBIGUOUS_RESULT


async def test_an_in_flight_fixer_does_not_hold_up_an_unrelated_re_review(tmp_path: Path) -> None:
    """A lane is not a queue of one.

    Refusing every non-worker stage while any stage in the lane was active made
    the lane strictly serial. One fixer that takes an hour held up the
    adjudication of every finding that had already been fixed, and a lane with
    three settled findings sat idle waiting on a fourth it had nothing to do
    with.
    """

    orca = FakeOrca()
    value, store = controller(tmp_path, orca, graph_config=config(max_workers=3, max_fixers=2))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    second = review_finding_data(2)
    second["validation"] = [{"command": "pytest tests/test_two.py", "expected": "passes"}]
    await advance_to_fixer(value, orca, run_id, review_finding_data(1), second)

    first_fixer = next(
        item
        for item in orca.starts
        if "fixer" in str(item["lane_name"]) and "finding-1" in str(item["lane_name"])
    )
    orca.complete_task(
        str(first_fixer["task_id"]),
        fix_attempt(
            "finding-1",
            validation=[{"command": "pytest", "status": "passed", "output": "passed"}],
        ),
    )

    result = await value.monitor(run_id)

    # The second fixer is still out, and the first finding's re-review starts
    # anyway - it reads a frozen diff in its own worktree and owns nothing the
    # fixer touches.
    assert [item.role for item in result.started] == [StageKind.RE_REVIEWER], store.events(run_id)
    assert any(
        item.role is StageKind.FIXER and item.phase is StagePhase.DISPATCHED
        for item in store.stages(run_id)
    )


async def test_approved_commits_integrate_serially_and_are_auditable(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, store = controller(
        tmp_path, orca, graph_config=config(max_workers=2, max_fixers=2), git=git
    )
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    second = review_finding_data(2)
    second["validation"] = [{"command": "pytest tests/test_two.py", "expected": "passes"}]
    await advance_to_fixer(value, orca, run_id, review_finding_data(1), second)
    fixer_starts = [item for item in orca.starts if "fixer" in str(item["lane_name"])]
    for start in fixer_starts:
        finding_id = "finding-1" if "finding-1" in str(start["lane_name"]) else "finding-2"
        command = "pytest" if finding_id == "finding-1" else "pytest tests/test_two.py"
        orca.complete_task(
            str(start["task_id"]),
            fix_attempt(
                finding_id,
                validation=[{"command": command, "status": "passed", "output": "passed"}],
            ),
        )
    result = await value.monitor(run_id)
    # Each re-review reads a diff already frozen to an exact base and head, so
    # neither has a reason to wait on the other. Integration is what stays
    # serial, and the receipts below are what prove it.
    assert [item.role for item in result.started] == [
        StageKind.RE_REVIEWER,
        StageKind.RE_REVIEWER,
    ], store.events(run_id)

    for started in result.started:
        stage = next(item for item in store.stages(run_id) if item.orca_task_id == started.task_id)
        assert stage.finding_id is not None
        orca.complete_task(started.task_id, re_review("resolved", finding_id=stage.finding_id))

    result = await value.monitor(run_id)
    result = await value.monitor(run_id)

    assert result.status == "complete"
    receipts = store.integrations(run_id)
    assert [item.status for item in receipts] == ["integrated", "integrated"]
    assert len({item.integrated_sha for item in receipts}) == 2
    assert store.lanes(run_id)[0].integration_head_sha == receipts[-1].integrated_sha
    assert git.validation_calls[-1] == ["pytest", "pytest tests/test_two.py"]


async def test_introduced_regression_integrates_the_approved_composite_chain(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    original_commit = fixer_sha("finding-1", 1)
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    introduced = review_finding_data(2)
    revision = introduced["review_revision"]
    assert isinstance(revision, dict)
    revision["head_sha"] = original_commit
    introduced["validation"] = [{"command": "pytest tests/regression.py", "expected": "passes"}]
    orca.complete_dispatched(
        re_review(
            "regression_introduced_by_fix",
            new_findings=[{"origin": "introduced_by_fix", "finding": introduced}],
        )
    )
    result = await value.monitor(run_id)
    assert result.started[0].role is StageKind.FIXER
    corrected_commit = fixer_sha("finding-2", 1)
    git.commit_chain_override = [original_commit, corrected_commit]
    orca.complete_dispatched(
        fix_attempt(
            "finding-2",
            base_sha=original_commit,
            validation=[
                {"command": "pytest tests/regression.py", "status": "passed", "output": "passed"}
            ],
        )
    )
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved", finding_id="finding-2"))

    result = await value.monitor(run_id)

    assert result.status == "complete"
    receipt = store.integrations(run_id)[0]
    assert receipt.source_commits == [original_commit, corrected_commit]
    assert receipt.source_finding_ids == ["finding-1", "finding-2"]
    assert git.validation_calls[-1] == ["pytest", "pytest tests/regression.py"]


async def test_integrated_receipt_replay_settles_every_composite_finding(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    original_commit = fixer_sha("finding-1", 1)
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    introduced = review_finding_data(2)
    revision = introduced["review_revision"]
    assert isinstance(revision, dict)
    revision["head_sha"] = original_commit
    orca.complete_dispatched(
        re_review(
            "regression_introduced_by_fix",
            new_findings=[{"origin": "introduced_by_fix", "finding": introduced}],
        )
    )
    await value.monitor(run_id)
    corrected_commit = fixer_sha("finding-2", 1)
    orca.complete_dispatched(fix_attempt("finding-2", base_sha=original_commit))
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved", finding_id="finding-2"))
    current = next(item for item in store.findings(run_id) if item.finding_id == "finding-2")
    git.commit_chain_override = [original_commit, corrected_commit]
    store.begin_integration(
        run_id,
        current,
        fixer_commit_sha=corrected_commit,
        source_commits=[original_commit, corrected_commit],
        source_finding_ids=["finding-1", "finding-2"],
        base_sha="b" * 40,
    )
    store.finish_integration(
        run_id,
        current,
        status="integrated",
        integrated_sha="f" * 40,
        validation_results=[],
    )

    result = await value.monitor(run_id)

    assert result.status == "complete"
    assert all(item.phase is FindingPhase.RESOLVED for item in result.findings)


async def test_deferring_introduced_regression_defers_its_composite_predecessor(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    original_commit = fixer_sha("finding-1", 1)
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    introduced = review_finding_data(2)
    revision = introduced["review_revision"]
    assert isinstance(revision, dict)
    revision["head_sha"] = original_commit
    orca.complete_dispatched(
        re_review(
            "regression_introduced_by_fix",
            new_findings=[{"origin": "introduced_by_fix", "finding": introduced}],
        )
    )
    await value.monitor(run_id)
    orca.complete_dispatched(
        fix_attempt("finding-2", status="blocked_scope", base_sha=original_commit)
    )
    await value.monitor(run_id)
    orca.complete_dispatched(escalation("scope_escape", action="defer", finding_id="finding-2"))

    result = await value.monitor(run_id)

    assert result.status == "complete"
    assert {item.finding_id: item.phase for item in result.findings} == {
        "finding-1": FindingPhase.DEFERRED,
        "finding-2": FindingPhase.DEFERRED,
    }


async def test_integration_conflict_maps_to_the_finding(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.conflict = True
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))

    result = await value.monitor(run_id)

    assert result.findings[0].escalation_reason is FindingReason.INTEGRATION_CONFLICT
    assert store.integrations(run_id)[0].status == "conflict"


async def test_conflict_retry_relands_the_same_round_instead_of_spending_one(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.conflict = True
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))
    result = await value.monitor(run_id)
    assert result.findings[0].escalation_reason is FindingReason.INTEGRATION_CONFLICT

    orca.complete_dispatched(escalation("integration_conflict", "approve_unchanged"))
    result = await value.monitor(run_id)

    # The fix was approved on its merits, so the retry is a rebase at round 1
    # rather than the second and final round the ceiling would then close.
    assert result.findings[0].round == 1
    assert result.started[0].role is StageKind.FIXER
    assert any(":retry1" in stage.stage_key for stage in store.stages(run_id))


async def test_an_adjudicator_that_never_settles_a_finding_is_bounded(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt(validation=[]))
    result = await value.monitor(run_id)
    assert result.findings[0].escalation_reason is FindingReason.VALIDATION_FAILED

    # An adjudicator can approve the finding unchanged at the ceiling round and
    # leave it exactly where it started: same phase, same round, same trigger.
    # The retry suffix makes the next dispatch a fresh key, so without a bound
    # here the same adjudication runs again on every monitor pass forever.
    adjudications = 0
    for _ in range(12):
        if result.findings[0].phase is FindingPhase.BLOCKED:
            break
        orca.complete_dispatched(escalation("validation_failed", "approve_unchanged"))
        adjudications += 1
        result = await value.monitor(run_id)

    assert result.findings[0].phase is FindingPhase.BLOCKED
    assert adjudications <= 4
    escalation_stages = [
        stage
        for stage in store.stages(run_id)
        if stage.role is StageKind.ESCALATION and ":escalate:" in stage.stage_key
    ]
    assert len(escalation_stages) <= 4


async def test_a_second_acceptance_of_the_same_failed_validation_blocks(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.lane_validation_fails = True
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))
    result = await value.monitor(run_id)
    assert result.findings[0].escalation_reason is FindingReason.VALIDATION_FAILED

    # The head, the fix and the failing command are all the same on the second
    # look, so a second acceptance cannot be reading anything the first one
    # missed. Blocking costs an owner a glance; readjudicating costs a dispatch
    # every tick for as long as the run is monitored.
    adjudications = 0
    for _ in range(10):
        if result.findings[0].phase is FindingPhase.BLOCKED:
            break
        orca.complete_dispatched(escalation("validation_failed", "accept_fix"))
        adjudications += 1
        result = await value.monitor(run_id)

    assert result.findings[0].phase is FindingPhase.BLOCKED
    assert adjudications == 2


async def test_reopening_a_blocked_finding_does_not_refund_the_adjudication_budget(
    tmp_path: Path,
) -> None:
    """A reopen frees the stage key. It must not also free the verdict count.

    The reopen deletes every escalation row at or past the reopened round, which
    is what lets a re-adjudication record its own verdict under the same stage.
    While the bound was read from that table, the deletion also refunded it: run
    1f13dd37 reopened one finding three times and paid thirteen accept_fix
    adjudications against one unchanged failing command, because each reopen
    reset the count that was supposed to stop the second.
    """

    orca = FakeOrca()
    git = FakeGit()
    git.lane_validation_fails = True
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))
    result = await value.monitor(run_id)

    adjudications = 0
    for _ in range(10):
        if result.findings[0].phase is FindingPhase.BLOCKED:
            break
        orca.complete_dispatched(escalation("validation_failed", "accept_fix"))
        adjudications += 1
        result = await value.monitor(run_id)
    assert result.findings[0].phase is FindingPhase.BLOCKED
    assert adjudications == 2

    finding = result.findings[0]
    store.reopen_finding(
        run_id,
        finding.finding_id,
        phase=FindingPhase.PENDING_ESCALATION,
        round=finding.round,
        escalation_reason=FindingReason.VALIDATION_FAILED,
        note="owner looked and wants one more adjudication",
    )
    # One more look is what the reopen bought. A second would be the loop again.
    after = 0
    result = await value.monitor(run_id)
    for _ in range(10):
        if result.findings[0].phase is FindingPhase.BLOCKED:
            break
        orca.complete_dispatched(escalation("validation_failed", "accept_fix"))
        after += 1
        result = await value.monitor(run_id)

    assert result.findings[0].phase is FindingPhase.BLOCKED
    assert after <= 1


async def test_accepting_a_fix_rechecks_it_instead_of_replaying_the_stored_verdict(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.lane_validation_fails = True
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))
    result = await value.monitor(run_id)
    assert result.findings[0].escalation_reason is FindingReason.VALIDATION_FAILED
    assert store.integrations(run_id)[0].status == "validation_failed"

    # The stored failure was a fact about one check against one head, and the
    # check has since been repaired. An acceptance has to reach Git again to find
    # that out. Replaying the receipt instead made accept_fix a no-op, which is
    # how a correct fix escalated on unrunnable output until it blocked.
    git.lane_validation_fails = False
    orca.complete_dispatched(escalation("validation_failed", "accept_fix"))
    result = await value.monitor(run_id)

    assert store.integrations(run_id)[0].status == "integrated"
    assert result.findings[0].phase is not FindingPhase.BLOCKED


async def test_a_stage_whose_worker_died_is_dispatched_again(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    first = [stage for stage in store.stages(run_id) if stage.orca_dispatch_id is not None]
    assert first

    # Killing the supervisor while its workers are live leaves every dispatched
    # Task back at ready with no result. The stage still holds the Dispatch id
    # that proved a worker started, and nothing else clears it, so without this
    # the run can never start that stage again.
    orca.abandon_dispatched()
    result = await value.monitor(run_id)

    assert [launch.role for launch in result.started] == [StageKind.WORKER]
    restarted = [stage for stage in store.stages(run_id) if stage.orca_dispatch_id is not None]
    assert restarted


async def test_a_stage_that_committed_before_dying_is_not_dispatched_again(
    tmp_path: Path,
) -> None:
    """Releasing is recovery only when there is nothing to recover.

    `application-lifecycle-kas-580` is the case this exists for. The worker did
    the whole job, opened the pull request that was merged three hours later,
    and died before reporting. The stage was released, a fresh agent went into a
    worktree that already held the finished work, found nothing to do, returned
    no result, and the lane failed. Re-dispatching cost that run everything and
    recovered nothing.
    """

    git = FakeGit()
    orca = FakeOrca()
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    worker = next(stage for stage in store.stages(run_id) if stage.role is StageKind.WORKER)
    dispatch_id = worker.orca_dispatch_id
    assert dispatch_id is not None
    assert worker.worktree_id is not None
    # The baseline is read at start, before the agent has touched anything.
    assert worker.start_head_sha == git.heads[worker.worktree_id]

    # What the dead worker left behind: a commit, and no result.
    git.heads[worker.worktree_id] = "9" * 40
    orca.abandon_dispatched()
    result = await value.monitor(run_id)

    assert result.started == []
    held = next(stage for stage in store.stages(run_id) if stage.role is StageKind.WORKER)
    assert held.orca_dispatch_id == dispatch_id
    assert held.result_json is None
    events = [item for item in store.events(run_id) if item["kind"] == "stage_unreported_work_held"]
    assert len(events) == 1
    assert events[0]["payload"]["dispatch_id"] == dispatch_id

    # Every tick re-observes the same ready Task. Saying so once is a record;
    # saying so forever buries the rest of the run's history.
    await value.monitor(run_id)

    repeated = [
        item for item in store.events(run_id) if item["kind"] == "stage_unreported_work_held"
    ]
    assert len(repeated) == 1


async def test_a_stage_that_reported_keeps_its_dispatch(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    orca.complete_dispatched()
    await value.monitor(run_id)

    # A completed stage is settled by its result. Releasing its dispatch would
    # offer finished work back to the scheduler.
    worker = next(stage for stage in store.stages(run_id) if stage.role is StageKind.WORKER)
    assert worker.orca_dispatch_id is not None


async def test_restart_recovers_cherry_pick_before_receipt_settlement(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.crash_after_cherry_pick = True
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))

    with pytest.raises(RuntimeError, match="crash after cherry-pick"):
        await value.monitor(run_id)

    restarted = ExecutionController(
        config=config(), orca=orca, store=store, git=git, publisher=FakePublisher()
    )
    result = await restarted.monitor(run_id)
    assert result.status == "complete"
    assert store.integrations(run_id)[0].status == "integrated"


async def test_restart_aborts_partial_composite_then_retries_from_reserved_base(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.crash_mid_sequence = True
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))

    with pytest.raises(RuntimeError, match="crash mid cherry-pick sequence"):
        await value.monitor(run_id)
    assert git.in_progress

    restarted = ExecutionController(
        config=config(), orca=orca, store=store, git=git, publisher=FakePublisher()
    )
    result = await restarted.monitor(run_id)

    assert result.status == "complete"
    assert not git.in_progress
    assert store.integrations(run_id)[0].status == "integrated"


async def test_restart_does_not_abort_an_unrelated_cherry_pick(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.crash_mid_sequence = True
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved"))
    with pytest.raises(RuntimeError, match="crash mid cherry-pick sequence"):
        await value.monitor(run_id)
    git.active_sequence_commits = ["9" * 40]

    restarted = ExecutionController(
        config=config(), orca=orca, store=store, git=git, publisher=FakePublisher()
    )
    result = await restarted.monitor(run_id)

    assert result.findings[0].escalation_reason is FindingReason.INTEGRATION_CONFLICT
    assert git.abort_calls == 0
    assert git.in_progress
    assert store.integrations(run_id)[0].status == "conflict"


async def test_restart_reconciliation_does_not_duplicate_tasks_or_receipts(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    task_count = len(orca.tasks_by_id)
    event_count = len(store.events(run_id))

    restarted = ExecutionController(
        config=config(), orca=orca, store=store, git=FakeGit(), publisher=FakePublisher()
    )
    await restarted.monitor(run_id)
    await restarted.monitor(run_id)

    assert len(orca.tasks_by_id) == task_count
    assert len(store.events(run_id)) == event_count


async def test_restart_recovers_task_created_before_local_binding(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    orca.fail_after_task_create = True

    with pytest.raises(RuntimeError, match="remote task creation"):
        await value.accept(run_id)

    restarted = ExecutionController(
        config=config(), orca=orca, store=store, git=FakeGit(), publisher=FakePublisher()
    )
    result = await restarted.accept(run_id)
    assert len(orca.tasks_by_id) == 1
    assert len(result.started) == 1


async def test_restart_recovers_run_created_before_local_binding(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    orca.fail_after_run_create = True

    with pytest.raises(RuntimeError, match="remote run creation"):
        await value.accept(run_id)

    restarted = ExecutionController(
        config=config(), orca=orca, store=store, git=FakeGit(), publisher=FakePublisher()
    )
    result = await restarted.accept(run_id)
    assert len(orca.runs_by_id) == 1
    assert result.orca_run_id == "orca-run-1"


async def test_one_stage_that_cannot_start_does_not_park_the_rest_of_the_wave(
    tmp_path: Path,
) -> None:
    """A refused start belongs to its own stage, not to every stage behind it.

    `_start_ready` walks the whole ready set in one pass, so before this the
    first Orca refusal raised out of that loop and nothing after it was even
    reached. The lane that failed then sat in STARTING holding a worker slot,
    and the run read as idle. That is the shape of an overnight stall.
    """

    orca = FakeOrca()
    orca.refuse_starts.add("task-1")
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal(lane_count=2)).run_id

    result = await value.accept(run_id)

    assert [launch.task_id for launch in result.started] == ["task-2"]
    stages = {stage.orca_task_id: stage for stage in store.stages(run_id)}
    # Refused, so the slot goes back and the stage is tried again next tick.
    assert stages["task-1"].phase is StagePhase.READY
    assert stages["task-2"].phase is StagePhase.DISPATCHED
    failures = [
        event["payload"] for event in store.events(run_id) if event["kind"] == "stage_start_failed"
    ]
    # One event per attempt, and every one of them handed the slot back.
    assert failures and all(failure["released"] is True for failure in failures)
    assert "task_not_startable" in str(failures[0]["detail"])


async def test_a_timed_out_start_keeps_its_reservation_and_is_adopted_next_tick(
    tmp_path: Path,
) -> None:
    """A timeout is not a failure. It is the absence of an answer.

    `worker-start` can create the Dispatch and lose the reply, so releasing the
    reservation on a timeout races reconciliation - which is already the thing
    that looks a STARTING stage's Dispatch up and either adopts it or frees the
    slot. Leave the stage alone and let that decide.
    """

    orca = FakeOrca()
    orca.timeout_starts.add("task-1")
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal(lane_count=2)).run_id

    result = await value.accept(run_id)

    assert [launch.task_id for launch in result.started] == ["task-2"]
    stages = {stage.orca_task_id: stage for stage in store.stages(run_id)}
    assert stages["task-1"].phase is StagePhase.STARTING
    assert stages["task-1"].orca_dispatch_id is None
    failures = [
        event["payload"] for event in store.events(run_id) if event["kind"] == "stage_start_failed"
    ]
    assert [failure["released"] for failure in failures] == [False]

    await value.monitor(run_id)

    adopted = {stage.orca_task_id: stage for stage in store.stages(run_id)}["task-1"]
    assert adopted.phase is StagePhase.DISPATCHED
    assert adopted.orca_dispatch_id == "dispatch-1"
    # Adopted, never started twice.
    assert [start["task_id"] for start in orca.starts] == ["task-1", "task-2"]


async def test_a_blocked_lane_does_not_stop_a_lane_that_is_still_working(
    tmp_path: Path,
) -> None:
    """One lane's block is one lane's problem.

    `assistant-kas-576` blocked on a required-check query it made one second
    after pushing its branch, and that took the whole run terminal - stopping
    `ui-kas-564` and `application-lifecycle-kas-580`, neither of which had
    anything to do with that pull request.
    """

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal(lane_count=2)).run_id
    await value.accept(run_id)

    blocked, working = store.lanes(run_id)
    store.block_lane(run_id, blocked.lane_id, "GitHub required-check query failed")

    result = await value.monitor(run_id)

    assert result.status == "active"
    phases = {lane.name: lane.phase for lane in result.lanes}
    assert phases[blocked.name] is LanePhase.BLOCKED
    assert phases[working.name] is LanePhase.ACTIVE
    # And the run row itself was never written terminal.
    assert store.run(run_id).status == "active"


async def test_resuming_a_blocked_lane_clears_the_block_and_the_run_status(
    tmp_path: Path,
) -> None:
    """`reopen` and `settle` act on findings, and this lane has none left."""

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    lane = store.lanes(run_id)[0]
    store.block_lane(run_id, lane.lane_id, "the authorized lane pull request is no longer open")
    store.set_terminal_status(run_id, "blocked")

    resumed = value.resume(run_id, None, "owner merged it on purpose")

    assert [item.name for item in resumed] == [lane.name]
    assert store.lanes(run_id)[0].phase is LanePhase.ACTIVE
    # Half a recovery is not one: a run row left `blocked` reads as stopped.
    assert store.run(run_id).status == "active"
    notes = [
        event["payload"]
        for event in store.events(run_id)
        if event["kind"] == "supervisor_resumed_lane"
    ]
    assert notes == [{"lane": lane.name, "note": "owner merged it on purpose"}]


async def test_resuming_names_the_lane_when_that_lane_is_not_blocked(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal(lane_count=2)).run_id
    await value.accept(run_id)
    blocked, working = store.lanes(run_id)
    store.block_lane(run_id, blocked.lane_id, "blocked")

    with pytest.raises(ValueError, match=f"lane {working.name} of run .* no blocked lane"):
        value.resume(run_id, working.name, "note")

    # The one that is blocked still resumes by name, and only that one.
    assert [item.name for item in value.resume(run_id, blocked.name, "note")] == [blocked.name]


async def test_resuming_a_run_with_nothing_blocked_is_refused(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)

    with pytest.raises(ValueError, match="no blocked lane to resume"):
        value.resume(run_id, None, "note")


async def test_restart_recovers_dispatch_started_before_local_binding(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    orca.fail_after_worker_start = True

    with pytest.raises(RuntimeError, match="remote worker start"):
        await value.accept(run_id)

    restarted = ExecutionController(
        config=config(), orca=orca, store=store, git=FakeGit(), publisher=FakePublisher()
    )
    result = await restarted.monitor(run_id)
    assert len(orca.starts) == 1
    assert result.stages[0].orca_dispatch_id == "dispatch-1"


async def test_global_worker_limit_counts_other_active_runs(tmp_path: Path) -> None:
    orca = FakeOrca()
    graph = config(max_workers=1, max_fixers=1)
    value, store = controller(tmp_path, orca, graph_config=graph)
    other_run = store.record_proposal(proposal())
    other_stage = store.stages(other_run)[0]
    store.mark_accepted(other_run, "orca-other")
    store.bind_stage_task(other_run, other_stage.stage_id, "task-other")
    store.mark_stage_started(
        other_run,
        other_stage.stage_id,
        "dispatch-other",
        "repo::/tmp/other",
        {"ok": True},
    )

    run_id = value.propose(proposal()).run_id
    result = await value.accept(run_id)

    assert result.started == []
    assert store.active_worker_count() == 1


async def test_monitor_reclaims_an_abandoned_reservation_from_another_run(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    graph = config(max_workers=1, max_fixers=1)
    value, store = controller(tmp_path, orca, graph_config=graph)
    abandoned = store.record_proposal(proposal())
    abandoned_stage = store.stages(abandoned)[0]
    store.mark_accepted(abandoned, "orca-abandoned")
    store.bind_stage_task(abandoned, abandoned_stage.stage_id, "task-abandoned")
    assert store.reserve_stage_start(
        abandoned, abandoned_stage, max_workers=1, max_lanes=1, max_lane_fixers=1
    )

    run_id = value.propose(proposal()).run_id
    result = await value.accept(run_id)

    assert [launch.role for launch in result.started] == [StageKind.WORKER]
    assert store.stages(abandoned)[0].phase is StagePhase.READY


async def test_monitor_keeps_a_reservation_whose_dispatch_cannot_be_read(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    graph = config(max_workers=1, max_fixers=1)
    value, store = controller(tmp_path, orca, graph_config=graph)
    abandoned = store.record_proposal(proposal())
    abandoned_stage = store.stages(abandoned)[0]
    store.mark_accepted(abandoned, "orca-abandoned")
    store.bind_stage_task(abandoned, abandoned_stage.stage_id, "task-abandoned")
    assert store.reserve_stage_start(
        abandoned, abandoned_stage, max_workers=1, max_lanes=1, max_lane_fixers=1
    )
    orca.unreadable_dispatches.add("task-abandoned")

    run_id = value.propose(proposal()).run_id
    result = await value.accept(run_id)

    assert result.started == []
    assert store.stages(abandoned)[0].phase is StagePhase.STARTING


async def test_invalid_fixer_result_stops_for_ambiguous_escalation(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched("not-json")
    result = await value.monitor(run_id)

    assert result.started[0].role is StageKind.ESCALATION
    assert result.findings[0].escalation_reason == "ambiguous_result"


async def test_parallel_lane_limit_holds_second_lane(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca, graph_config=config(max_parallel_lanes=1))
    run_id = value.propose(proposal(lane_count=2)).run_id
    result = await value.accept(run_id)
    assert [launch.lane for launch in result.started] == ["issue-100"]

    orca.complete_dispatched()
    result = await value.monitor(run_id)
    assert [launch.lane for launch in result.started] == ["issue-101"]
    orca.complete_dispatched()
    result = await value.monitor(run_id)
    assert [launch.lane for launch in result.started] == ["issue-100"]
    orca.complete_dispatched(initial_review_report_json())
    result = await value.monitor(run_id)
    assert result.started[0].lane == "issue-101"


async def test_accept_rejects_non_lane_plan(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal(next_action="needs_owner")).run_id
    with pytest.raises(ValueError, match="no executable lanes"):
        await value.accept(run_id)


async def test_acceptance_freezes_authorization_before_publication(tmp_path: Path) -> None:
    orca = FakeOrca()
    publisher = FakePublisher(["pending"])
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    assert publisher.publish_calls == []
    assert store.acceptance_authorization(run_id) is None

    await value.accept(run_id)

    authorization = store.acceptance_authorization(run_id)
    assert authorization is not None
    assert len(authorization.proposal_sha256) == 64
    assert len(authorization.config_sha256) == 64
    assert publisher.publish_calls == []


async def test_resumed_run_rejects_changed_graph_policy(tmp_path: Path) -> None:
    orca = FakeOrca()
    publisher = FakePublisher()
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    changed = ExecutionController(
        config=config(max_workers=3),
        orca=orca,
        store=store,
        git=FakeGit(),
        publisher=publisher,
    )

    with pytest.raises(ValueError, match="policy changed after acceptance"):
        await changed.monitor(run_id)
    with pytest.raises(ValueError, match="policy changed after acceptance"):
        await changed.accept(run_id)

    assert publisher.publish_calls == []


async def test_reauthorizing_a_policy_change_lets_the_same_run_continue(
    tmp_path: Path,
) -> None:
    """A config edit mid-run must be recoverable without discarding the run.

    Everything the run has done is still valid: the same lanes, the same frozen
    findings, the same worktrees. Only the policy the next stage will run under
    moved. Telling the owner to record a new proposal throws all of that away,
    and what people did instead was edit the authorization row by hand.
    """

    orca = FakeOrca()
    publisher = FakePublisher()
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    before = store.acceptance_authorization(run_id)
    assert before is not None

    changed = ExecutionController(
        config=config(max_workers=3),
        orca=orca,
        store=store,
        git=FakeGit(),
        publisher=publisher,
    )
    with pytest.raises(ValueError, match="policy changed after acceptance"):
        await changed.monitor(run_id)

    # Reading the change is separate from making it, so a preview leaves the
    # run exactly as broken as it was.
    preview = changed.reauthorize(run_id, "", apply=False)
    assert preview.applied is False
    assert [(item.path, item.before, item.after) for item in preview.changes] == [
        ("max_parallel_workers", "4", "3")
    ]
    with pytest.raises(ValueError, match="policy changed after acceptance"):
        await changed.monitor(run_id)

    result = changed.reauthorize(run_id, "raised max_workers on purpose")
    after = result.authorization

    assert result.applied is True
    assert result.comparable is True
    assert [item.path for item in result.changes] == ["max_parallel_workers"]
    assert after.config_sha256 != before.config_sha256
    assert after.proposal_sha256 == before.proposal_sha256
    await changed.monitor(run_id)
    # The original controller is now the one out of date, in the same way.
    with pytest.raises(ValueError, match="policy changed after acceptance"):
        await value.monitor(run_id)


async def test_reauthorizing_refuses_when_the_proposal_itself_changed(
    tmp_path: Path,
) -> None:
    """A changed plan is not a changed policy, and must not be waved through."""

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    authorization = store.acceptance_authorization(run_id)
    assert authorization is not None

    with pytest.raises(ValueError, match="proposal itself changed"):
        store.reauthorize_acceptance(
            run_id,
            authorization.model_copy(update={"proposal_sha256": "0" * 64}),
            "different lanes",
        )

    assert store.acceptance_authorization(run_id) == authorization


async def test_reauthorizing_an_unaccepted_run_is_a_missing_authorization(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id

    with pytest.raises(KeyError, match="no frozen acceptance authorization"):
        value.reauthorize(run_id, "nothing to re-freeze yet")


async def test_publication_is_idempotent_and_exact_pending_head_stays_active(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    publisher = FakePublisher(["pending"])
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json())

    first = await value.monitor(run_id)
    second = await value.monitor(run_id)

    assert first.status == second.status == "active"
    assert publisher.publish_calls == [("b" * 40, None)]
    assert store.publications(run_id)[0].draft
    assert store.ci_receipts(run_id)[0].head_sha == "b" * 40


async def test_ci_failure_creates_scoped_finding_then_republishes_fix(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    publisher = FakePublisher(["failed", "passed"])
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json())

    failed = await value.monitor(run_id)
    assert [item.role for item in failed.started] == [StageKind.FIXER]
    finding = next(item for item in failed.findings if item.origin == "ci_failure")
    assert finding.effective_contract.allowed_write_scope.paths == ["src/file1.py"]
    assert store.ci_failures(run_id)[0].published_sha == "b" * 40

    orca.complete_dispatched(fix_attempt(finding.finding_id, base_sha="b" * 40))
    passed = await value.monitor(run_id)

    assert passed.status == "complete", store.events(run_id)
    assert len(publisher.publish_calls) == 2
    assert publisher.publish_calls[1][1] == "b" * 40
    assert store.publications(run_id)[-1].draft is False


async def test_a_complete_lane_is_never_touched_by_publication_again(tmp_path: Path) -> None:
    """A lane that has succeeded must not be able to fail on a later tick.

    Publication used to skip only blocked and failed lanes, so a complete lane
    was re-marked-ready every tick. ``mark_ready`` re-verifies that the pull
    request is still OPEN, so an owner merging it - the outcome the lane was
    working toward - turned the next tick into "the authorized lane pull request
    is no longer open", blocking the lane and taking the whole run terminal with
    every other lane still working.
    """

    class MergedAfterReady(FakePublisher):
        async def mark_ready(self, receipt: PublicationReceipt) -> PublicationReceipt:
            if self.ready_calls:
                raise PublicationError("the authorized lane pull request is no longer open")
            return await super().mark_ready(receipt)

    orca = FakeOrca()
    publisher = MergedAfterReady()
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json())

    first = await value.monitor(run_id)
    assert first.status == "complete", store.events(run_id)

    second = await value.monitor(run_id)

    assert second.status == "complete"
    assert publisher.ready_calls == ["b" * 40]
    assert store.lanes(run_id)[0].phase is LanePhase.COMPLETE


async def test_stale_ci_sha_and_publication_errors_block_the_lane_once_they_persist(
    tmp_path: Path,
) -> None:
    class StalePublisher(FakePublisher):
        async def checks(self, receipt: PublicationReceipt) -> CiReceipt:
            return CiReceipt(provider="github", head_sha="d" * 40, status="passed", checks=[])

    class BrokenPublisher(FakePublisher):
        async def publish(
            self,
            *,
            run_id: str,
            lane: LaneRecord,
            head_sha: str,
            previous: PublicationReceipt | None,
        ) -> PublicationReceipt:
            raise PublicationError("authentication failed")

    for publisher in (StalePublisher(), BrokenPublisher()):
        orca = FakeOrca()
        value, store = controller(tmp_path / type(publisher).__name__, orca, publisher=publisher)
        run_id = value.propose(proposal()).run_id
        await value.accept(run_id)
        await advance_to_initial_review(value, orca, run_id)
        orca.complete_dispatched(initial_review_report_json())
        first = await value.monitor(run_id)
        assert first.status == "active"

        await value.monitor(run_id)
        result = await value.monitor(run_id)

        assert result.status == "blocked"
        blocked = [item for item in store.events(run_id) if item["kind"] == "lane_blocked"]
        assert blocked
        payload = blocked[-1]["payload"]
        assert isinstance(payload, dict)
        assert "unchanged over 3 publication passes" in payload["reason"]


async def test_a_remote_that_has_not_answered_yet_is_not_the_lanes_failure(
    tmp_path: Path,
) -> None:
    """`assistant-kas-576` blocked one second after pushing its branch.

    The required-check query ran before GitHub had registered the workflow for
    that head, so it raised, and the lane was blocked on a run whose checks were
    all green by the time anybody looked. Nothing about that first answer was
    knowledge. Give the remote another pass before calling it a failure.
    """

    class SlowToAnswer(FakePublisher):
        def __init__(self) -> None:
            super().__init__()
            self.queries = 0

        async def checks(self, receipt: PublicationReceipt) -> CiReceipt:
            self.queries += 1
            if self.queries == 1:
                raise PublicationError(f"no required check has reported for {receipt.head_sha}")
            return await super().checks(receipt)

    orca = FakeOrca()
    publisher = SlowToAnswer()
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json())

    first = await value.monitor(run_id)

    assert first.status == "active"
    assert store.lanes(run_id)[0].phase is not LanePhase.BLOCKED
    errors = [item for item in store.events(run_id) if item["kind"] == "lane_publication_error"]
    assert len(errors) == 1

    second = await value.monitor(run_id)

    # And the lane finishes on the answer it was always going to get.
    assert second.status == "complete", store.events(run_id)
    assert store.publications(run_id)[-1].draft is False
    assert [item["kind"] for item in store.events(run_id)].count("lane_blocked") == 0


async def test_ci_fix_rounds_stop_after_two_failed_republished_heads(tmp_path: Path) -> None:
    orca = FakeOrca()
    publisher = FakePublisher(["failed", "failed", "failed"])
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json())
    first = await value.monitor(run_id)
    first_finding = next(item for item in first.findings if item.origin == "ci_failure")

    orca.complete_dispatched(fix_attempt(first_finding.finding_id, base_sha="b" * 40))
    second = await value.monitor(run_id)
    second_finding = next(
        item
        for item in second.findings
        if item.origin == "ci_failure" and item.finding_id != first_finding.finding_id
    )
    second_base = second.lanes[0].integration_head_sha
    assert second_base is not None
    orca.complete_dispatched(
        fix_attempt(
            second_finding.finding_id,
            base_sha=second_base,
            changed_path="src/file1.py",
        )
    )

    result = await value.monitor(run_id)

    assert result.status == "blocked"
    assert len(store.ci_failures(run_id)) == 2
    assert len(publisher.publish_calls) == 3


async def test_blocked_worker_escalates_instead_of_starting_a_review(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)

    orca.complete_dispatched(worker_blocked())
    result = await value.monitor(run_id)

    assert [finding.origin for finding in result.findings] == ["worker_blocked"]
    assert result.findings[0].phase is FindingPhase.ESCALATING
    assert result.findings[0].escalation_reason is FindingReason.WORKER_DECISION
    assert [launch.role for launch in result.started] == [StageKind.ESCALATION]
    assert StageKind.INITIAL_REVIEWER not in {stage.role for stage in result.stages}
    assert store.lanes(run_id)[0].review_head_sha is None


async def test_blocked_worker_decision_reaches_the_adjudicator_verbatim(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    orca.complete_dispatched(worker_blocked(question="Which document may be uploaded?"))

    result = await value.monitor(run_id)

    contract = result.findings[0].effective_contract
    assert contract.required_outcome.startswith("Which document may be uploaded?")
    assert contract.allowed_write_scope.paths == ["src/file1.py"]
    spec = orca.tasks_by_id[result.started[0].task_id]["spec"]
    assert "Which document may be uploaded?" in str(spec)


async def test_blocked_worker_must_leave_a_clean_checkout(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.lane_clean_override = False
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)

    orca.complete_dispatched(worker_blocked())
    result = await value.monitor(run_id)

    assert result.status == "failed"
    assert result.findings == []


async def test_blocked_worker_head_must_match_its_lane(tmp_path: Path) -> None:
    orca = FakeOrca()
    git = FakeGit()
    git.lane_head_override = "9" * 40
    value, _ = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)

    orca.complete_dispatched(worker_blocked())
    result = await value.monitor(run_id)

    assert result.status == "failed"


async def test_adjudicated_worker_decision_resumes_as_a_bounded_fixer(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    orca.complete_dispatched(worker_blocked())
    await value.monitor(run_id)
    revised = review_finding_data()
    revised["id"] = "worker-decision-1"
    revised["review_revision"] = {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "diff_sha256": "c" * 64,
    }
    orca.complete_dispatched(
        json.dumps(
            {
                "finding_id": "worker-decision-1",
                "round": 1,
                "reason": "worker_decision",
                "action": "approve_scope_revision",
                "rationale": "Fix all four.",
                "revised_finding": revised,
            }
        )
    )

    result = await value.monitor(run_id)

    assert [launch.role for launch in result.started] == [StageKind.FIXER]
    assert result.findings[0].phase is FindingPhase.FIXING
    assert result.findings[0].round == 2


async def test_fixer_missing_a_required_validation_escalates(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched(fix_attempt(validation=[]))
    result = await value.monitor(run_id)

    assert result.findings[0].escalation_reason is FindingReason.VALIDATION_FAILED
    assert [launch.role for launch in result.started] == [StageKind.ESCALATION]


async def test_fixer_reporting_a_failed_required_validation_escalates(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched(
        fix_attempt(validation=[{"command": "pytest", "status": "failed", "output": "1 failed"}])
    )
    result = await value.monitor(run_id)

    assert result.findings[0].escalation_reason is FindingReason.VALIDATION_FAILED


async def test_fixer_may_add_its_own_proof_beyond_the_contract(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, _ = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())

    orca.complete_dispatched(
        fix_attempt(
            validation=[
                {"command": "pytest", "status": "passed", "output": "passed"},
                {"command": "pytest tests/test_one.py", "status": "passed", "output": "passed"},
            ]
        )
    )
    result = await value.monitor(run_id)

    assert result.findings[0].escalation_reason is None
    assert [launch.role for launch in result.started] == [StageKind.RE_REVIEWER]


async def test_an_adjudicated_retry_at_the_ceiling_is_granted_once(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    orca.complete_dispatched(fix_attempt(validation=[]))
    await value.monitor(run_id)
    orca.complete_dispatched(escalation("validation_failed", "approve_unchanged"))
    result = await value.monitor(run_id)
    assert result.findings[0].round == 2

    orca.complete_dispatched(fix_attempt(round=2, validation=[]))
    await value.monitor(run_id)
    orca.complete_dispatched(escalation("validation_failed", "approve_unchanged", round=2))
    result = await value.monitor(run_id)

    # The adjudicator read the head itself and asked for one more attempt, so the
    # ceiling round is re-run rather than closed over verified work.
    assert result.findings[0].phase is FindingPhase.FIXING
    assert result.findings[0].round == 2
    assert any(":retry1" in stage.stage_key for stage in store.stages(run_id))

    orca.complete_dispatched(fix_attempt(round=2, validation=[]))
    await value.monitor(run_id)
    orca.complete_dispatched(escalation("validation_failed", "approve_unchanged", round=2))
    result = await value.monitor(run_id)

    # A second grant would make the ceiling mean nothing, so the finding blocks.
    assert result.findings[0].phase is FindingPhase.BLOCKED


async def test_a_review_requiring_shell_syntax_is_rejected_at_the_reviewer(
    tmp_path: Path,
) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    finding = review_finding_data()
    finding["validation"] = [{"command": "cd app/ui && npx tsc -b", "expected": "exit 0"}]
    orca.complete_dispatched(initial_review_report_json(finding))

    result = await value.monitor(run_id)

    # The runner has no shell, so this contract could only ever fail, and the
    # fix loop cannot tell a broken command from a broken fix. Say so while the
    # reviewer is still the one who can restate it.
    assert not result.findings
    rejected = [event for event in store.events(run_id) if event["kind"] == "stage_result_rejected"]
    assert any("without a shell" in str(event["payload"]) for event in rejected)


def test_a_reopened_stage_no_longer_counts_toward_its_family() -> None:
    base = "lane:finding-1:escalate:2:validation_failed"
    settled = {
        f"{base}:reopened20260822123245",
        f"{base}:retry1:reopened20260822123245",
        f"{base}:retry2:reopened20260822123245",
    }

    # A reopen renames a stage by appending, so a prefix match still saw all
    # three and handed the finding an ordinal it had never reached - which the
    # escalation ceiling then read as a loop and blocked on the next tick.
    assert _next_key(settled, base) == base
    assert _next_key(settled | {base}, base) == f"{base}:retry1"


async def test_a_conflict_retry_is_rebuilt_on_the_lane_head_it_has_to_land_on(
    tmp_path: Path,
) -> None:
    """A rebase is the supervisor's job, because no adjudicator can do it.

    `finding-escape-drops-search-drilldown-with-job-sheet` was dispatched three
    times at round 2, every time from `0706fc6c`, while the lane head sat at
    `f1c5dbd8`. The fix was correct - it cherry-picked cleanly by hand - and it
    conflicted identically on every attempt because it was rebuilt on the base
    that had just conflicted. Re-adjudicating that forever cannot help: the
    supervisor knows both shas and Git is the only thing that can settle it.
    """

    orca = FakeOrca()
    git = FakeGit()
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data(), review_finding_data(2))
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    # The first fix lands and moves the lane head; the second then conflicts on
    # the head it was never built against. That is the whole defect in one pass.
    git.conflict_commits = {fixer_sha("finding-2", 1)}
    orca.complete_dispatched(re_review("resolved"))
    await value.monitor(run_id)

    lane_head = store.lanes(run_id)[0].integration_head_sha
    assert lane_head is not None and lane_head != "b" * 40, "the first fix should have landed"
    conflicted = next(
        item for item in store.findings(run_id) if item.phase is not FindingPhase.RESOLVED
    )
    assert conflicted.escalation_reason is FindingReason.INTEGRATION_CONFLICT
    # It was dispatched from the review revision, which is now behind the lane.
    assert conflicted.dispatch_base_sha is None

    orca.complete_dispatched(
        escalation("integration_conflict", "approve_unchanged", finding_id=conflicted.finding_id)
    )
    result = await value.monitor(run_id)

    retried = next(item for item in result.findings if item.finding_id == conflicted.finding_id)
    assert retried.dispatch_base_sha == lane_head
    assert retried.phase is FindingPhase.FIXING
    # And the fixer's checkout is cut from that head, not from the frozen review.
    retry = next(
        stage
        for stage in store.stages(run_id)
        if stage.finding_id == conflicted.finding_id
        and stage.role is StageKind.FIXER
        and ":retry" in stage.stage_key
    )
    assert retry.phase is not StagePhase.PENDING


async def test_a_rebased_retry_lands_and_the_round_slot_follows_it(tmp_path: Path) -> None:
    """The rebase is only worth doing if the ledgers let the retry finish.

    A conflict retry stays in its own round on purpose, and both per-round
    ledgers keyed on that round refused the retry for producing a different
    commit - which is exactly what it was sent to do. The integration slot is
    re-pointed at the commit that is now current, and the re-review guard asks
    the stage rather than the round.
    """

    orca = FakeOrca()
    git = FakeGit()
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data(), review_finding_data(2))
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    git.conflict_commits = {fixer_sha("finding-2", 1)}
    orca.complete_dispatched(re_review("resolved"))
    await value.monitor(run_id)
    conflicted_commit = store.integration(
        next(item.finding_key for item in store.findings(run_id) if item.finding_id == "finding-2"),
        1,
    )
    assert conflicted_commit is not None and conflicted_commit.status == "conflict"

    orca.complete_dispatched(
        escalation("integration_conflict", "approve_unchanged", finding_id="finding-2")
    )
    await value.monitor(run_id)
    # The rebased fixer produces a genuinely different commit, and it applies.
    git.conflict_commits = set()
    git.head_override = fixer_sha("finding-2", 3)
    orca.complete_dispatched(fix_attempt("finding-2"))
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved", finding_id="finding-2"))

    result = await value.monitor(run_id)

    assert result.status == "complete", store.events(run_id)
    assert all(item.phase is FindingPhase.RESOLVED for item in result.findings)
    landed = store.integration(conflicted_commit.finding_key, 1)
    assert landed is not None
    assert landed.status == "integrated"
    # The slot now names the commit that actually landed, and the one it replaced
    # is on the record rather than overwritten in silence.
    assert landed.fixer_commit_sha == fixer_sha("finding-2", 3)
    reopened = [item for item in store.events(run_id) if item["kind"] == "integration_reopened"]
    assert reopened and reopened[-1]["payload"]["previous_commit_sha"] == fixer_sha("finding-2", 1)


async def test_a_fix_stacked_on_another_fix_is_never_rebased_off_it(tmp_path: Path) -> None:
    """A rebase that drops a commit is worse than a conflict that stops.

    An introduced-regression fix is built on the commit it corrects, and its
    integration replays that whole chain. Moving its base to the lane head would
    cut the predecessor out of the range and land half the work, so the retry is
    left where it is and the ceiling handles it.
    """

    orca = FakeOrca()
    git = FakeGit()
    value, store = controller(tmp_path, orca, git=git)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    original_commit = fixer_sha("finding-1", 1)
    orca.complete_dispatched(fix_attempt())
    await value.monitor(run_id)
    introduced = review_finding_data(2)
    revision = introduced["review_revision"]
    assert isinstance(revision, dict)
    revision["head_sha"] = original_commit
    git.conflict_commits = {fixer_sha("finding-2", 1)}
    orca.complete_dispatched(
        re_review(
            "regression_introduced_by_fix",
            new_findings=[{"origin": "introduced_by_fix", "finding": introduced}],
        )
    )
    await value.monitor(run_id)
    orca.complete_dispatched(fix_attempt("finding-2", base_sha=original_commit))
    await value.monitor(run_id)
    orca.complete_dispatched(re_review("resolved", finding_id="finding-2"))
    result = await value.monitor(run_id)
    stacked = next(item for item in result.findings if item.finding_id == "finding-2")
    assert stacked.escalation_reason is FindingReason.INTEGRATION_CONFLICT

    orca.complete_dispatched(
        escalation("integration_conflict", "approve_unchanged", finding_id="finding-2")
    )
    result = await value.monitor(run_id)

    stacked = next(item for item in result.findings if item.finding_id == "finding-2")
    assert stacked.dispatch_base_sha is None
    assert not [item for item in store.events(run_id) if item["kind"] == "fix_base_advanced"]
    # And not because there was nothing to move to: the lane head really has
    # advanced past the commit this fix is stacked on.
    assert store.lanes(run_id)[0].integration_head_sha not in {None, original_commit}


def budgeted(*, soft: int | None, hard: int | None, max_timeouts: int = 2) -> GraphConfig:
    """A config whose worker role carries a wall-clock budget."""

    base = config()
    return base.model_copy(
        update={
            "stage_budgets": StageBudgets(
                worker=StageBudget(soft_minutes=soft, hard_minutes=hard),
                max_timeouts=max_timeouts,
            )
        }
    )


def backdate_dispatch(store: StateStore, run_id: str, minutes: int) -> None:
    """Move this run's start reservations back, so a budget has something to measure.

    The clock is read from `stage_start_reserved` rather than from the stage
    row, so this is the whole of what "an agent has been running a while" means
    to the supervisor.
    """

    moved = datetime.now(UTC) - timedelta(minutes=minutes)
    with Session(store._engine) as session:
        for row in session.exec(
            select(EventRow).where(
                EventRow.run_id == run_id, EventRow.kind == "stage_start_reserved"
            )
        ).all():
            row.created_at = moved
            session.add(row)
        session.commit()


async def test_a_stage_inside_its_budget_is_not_overdue(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca, graph_config=budgeted(soft=45, hard=90))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    backdate_dispatch(store, run_id, 44)

    result = await value.monitor(run_id)

    assert result.overdue == []
    assert orca.releases == []
    assert not [item for item in store.events(run_id) if item["kind"] == "stage_overdue"]


async def test_an_unconfigured_budget_waits_forever_as_it_always_did(tmp_path: Path) -> None:
    """No default can know how long this repository's work takes, so there isn't one."""

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    backdate_dispatch(store, run_id, 10_000)

    result = await value.monitor(run_id)

    assert result.overdue == []
    assert orca.releases == []


async def test_a_stage_past_its_soft_budget_says_so_once_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """A slow agent and a wedged one look identical, so soft only reports."""

    orca = FakeOrca()
    value, store = controller(tmp_path, orca, graph_config=budgeted(soft=45, hard=None))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    backdate_dispatch(store, run_id, 50)

    result = await value.monitor(run_id)

    assert [item.budget for item in result.overdue] == ["soft"]
    overdue = result.overdue[0]
    assert overdue.role is StageKind.WORKER
    assert overdue.lane == "issue-100"
    assert overdue.minutes == 50
    assert orca.releases == []
    stage = next(item for item in store.stages(run_id) if item.role is StageKind.WORKER)
    assert stage.phase is StagePhase.DISPATCHED
    assert stage.orca_dispatch_id is not None

    # Still overdue next tick, but the event is not written again: an owner
    # reading the log wants to know when it went overdue, not how often the
    # ticker has since noticed.
    again = await value.monitor(run_id)
    assert [item.budget for item in again.overdue] == ["soft"]
    assert len([item for item in store.events(run_id) if item["kind"] == "stage_overdue"]) == 1


async def test_an_overdue_stage_reports_a_poll_loop_rather_than_only_minutes(
    tmp_path: Path,
) -> None:
    """Minutes cannot tell a slow stage from a stuck one; repeated turns can.

    A worker waiting on a subprocess by writing empty stdin every thirty seconds
    is late and doing nothing, and the two facts read identically from the
    ledger. Identical consecutive tool calls is a string comparison, so the
    supervisor can say which one it is without asking anybody.
    """

    orca = FakeOrca()
    orca.turns = ["exec:write_stdin({})"] * 8 + ["read:foo"]
    orca.turns.append("exec:write_stdin({})")
    value, store = controller(tmp_path, orca, graph_config=budgeted(soft=45, hard=None))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    backdate_dispatch(store, run_id, 50)

    result = await value.monitor(run_id)

    assert [item.activity for item in result.overdue] == ["exec repeated 9/10 turns unchanged"]
    event = next(item for item in store.events(run_id) if item["kind"] == "stage_overdue")
    assert event["payload"]["activity"] == "exec repeated 9/10 turns unchanged"


async def test_a_worker_that_cannot_be_read_reports_nothing_rather_than_idle(
    tmp_path: Path,
) -> None:
    """Unreadable is not idle, and one Orca failure must not stop the tick."""

    orca = FakeOrca()
    orca.unreadable_turns = True
    value, store = controller(tmp_path, orca, graph_config=budgeted(soft=45, hard=None))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    backdate_dispatch(store, run_id, 50)

    result = await value.monitor(run_id)

    assert [item.budget for item in result.overdue] == ["soft"]
    assert [item.activity for item in result.overdue] == [None]


async def test_a_stage_calling_different_things_is_named_without_a_moving_count(
    tmp_path: Path,
) -> None:
    """A count that moves every tick reprints the status line on noise."""

    orca = FakeOrca()
    orca.turns = ["exec:one", "read:two", "exec:three"]
    value, store = controller(tmp_path, orca, graph_config=budgeted(soft=45, hard=None))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    backdate_dispatch(store, run_id, 50)

    result = await value.monitor(run_id)

    assert [item.activity for item in result.overdue] == ["exec"]


async def test_a_stage_past_its_hard_budget_is_released_and_dispatched_again(
    tmp_path: Path,
) -> None:
    """Releasing the terminal is the whole mechanism.

    Orca hands the Task back to READY once its worker terminal is gone, and the
    existing dead-dispatch path clears the Dispatch id from there. Nothing here
    records a result, because a stage that ran out of time produced none.
    """

    orca = FakeOrca()
    value, store = controller(tmp_path, orca, graph_config=budgeted(soft=45, hard=90))
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    stage = next(item for item in store.stages(run_id) if item.role is StageKind.WORKER)
    dispatch_id = stage.orca_dispatch_id
    assert dispatch_id is not None
    backdate_dispatch(store, run_id, 95)

    result = await value.monitor(run_id)

    assert [item.budget for item in result.overdue] == ["hard"]
    assert orca.releases == [dispatch_id]
    # Releasing the Dispatch alone leaves the agent process tree resident, so the
    # terminal orkastrator opened has to be named for it to actually be reclaimed.
    assert orca.released_terminals == [f"term-{dispatch_id}"]
    timed_out = [item for item in store.events(run_id) if item["kind"] == "stage_timed_out"]
    assert len(timed_out) == 1
    released = next(item for item in store.stages(run_id) if item.role is StageKind.WORKER)
    assert released.result_json is None
    assert released.processed is False

    orca.abandon_dispatched()
    restarted = await value.monitor(run_id)

    assert [launch.role for launch in restarted.started] == [StageKind.WORKER]
    assert restarted.status == "active"


async def test_a_settled_stage_releases_the_terminal_orkastrator_opened(tmp_path: Path) -> None:
    """The lane completing is not what reclaims the worker; releasing its pane is.

    Orca refuses to close a terminal it did not create, so a stage that finishes
    normally used to leave its whole agent tree resident. The handle recorded at
    start is the only thing that tells the supervisor which pane is its to close.
    """

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    stage = next(item for item in store.stages(run_id) if item.role is StageKind.WORKER)
    assert stage.orca_terminal_handle == f"term-{stage.orca_dispatch_id}"

    orca.complete_dispatched(worker_result())
    await value.monitor(run_id)

    assert orca.releases == [stage.orca_dispatch_id]
    assert orca.released_terminals == [stage.orca_terminal_handle]


async def test_a_stage_that_wedges_every_time_blocks_instead_of_looping(tmp_path: Path) -> None:
    """A release is worth doing twice. A stage that wedges on every dispatch is
    telling you something a third one will not."""

    orca = FakeOrca()
    value, store = controller(
        tmp_path, orca, graph_config=budgeted(soft=None, hard=90, max_timeouts=1)
    )
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    backdate_dispatch(store, run_id, 95)

    await value.monitor(run_id)
    orca.abandon_dispatched()
    await value.monitor(run_id)
    backdate_dispatch(store, run_id, 95)
    result = await value.monitor(run_id)

    assert result.status == "blocked"
    blocked = [item for item in store.events(run_id) if item["kind"] == "lane_blocked"]
    payload = blocked[-1]["payload"]
    assert isinstance(payload, dict)
    assert "exceeded its 90-minute budget" in payload["reason"]


async def test_a_merged_pull_request_lands_the_lane_instead_of_failing_it(
    tmp_path: Path,
) -> None:
    """Merged and closed are opposite outcomes, and only one of them is a failure.

    Publication read every non-OPEN state as "the authorized lane pull request
    is no longer open", so an owner merging a lane's pull request - the thing
    the lane exists to produce - was reported as the lane failing. The merge is
    discovered here while observing checks, which is the window
    `_a_complete_lane_is_never_touched_by_publication_again` does not cover: the
    lane is still ACTIVE, so it is still being published.
    """

    class MergedWhileObserving(FakePublisher):
        async def checks(self, receipt: PublicationReceipt) -> CiReceipt:
            raise PullRequestLanded(receipt.head_sha)

    orca = FakeOrca()
    publisher = MergedWhileObserving()
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json())

    landed = await value.monitor(run_id)

    assert landed.status == "complete", store.events(run_id)
    receipt = store.publications(run_id)[-1]
    assert receipt.landed is True
    assert receipt.draft is False
    assert [item["kind"] for item in store.events(run_id)].count("pull_request_landed") == 1
    # A merge is not a check result, and recording one would claim CI passed for
    # a head whose checks now belong to the base branch's history.
    assert store.ci_receipts(run_id) == []
    # And the lane is not published again, so the merge cannot be re-discovered.
    again = await value.monitor(run_id)
    assert again.status == "complete"
    assert len(publisher.publish_calls) == 1


async def test_a_lane_published_onto_a_merged_pull_request_never_asks_for_checks(
    tmp_path: Path,
) -> None:
    """A landed receipt is terminal, so nothing downstream of it runs."""

    class AlreadyMerged(FakePublisher):
        async def publish(
            self,
            *,
            run_id: str,
            lane: LaneRecord,
            head_sha: str,
            previous: PublicationReceipt | None,
        ) -> PublicationReceipt:
            receipt = await super().publish(
                run_id=run_id, lane=lane, head_sha=head_sha, previous=previous
            )
            return receipt.model_copy(update={"draft": False, "landed": True})

        async def checks(self, receipt: PublicationReceipt) -> CiReceipt:
            raise AssertionError("a merged head has no checks left to observe")

    orca = FakeOrca()
    publisher = AlreadyMerged()
    value, store = controller(tmp_path, orca, publisher=publisher)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_initial_review(value, orca, run_id)
    orca.complete_dispatched(initial_review_report_json())

    result = await value.monitor(run_id)

    assert result.status == "complete", store.events(run_id)
    assert publisher.ready_calls == []
    assert store.publications(run_id)[-1].landed is True


async def test_a_run_accepted_before_the_policy_was_stored_says_so_rather_than_nothing(
    tmp_path: Path,
) -> None:
    """An empty change list and an unreadable one must not print the same.

    Every run live when this shipped was accepted without its policy payload,
    so the diff cannot be computed for them. Reporting "no changes" there would
    tell an owner the config matches when the digests say it does not.
    """

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    with Session(store._engine) as session:
        row = session.get(AcceptanceAuthorizationRow, run_id)
        assert row is not None
        row.config_json = None
        session.add(row)
        session.commit()

    changed = ExecutionController(
        config=config(max_workers=3),
        orca=orca,
        store=store,
        git=FakeGit(),
        publisher=FakePublisher(),
    )
    preview = changed.reauthorize(run_id, "", apply=False)

    assert preview.comparable is False
    assert preview.changes == []

    applied = changed.reauthorize(run_id, "raised max_workers on purpose")

    # Applying it stores the policy, so the next change is readable.
    assert applied.applied is True
    assert store.accepted_config(run_id) == changed._config.model_dump(mode="json")
    again = ExecutionController(
        config=config(max_workers=2),
        orca=orca,
        store=store,
        git=FakeGit(),
        publisher=FakePublisher(),
    ).reauthorize(run_id, "", apply=False)
    assert again.comparable is True
    assert [(item.path, item.before, item.after) for item in again.changes] == [
        ("max_parallel_workers", "3", "2")
    ]
    recorded = [
        item for item in store.events(run_id) if item["kind"] == "supervisor_reauthorized_policy"
    ]
    assert recorded[-1]["payload"]["changes"] == []


async def test_an_unchanged_run_records_the_policy_it_was_accepted_under(tmp_path: Path) -> None:
    """A run started before the payload existed becomes readable without authorizing anything.

    The digest already proves the config on disk is the accepted one, so
    recording it changes nothing about what the run is allowed to do. It just
    means the *next* policy change can be read as fields instead of digests.
    """

    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    with Session(store._engine) as session:
        row = session.get(AcceptanceAuthorizationRow, run_id)
        assert row is not None
        row.config_json = None
        session.add(row)
        session.commit()
    assert store.accepted_config(run_id) is None

    preview = value.reauthorize(run_id, "", apply=False)

    assert preview.comparable is True
    assert preview.changes == []
    assert preview.applied is False
    assert store.accepted_config(run_id) == config().model_dump(mode="json")
    # Reading is not authorizing: nothing was re-frozen.
    assert [
        item for item in store.events(run_id) if item["kind"] == "supervisor_reauthorized_policy"
    ] == []
