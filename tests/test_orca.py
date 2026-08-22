"""Orca JSON adapter contract tests."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orkastrator.config import AgentProfile
from orkastrator.orca import JsonObject, OrcaClient, OrcaError, SubprocessRunner


class FakeRunner:
    def __init__(self, responses: list[JsonObject]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    async def run(self, arguments: Sequence[str]) -> JsonObject:
        self.calls.append(tuple(arguments))
        return self.responses.pop(0)


def profile(*, agent: str = "codex", fast: bool = False) -> AgentProfile:
    return AgentProfile(agent=agent, model="gpt-test", strength="high", fast=fast)


def worktree_response() -> JsonObject:
    return {
        "ok": True,
        "result": {
            "worktrees": [
                {
                    "worktreeId": "repo::/tmp/main",
                    "repoId": "repo",
                    "repo": "example",
                    "path": "/tmp/main",
                    "displayName": "main",
                    "workspaceStatus": "in-progress",
                    "status": "working",
                }
            ]
        },
    }


async def test_snapshot_parses_stable_fields() -> None:
    snapshot = await OrcaClient(FakeRunner([worktree_response()])).snapshot()
    assert snapshot.worktrees[0].worktree_id == "repo::/tmp/main"


async def test_create_run_and_task_parse_nested_ids() -> None:
    runner = FakeRunner(
        [
            {"id": "request-1", "ok": True, "result": {"run": {"id": "run-1"}}},
            {"id": "request-2", "ok": True, "result": {"taskId": "task-1"}},
        ]
    )
    client = OrcaClient(runner)

    run_id, _ = await client.create_run("Ship work")
    task_id, _ = await client.create_task("Implement it", ["task-0"])

    assert run_id == "run-1"
    assert task_id == "task-1"
    assert runner.calls[1] == (
        "orchestration",
        "task-create",
        "--spec",
        "Implement it",
        "--deps",
        '["task-0"]',
        "--json",
    )


async def test_runs_lists_recoverable_orca_runs() -> None:
    runner = FakeRunner([{"ok": True, "result": {"runs": [{"id": "run-1", "objective": "work"}]}}])
    assert await OrcaClient(runner).runs() == [{"id": "run-1", "objective": "work"}]
    assert runner.calls == [("orchestration", "run-list", "--json")]


async def test_tasks_and_release_worker() -> None:
    runner = FakeRunner(
        [
            {"ok": True, "result": {"tasks": [{"id": "task-1", "status": "ready"}]}},
            {"ok": True, "result": {}},
        ]
    )
    client = OrcaClient(runner)
    assert (await client.tasks("run-1"))[0]["id"] == "task-1"
    await client.release_worker("dispatch-1")
    assert runner.calls[1] == (
        "orchestration",
        "worker-release",
        "--dispatch",
        "dispatch-1",
        "--json",
    )


async def test_worker_dispatch_recovers_supervised_worktree() -> None:
    runner = FakeRunner(
        [
            {"ok": True, "result": {"dispatch": {"id": "dispatch-1"}}},
            {
                "ok": True,
                "result": {"worker": {"worktree_id": "repo::/tmp/issue-123"}},
            },
        ]
    )

    recovered = await OrcaClient(runner).worker_dispatch("task-1")

    assert recovered == ("dispatch-1", "repo::/tmp/issue-123")
    assert runner.calls == [
        ("orchestration", "dispatch-show", "--task", "task-1", "--json"),
        ("orchestration", "worker-show", "--dispatch", "dispatch-1", "--json"),
    ]


async def test_worker_dispatch_returns_none_for_unassigned_task() -> None:
    client = OrcaClient(FakeRunner([{"ok": True, "result": {"dispatch": None}}]))
    assert await client.worker_dispatch("task-1") is None


async def test_start_worker_uses_model_effort_and_new_worktree() -> None:
    runner = FakeRunner(
        [
            {
                "ok": True,
                "result": {
                    "dispatchId": "dispatch-1",
                    "worker": {"worktreeId": "repo::/tmp/issue-123"},
                },
            }
        ]
    )
    client = OrcaClient(runner)

    dispatch_id, worktree_id, _ = await client.start_worker(
        task_id="task-1",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id=None,
        profile=profile(),
    )

    assert (dispatch_id, worktree_id) == ("dispatch-1", "repo::/tmp/issue-123")
    assert runner.calls[0] == (
        "orchestration",
        "worker-start",
        "--task",
        "task-1",
        "--worktree",
        "new-top-level",
        "--repo",
        "id:repo",
        "--name",
        "issue-123",
        "--setup",
        "run",
        "--agent",
        "codex",
        "--model",
        "gpt-test",
        "--effort",
        "high",
        "--json",
    )


async def test_start_worker_reuses_lane_worktree() -> None:
    runner = FakeRunner([{"ok": True, "result": {"dispatchId": "dispatch-2"}}])
    client = OrcaClient(runner)
    _, worktree_id, _ = await client.start_worker(
        task_id="task-2",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id="repo::/tmp/issue-123",
        profile=profile(),
    )
    assert worktree_id == "repo::/tmp/issue-123"
    assert "id:repo::/tmp/issue-123" in runner.calls[0]


async def test_start_fixer_uses_child_worktree_at_exact_review_head() -> None:
    runner = FakeRunner(
        [
            {
                "ok": True,
                "result": {
                    "dispatchId": "dispatch-3",
                    "worker": {"worktreeId": "repo::/tmp/finding-1"},
                },
            }
        ]
    )
    client = OrcaClient(runner)
    _, worktree_id, _ = await client.start_worker(
        task_id="task-3",
        lane_name="finding-1-fixer-r1",
        repo_selector="id:repo",
        worktree_id=None,
        profile=profile(),
        base_ref="b" * 40,
        parent_worktree_id="repo::/tmp/issue-123",
    )

    assert worktree_id == "repo::/tmp/finding-1"
    assert runner.calls[0][0:12] == (
        "orchestration",
        "worker-start",
        "--task",
        "task-3",
        "--worktree",
        "new-child",
        "--repo",
        "id:repo",
        "--name",
        "finding-1-fixer-r1",
        "--setup",
        "run",
    )
    assert runner.calls[0][12:14] == ("--base-branch", "b" * 40)


async def test_fast_codex_worker_uses_custom_argv_before_supervised_dispatch() -> None:
    runner = FakeRunner(
        [
            {"ok": True, "result": {"worktree": {"id": "repo::/tmp/issue-123"}}},
            {"ok": True, "result": {"terminal": {"handle": "terminal-1"}}},
            {"ok": True, "result": {}},
            {"ok": True, "result": {"dispatchId": "dispatch-1"}},
        ]
    )
    client = OrcaClient(runner)

    dispatch_id, worktree_id, _ = await client.start_worker(
        task_id="task-1",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id=None,
        profile=profile(fast=True),
    )

    assert (dispatch_id, worktree_id) == ("dispatch-1", "repo::/tmp/issue-123")
    assert runner.calls[0] == (
        "worktree",
        "create",
        "--name",
        "issue-123",
        "--no-parent",
        "--repo",
        "id:repo",
        "--setup",
        "run",
        "--json",
    )
    assert runner.calls[1] == (
        "terminal",
        "create",
        "--worktree",
        "id:repo::/tmp/issue-123",
        "--title",
        "issue-123-codex-fast",
        "--command",
        "codex --model gpt-test "
        "-c 'model_reasoning_effort=\"high\"' -c 'service_tier=\"priority\"'",
        "--json",
    )
    assert runner.calls[2][0:6] == (
        "terminal",
        "wait",
        "--terminal",
        "terminal-1",
        "--for",
        "tui-idle",
    )
    assert runner.calls[3] == (
        "orchestration",
        "worker-start",
        "--task",
        "task-1",
        "--terminal",
        "terminal-1",
        "--json",
    )


async def test_fast_claude_worker_reuses_worktree_with_fast_settings() -> None:
    runner = FakeRunner(
        [
            {"ok": True, "result": {"terminalHandle": "terminal-2"}},
            {"ok": True, "result": {}},
            {"ok": True, "result": {"dispatchId": "dispatch-2"}},
        ]
    )
    client = OrcaClient(runner)

    _, worktree_id, _ = await client.start_worker(
        task_id="task-2",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id="repo::/tmp/issue-123",
        profile=profile(agent="claude", fast=True),
    )

    assert worktree_id == "repo::/tmp/issue-123"
    assert runner.calls[0][0:8] == (
        "terminal",
        "create",
        "--worktree",
        "id:repo::/tmp/issue-123",
        "--title",
        "issue-123-claude-fast",
        "--command",
        "claude --model gpt-test --effort high --settings '{\"fastMode\":true}'",
    )


async def test_fast_worker_rejects_unsupported_agent() -> None:
    client = OrcaClient(FakeRunner([]))
    with pytest.raises(OrcaError, match="unsupported for worker agent: omp"):
        await client.start_worker(
            task_id="task-1",
            lane_name="issue-123",
            repo_selector="id:repo",
            worktree_id="repo::/tmp/issue-123",
            profile=profile(agent="omp", fast=True),
        )


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False},
        {"ok": True},
        {"ok": True, "result": {"tasks": "bad"}},
    ],
)
async def test_invalid_orca_contracts_are_rejected(response: JsonObject) -> None:
    client = OrcaClient(FakeRunner([response]))
    with pytest.raises(OrcaError):
        if response.get("ok") is False:
            await client.status()
        else:
            await client.tasks("run-1")


class FakeProcess:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


async def test_subprocess_runner_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(stdout=b'{"ok": true}')

    async def create(*arguments: str, **kwargs: Any) -> FakeProcess:
        assert arguments == ("orca-ide", "status", "--json")
        assert kwargs["cwd"] == Path("/tmp")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    runner = SubprocessRunner(("orca-ide",), Path("/tmp"), 1)
    assert await runner.run(("status", "--json")) == {"ok": True}


@pytest.mark.parametrize(
    ("process", "message"),
    [
        (FakeProcess(stdout=b"", stderr=b"bad", returncode=2), "rc=2: bad"),
        (FakeProcess(stdout=b"nope"), "invalid JSON"),
        (FakeProcess(stdout=b"[]"), "JSON object"),
    ],
)
async def test_subprocess_runner_rejects_bad_results(
    monkeypatch: pytest.MonkeyPatch, process: FakeProcess, message: str
) -> None:
    async def create(*arguments: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(OrcaError, match=message):
        await SubprocessRunner(("orca-ide",), Path("/tmp"), 1).run(("status", "--json"))


async def test_subprocess_runner_kills_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowProcess(FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            if not self.killed:
                await asyncio.sleep(1)
            return self.stdout, self.stderr

    process = SlowProcess(stdout=b"")

    async def create(*arguments: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(OrcaError, match="timed out"):
        await SubprocessRunner(("orca-ide",), Path("/tmp"), 0.001).run(("status", "--json"))
    assert process.killed is True
