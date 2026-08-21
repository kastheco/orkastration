"""Orca adapter contract tests."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from kasgraph.models import LaneProposal
from kasgraph.orca import JsonObject, OrcaClient, OrcaError, SubprocessRunner


class FakeRunner:
    def __init__(self, responses: list[JsonObject]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    async def run(self, arguments: Sequence[str]) -> JsonObject:
        self.calls.append(tuple(arguments))
        return self.responses.pop(0)


def worktree_response(*, name: str = "main", worktree_id: str = "repo::/tmp/main") -> JsonObject:
    return {
        "ok": True,
        "result": {
            "worktrees": [
                {
                    "worktreeId": worktree_id,
                    "repoId": "repo",
                    "repo": "example",
                    "path": "/tmp/main",
                    "displayName": name,
                    "workspaceStatus": "in-progress",
                    "status": "working",
                    "liveTerminalCount": 1,
                }
            ]
        },
    }


async def test_snapshot_parses_stable_worktree_fields() -> None:
    client = OrcaClient(FakeRunner([worktree_response()]))

    snapshot = await client.snapshot()

    assert snapshot.worktrees[0].worktree_id == "repo::/tmp/main"
    assert snapshot.active_count == 1


async def test_create_lane_reuses_existing_name() -> None:
    runner = FakeRunner([worktree_response(name="issue-123")])
    client = OrcaClient(runner)
    lane = LaneProposal(
        name="issue-123",
        issue_id="ISSUE-123",
        repo_selector="id:repo",
        role="implementer",
        prompt="Implement the issue.",
        stop_condition="Tests pass.",
    )

    worktree_id, payload = await client.create_lane(lane)

    assert worktree_id == "repo::/tmp/main"
    assert payload["reused"] is True
    assert runner.calls == [("worktree", "ps", "--json")]


async def test_non_success_response_is_rejected() -> None:
    client = OrcaClient(FakeRunner([{"ok": False}]))
    with pytest.raises(OrcaError, match="non-success"):
        await client.status()


async def test_create_lane_uses_fixed_orca_arguments() -> None:
    runner = FakeRunner(
        [
            {"ok": True, "result": {"worktrees": []}},
            {"ok": True, "result": {"worktree": {"id": "repo::/tmp/issue-123"}}},
        ]
    )
    client = OrcaClient(runner)
    lane = LaneProposal(
        name="issue-123",
        issue_id="ISSUE-123",
        repo_selector="id:repo",
        role="implementer",
        prompt="Implement the issue.",
        stop_condition="Tests pass.",
    )

    worktree_id, _ = await client.create_lane(lane)

    assert worktree_id == "repo::/tmp/issue-123"
    assert runner.calls[1] == (
        "worktree",
        "create",
        "--repo",
        "id:repo",
        "--name",
        "issue-123",
        "--no-parent",
        "--agent",
        "codex",
        "--prompt",
        "Implement the issue.",
        "--json",
    )


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"worktrees": "not-a-list"}},
        {"ok": True, "result": {"worktrees": [{}]}},
    ],
)
async def test_snapshot_rejects_invalid_contract(response: JsonObject) -> None:
    with pytest.raises(OrcaError):
        await OrcaClient(FakeRunner([response])).snapshot()


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
    runner = SubprocessRunner(("orca-ide",), Path("/tmp"), 1)

    with pytest.raises(OrcaError, match=message):
        await runner.run(("status", "--json"))


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
    runner = SubprocessRunner(("orca-ide",), Path("/tmp"), 0.001)

    with pytest.raises(OrcaError, match="timed out"):
        await runner.run(("status", "--json"))
    assert process.killed is True
