"""Subscription-backed Codex planner tests."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kasgraph.config import AgentProfile
from kasgraph.planner import CodexCliPlanner, PlannerError, SubprocessCodexRunner


def plan_json() -> str:
    return json.dumps(
        {
            "objective": "Ship work",
            "rationale": "One lane is unblocked.",
            "next_action": "propose_lanes",
            "owner_question": None,
            "lanes": [
                {
                    "name": "issue-123",
                    "issue_id": "ISSUE-123",
                    "repo_selector": "id:repo",
                    "dependencies": [],
                    "prompt": "Implement it.",
                    "stop_condition": "Tests pass.",
                }
            ],
        }
    )


class FakeRunner:
    def __init__(self, output: str):
        self.output = output
        self.schema: dict[str, object] | None = None
        self.prompt = ""

    async def run(self, prompt: str, schema: dict[str, object]) -> str:
        self.prompt = prompt
        self.schema = schema
        return self.output


async def test_planner_passes_supervisor_schema_and_validates_output() -> None:
    runner = FakeRunner(plan_json())
    plan = await CodexCliPlanner(runner).plan("Ship work")
    assert plan.lanes[0].issue_id == "ISSUE-123"
    assert runner.schema is not None
    assert runner.schema["title"] == "SupervisorPlan"
    assert "Never mutate Linear" in runner.prompt


async def test_planner_rejects_invalid_json() -> None:
    with pytest.raises(PlannerError, match="failed SupervisorPlan"):
        await CodexCliPlanner(FakeRunner("{}")).plan("Ship work")


class FakeProcess:
    def __init__(self, output_path: Path, *, returncode: int = 0, write_output: bool = True):
        self.output_path = output_path
        self.returncode = returncode
        self.write_output = write_output
        self.killed = False
        self.prompt = b""

    async def communicate(self, prompt: bytes | None = None) -> tuple[bytes, bytes]:
        self.prompt = prompt or b""
        if self.write_output:
            self.output_path.write_text(plan_json())
        return b"stdout", b"planner failed"

    def kill(self) -> None:
        self.killed = True


def subprocess_runner(*, agent: str = "codex", timeout: float = 1) -> SubprocessCodexRunner:
    return SubprocessCodexRunner(
        command=("codex",),
        cwd=Path("/repo"),
        profile=AgentProfile(agent=agent, model="gpt-test", strength="xhigh"),
        timeout_seconds=timeout,
    )


async def test_subprocess_runner_uses_ephemeral_read_only_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: tuple[str, ...] = ()
    process: FakeProcess | None = None

    async def create(*arguments: str, **kwargs: Any) -> FakeProcess:
        nonlocal observed, process
        observed = arguments
        output_path = Path(arguments[arguments.index("--output-last-message") + 1])
        process = FakeProcess(output_path)
        assert kwargs["cwd"] == Path("/repo")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    output = await subprocess_runner().run("Plan this", {"type": "object"})

    assert json.loads(output)["next_action"] == "propose_lanes"
    assert observed[:5] == ("codex", "exec", "--ephemeral", "--sandbox", "read-only")
    assert 'model_reasoning_effort="xhigh"' in observed
    assert process is not None and process.prompt == b"Plan this"


async def test_subprocess_runner_rejects_wrong_agent() -> None:
    with pytest.raises(PlannerError, match="must be codex"):
        await subprocess_runner(agent="claude").run("Plan", {})


async def test_subprocess_runner_reports_failure_and_missing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed(*arguments: str, **kwargs: Any) -> FakeProcess:
        output_path = Path(arguments[arguments.index("--output-last-message") + 1])
        return FakeProcess(output_path, returncode=2, write_output=False)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed)
    with pytest.raises(PlannerError, match="rc=2"):
        await subprocess_runner().run("Plan", {})

    async def missing(*arguments: str, **kwargs: Any) -> FakeProcess:
        output_path = Path(arguments[arguments.index("--output-last-message") + 1])
        return FakeProcess(output_path, write_output=False)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    with pytest.raises(PlannerError, match="did not write"):
        await subprocess_runner().run("Plan", {})


async def test_subprocess_runner_kills_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowProcess(FakeProcess):
        async def communicate(self, prompt: bytes | None = None) -> tuple[bytes, bytes]:
            if not self.killed:
                await asyncio.sleep(1)
            return b"", b""

    process: SlowProcess | None = None

    async def create(*arguments: str, **kwargs: Any) -> SlowProcess:
        nonlocal process
        output_path = Path(arguments[arguments.index("--output-last-message") + 1])
        process = SlowProcess(output_path, write_output=False)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(PlannerError, match="timed out"):
        await subprocess_runner(timeout=0.001).run("Plan", {})
    assert process is not None and process.killed is True
