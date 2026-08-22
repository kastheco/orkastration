"""Dynamic convergence scheduling and restart tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orkastrator.config import AgentProfile, GraphConfig
from orkastrator.execution import ExecutionController
from orkastrator.git import GitCommandResult, LocalGit
from orkastrator.models import (
    CiCheckResult,
    CiReceipt,
    FindingPhase,
    FindingReason,
    LaneRecord,
    PublicationReceipt,
    ReReviewResult,
    StageKind,
    StagePhase,
    SupervisorPlan,
    ValidationRequirement,
    ValidationResult,
)
from orkastrator.orca import JsonObject, OrcaError
from orkastrator.publication import PublicationError
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
        self.runs_by_id: dict[str, JsonObject] = {}
        self.bound_runs: list[str] = []
        self.unreadable_dispatches: set[str] = set()
        self.messages_by_run: dict[str, list[JsonObject]] = {}
        self.fail_after_run_create = False
        self.fail_after_task_create = False
        self.fail_after_worker_start = False

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

    async def start_worker(self, **kwargs: object) -> tuple[str, str, JsonObject]:
        task_id = str(kwargs["task_id"])
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
        if self.fail_after_worker_start:
            self.fail_after_worker_start = False
            raise RuntimeError("crash after remote worker start")
        return dispatch_id, worktree_id, {"ok": True, "dispatchId": dispatch_id}

    async def release_worker(self, dispatch_id: str) -> JsonObject:
        self.releases.append(dispatch_id)
        return {"ok": True}

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
        raise AssertionError(f"unexpected fake worktree: {worktree_id}")

    async def changed_paths(self, worktree_id: str, base_sha: str, head_sha: str) -> list[str]:
        if self.changed_override is not None and "finding-" in worktree_id:
            return self.changed_override
        for number in range(1, 10):
            if f"finding-{number}" in worktree_id:
                return (
                    ["src/file1.py"] if "ci-finding-" in worktree_id else [f"src/file{number}.py"]
                )
        return ["src/file1.py"]

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
        if self.conflict:
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
        return [
            ValidationResult(command="pytest", status="passed", output="passed")
            for _ in requirements
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
    assert [item.role for item in result.started] == [StageKind.RE_REVIEWER], store.events(run_id)

    first_stage = next(
        item for item in store.stages(run_id) if item.orca_task_id == result.started[0].task_id
    )
    assert first_stage.finding_id is not None
    orca.complete_task(
        result.started[0].task_id,
        re_review("resolved", finding_id=first_stage.finding_id),
    )
    result = await value.monitor(run_id)
    assert [item.role for item in result.started] == [StageKind.RE_REVIEWER]
    second_stage = next(
        item for item in store.stages(run_id) if item.orca_task_id == result.started[0].task_id
    )
    assert second_stage.finding_id is not None
    orca.complete_task(
        result.started[0].task_id,
        re_review("resolved", finding_id=second_stage.finding_id),
    )
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


async def test_stale_ci_sha_and_publication_errors_block_the_lane(tmp_path: Path) -> None:
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
        result = await value.monitor(run_id)
        assert result.status == "blocked"
        blocked = [item for item in store.events(run_id) if item["kind"] == "lane_blocked"]
        assert blocked
        payload = blocked[-1]["payload"]
        assert isinstance(payload, dict) and payload["reason"]


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
