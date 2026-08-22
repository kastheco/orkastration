"""Dynamic convergence scheduling and restart tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kasgraph.config import AgentProfile, GraphConfig
from kasgraph.execution import ExecutionController
from kasgraph.models import (
    FindingPhase,
    FindingReason,
    ReReviewResult,
    StageKind,
    SupervisorPlan,
)
from kasgraph.orca import JsonObject
from kasgraph.store import StateStore
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


def fix_attempt(finding_id: str = "finding-1", *, round: int = 1, status: str = "fixed") -> str:
    return json.dumps(
        {
            "finding_id": finding_id,
            "round": round,
            "status": status,
            "base_sha": "b" * 40,
            "commit_sha": "d" * 40 if status == "fixed" else None,
            "changed_paths": ["src/file1.py"] if status == "fixed" else [],
            "validation_results": [],
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
            "reviewed_commit_sha": "d" * 40,
            "verdict": verdict,
            "rationale": "Checked the exact fix.",
            "evidence": [],
            "new_findings": new_findings or [],
        }
    )


def escalation(reason: str, action: str = "block", *, round: int = 1) -> str:
    return json.dumps(
        {
            "finding_id": "finding-1",
            "round": round,
            "reason": reason,
            "action": action,
            "rationale": "Bounded adjudication.",
            "revised_finding": None,
        }
    )


class FakeOrca:
    def __init__(self) -> None:
        self.tasks_by_id: dict[str, JsonObject] = {}
        self.dependencies: dict[str, list[str]] = {}
        self.starts: list[dict[str, object]] = []
        self.releases: list[str] = []
        self.runs_by_id: dict[str, JsonObject] = {}
        self.fail_after_run_create = False
        self.fail_after_task_create = False
        self.fail_after_worker_start = False

    async def create_run(self, objective: str) -> tuple[str, JsonObject]:
        assert objective.startswith("Do work\n\nKasgraph run: ")
        run_id = f"orca-run-{len(self.runs_by_id) + 1}"
        self.runs_by_id[run_id] = {"id": run_id, "objective": objective}
        if self.fail_after_run_create:
            self.fail_after_run_create = False
            raise RuntimeError("crash after remote run creation")
        return run_id, {"ok": True}

    async def runs(self) -> list[JsonObject]:
        return list(self.runs_by_id.values())

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
        worktree_id = str(kwargs.get("worktree_id") or "repo::/tmp/issue")
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

    def _refresh_ready(self) -> None:
        for task_id, dependencies in self.dependencies.items():
            if self.tasks_by_id[task_id]["status"] != "pending":
                continue
            if all(self.tasks_by_id[item]["status"] == "completed" for item in dependencies):
                self.tasks_by_id[task_id]["status"] = "ready"


def controller(
    tmp_path: Path,
    orca: FakeOrca,
    *,
    graph_config: GraphConfig | None = None,
) -> tuple[ExecutionController, StateStore]:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    return (
        ExecutionController(config=graph_config or config(), orca=orca, store=store),
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
        "finding-1": FindingPhase.RESOLVED,
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
    persisted = ReReviewResult.model_validate_json(body)
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
    assert phases["finding-1"] is FindingPhase.RESOLVED
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


async def test_finding_fanout_stays_serial_before_worktree_isolation(tmp_path: Path) -> None:
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
    assert len(fixer_starts) == 1


async def test_restart_reconciliation_does_not_duplicate_tasks_or_receipts(tmp_path: Path) -> None:
    orca = FakeOrca()
    value, store = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    await value.accept(run_id)
    await advance_to_fixer(value, orca, run_id, review_finding_data())
    task_count = len(orca.tasks_by_id)
    event_count = len(store.events(run_id))

    restarted = ExecutionController(config=config(), orca=orca, store=store)
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

    restarted = ExecutionController(config=config(), orca=orca, store=store)
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

    restarted = ExecutionController(config=config(), orca=orca, store=store)
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

    restarted = ExecutionController(config=config(), orca=orca, store=store)
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
