"""Accepted graph construction and monitoring tests."""

from pathlib import Path

import pytest

from kasgraph.config import GraphConfig
from kasgraph.execution import ExecutionController
from kasgraph.models import RoleName, StagePhase, SupervisorPlan
from kasgraph.orca import JsonObject
from kasgraph.store import StateStore


def config(*, max_parallel: int = 2) -> GraphConfig:
    profile = {"agent": "codex", "model": "gpt-test", "strength": "high"}
    return GraphConfig.model_validate(
        {
            "version": 1,
            "max_parallel_lanes": max_parallel,
            "supervisor": profile,
            "roles": {
                "worker": profile,
                "initial_reviewer": profile,
                "fixer": profile,
                "re_reviewer": {**profile, "strength": "xhigh"},
            },
        }
    )


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


class FakeOrca:
    def __init__(self) -> None:
        self.tasks_by_id: dict[str, JsonObject] = {}
        self.dependencies: dict[str, list[str]] = {}
        self.starts: list[dict[str, object]] = []
        self.releases: list[str] = []

    async def create_run(self, objective: str) -> tuple[str, JsonObject]:
        assert objective == "Do work"
        return "orca-run-1", {"ok": True}

    async def create_task(self, spec: str, dependencies: list[str]) -> tuple[str, JsonObject]:
        task_id = f"task-{len(self.tasks_by_id) + 1}"
        self.dependencies[task_id] = dependencies
        self.tasks_by_id[task_id] = {
            "id": task_id,
            "status": "ready" if not dependencies else "pending",
            "spec": spec,
        }
        return task_id, {"ok": True}

    async def tasks(self, orca_run_id: str) -> list[JsonObject]:
        assert orca_run_id == "orca-run-1"
        self._refresh_ready()
        return list(self.tasks_by_id.values())

    async def start_worker(self, **kwargs: object) -> tuple[str, str, JsonObject]:
        task_id = str(kwargs["task_id"])
        self.tasks_by_id[task_id]["status"] = "dispatched"
        worktree_id = str(kwargs.get("worktree_id") or "repo::/tmp/issue")
        dispatch_id = f"dispatch-{len(self.starts) + 1}"
        self.starts.append({**kwargs, "dispatch_id": dispatch_id, "worktree_id": worktree_id})
        return dispatch_id, worktree_id, {"ok": True, "dispatchId": dispatch_id}

    async def release_worker(self, dispatch_id: str) -> JsonObject:
        self.releases.append(dispatch_id)
        return {"ok": True}

    def complete_dispatched(self) -> None:
        for task in self.tasks_by_id.values():
            if task["status"] == "dispatched":
                task["status"] = "completed"

    def _refresh_ready(self) -> None:
        for task_id, dependencies in self.dependencies.items():
            if self.tasks_by_id[task_id]["status"] != "pending":
                continue
            if all(self.tasks_by_id[item]["status"] == "completed" for item in dependencies):
                self.tasks_by_id[task_id]["status"] = "ready"


def controller(tmp_path: Path, orca: FakeOrca, *, max_parallel: int = 2) -> ExecutionController:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    return ExecutionController(config=config(max_parallel=max_parallel), orca=orca, store=store)


async def test_accept_builds_four_stage_dag_and_starts_worker(tmp_path: Path) -> None:
    orca = FakeOrca()
    value = controller(tmp_path, orca)
    receipt = value.propose(proposal())
    result = await value.accept(receipt.run_id)

    assert result.status == "active"
    assert len(orca.tasks_by_id) == 4
    assert [stage.role for stage in result.stages] == list(RoleName)
    assert [launch.role for launch in result.started] == [RoleName.WORKER]
    assert orca.dependencies["task-2"] == ["task-1"]


async def test_monitor_releases_worker_and_reuses_worktree_for_review(tmp_path: Path) -> None:
    orca = FakeOrca()
    value = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    first = await value.accept(run_id)
    orca.complete_dispatched()

    second = await value.monitor(run_id)

    assert orca.releases == [first.started[0].dispatch_id]
    assert second.started[0].role is RoleName.INITIAL_REVIEWER
    assert orca.starts[1]["worktree_id"] == orca.starts[0]["worktree_id"]
    assert second.stages[0].phase is StagePhase.COMPLETED


async def test_monitor_completes_full_review_loop(tmp_path: Path) -> None:
    orca = FakeOrca()
    value = controller(tmp_path, orca)
    run_id = value.propose(proposal()).run_id
    result = await value.accept(run_id)

    for _ in range(4):
        orca.complete_dispatched()
        result = await value.monitor(run_id)

    assert result.status == "complete"
    assert len(orca.starts) == 4
    assert len(orca.releases) == 4
    assert all(stage.phase is StagePhase.COMPLETED for stage in result.stages)


async def test_parallel_limit_holds_second_lane_until_first_finishes(tmp_path: Path) -> None:
    orca = FakeOrca()
    value = controller(tmp_path, orca, max_parallel=1)
    run_id = value.propose(proposal(lane_count=2)).run_id
    result = await value.accept(run_id)
    assert [launch.lane for launch in result.started] == ["issue-100"]

    for _ in range(4):
        orca.complete_dispatched()
        result = await value.monitor(run_id)

    assert result.started[0].lane == "issue-101"


async def test_accept_rejects_non_lane_plan(tmp_path: Path) -> None:
    orca = FakeOrca()
    value = controller(tmp_path, orca)
    run_id = value.propose(proposal(next_action="needs_owner")).run_id
    with pytest.raises(ValueError, match="no executable lanes"):
        await value.accept(run_id)
