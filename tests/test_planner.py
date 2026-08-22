"""Subscription-backed Codex planner tests."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kasgraph.config import PlannerProfile
from kasgraph.planner import (
    ClaudeCliPlanner,
    CodexCliPlanner,
    PlannerError,
    SubprocessClaudeRunner,
    SubprocessCodexRunner,
)


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


def subprocess_runner(
    *, agent: str = "codex", timeout: float = 1, fast: bool = False
) -> SubprocessCodexRunner:
    return SubprocessCodexRunner(
        command=("codex",),
        cwd=Path("/repo"),
        profile=PlannerProfile(agent=agent, model="gpt-test", strength="xhigh", fast=fast),
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
    assert 'service_tier="default"' in observed
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


def claude_runner(
    *, agent: str = "claude", timeout: float = 1, fast: bool = False
) -> SubprocessClaudeRunner:
    return SubprocessClaudeRunner(
        command=("claude",),
        cwd=Path("/repo"),
        profile=PlannerProfile(agent=agent, model="sonnet", strength="high", fast=fast),
        timeout_seconds=timeout,
    )


class FakeClaudeProcess:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.prompt = b""
        self.killed = False

    async def communicate(self, prompt: bytes | None = None) -> tuple[bytes, bytes]:
        self.prompt = prompt or b""
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


async def test_claude_runner_uses_print_plan_mode_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = json.dumps({"type": "result", "structured_output": json.loads(plan_json())})
    process = FakeClaudeProcess(stdout=envelope.encode())
    observed: tuple[str, ...] = ()

    async def create(*arguments: str, **kwargs: Any) -> FakeClaudeProcess:
        nonlocal observed
        observed = arguments
        assert kwargs["cwd"] == Path("/repo")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    output = await claude_runner().run("Plan this", {"type": "object"})
    plan = await ClaudeCliPlanner(FakeRunner(output)).plan("Ship work")

    assert plan.next_action == "propose_lanes"
    assert observed[:4] == ("claude", "-p", "--model", "sonnet")
    assert "--permission-mode" in observed
    assert "--no-session-persistence" in observed
    assert "--json-schema" in observed
    settings_index = observed.index("--settings")
    assert observed[settings_index + 1] == '{"fastMode":false}'
    assert process.prompt == b"Plan this"


async def test_fast_mode_maps_to_each_cli_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, ...]] = []

    async def create(*arguments: str, **kwargs: Any) -> FakeClaudeProcess | FakeProcess:
        observed.append(arguments)
        if arguments[0] == "claude":
            envelope = json.dumps({"type": "result", "structured_output": json.loads(plan_json())})
            return FakeClaudeProcess(stdout=envelope.encode())
        output_path = Path(arguments[arguments.index("--output-last-message") + 1])
        return FakeProcess(output_path)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    await subprocess_runner(fast=True).run("Plan", {})
    await claude_runner(fast=True).run("Plan", {})

    assert 'service_tier="priority"' in observed[0]
    settings_index = observed[1].index("--settings")
    assert observed[1][settings_index + 1] == '{"fastMode":true}'


async def test_claude_runner_accepts_result_string(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeClaudeProcess(stdout=json.dumps({"result": plan_json()}).encode())

    async def create(*arguments: str, **kwargs: Any) -> FakeClaudeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert json.loads(await claude_runner().run("Plan", {}))["next_action"] == "propose_lanes"


@pytest.mark.parametrize(
    ("process", "message"),
    [
        (FakeClaudeProcess(stdout=b"nope"), "invalid JSON"),
        (FakeClaudeProcess(stdout=b"[]"), "non-object"),
        (FakeClaudeProcess(stdout=b"{}"), "omitted structured_output"),
        (FakeClaudeProcess(stdout=b"", stderr=b"bad", returncode=2), "rc=2: bad"),
    ],
)
async def test_claude_runner_rejects_bad_results(
    monkeypatch: pytest.MonkeyPatch, process: FakeClaudeProcess, message: str
) -> None:
    async def create(*arguments: str, **kwargs: Any) -> FakeClaudeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(PlannerError, match=message):
        await claude_runner().run("Plan", {})


async def test_claude_runner_rejects_wrong_agent() -> None:
    with pytest.raises(PlannerError, match="must be claude"):
        await claude_runner(agent="codex").run("Plan", {})
