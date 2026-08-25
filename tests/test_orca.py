"""Orca JSON adapter contract tests."""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orkastrator.config import AgentProfile
from orkastrator.orca import (
    MAX_TASK_SPEC_BYTES,
    JsonObject,
    OrcaClient,
    OrcaError,
    OrcaOutcomeUnknown,
    SubprocessRunner,
)


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


async def test_create_task_rejects_specs_over_the_delivery_limit() -> None:
    runner = FakeRunner([])

    with pytest.raises(OrcaError, match="maximum"):
        await OrcaClient(runner).create_task("x" * (MAX_TASK_SPEC_BYTES + 1), [])

    assert runner.calls == []


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


async def test_worker_turns_returns_bounded_signatures_not_the_transcript() -> None:
    """The caller is asking whether turns differ, not what they said.

    Returning the text would put a stalled agent's whole conversation into the
    supervisor's, which is the cost this reading exists to avoid.
    """

    runner = FakeRunner(
        [
            {
                "ok": True,
                "result": {
                    "transcript": {
                        "messages": [
                            {"role": "user", "blocks": [{"type": "text", "text": "go"}]},
                            {
                                "role": "assistant",
                                "blocks": [
                                    {"type": "text", "text": "thinking"},
                                    {"type": "tool-call", "name": "exec", "input": "a" * 900},
                                ],
                            },
                            {
                                "role": "assistant",
                                "blocks": [{"type": "tool-call", "name": "read", "input": "f"}],
                            },
                        ]
                    }
                },
            }
        ]
    )

    turns = await OrcaClient(runner).worker_turns("dispatch-1")

    assert turns == [f"exec:{'a' * 400}", "read:f"]
    assert runner.calls[0][0:4] == ("orchestration", "worker-read", "--dispatch", "dispatch-1")


async def test_worker_turns_reads_a_worker_with_no_transcript_as_empty() -> None:
    runner = FakeRunner([{"ok": True, "result": {"transcript": None}}])
    assert await OrcaClient(runner).worker_turns("dispatch-1") == []


async def test_releasing_a_worker_closes_the_terminal_orkastrator_opened() -> None:
    """Releasing the Dispatch reclaims nothing when orkastrator started the agent.

    Orca calls such a terminal `external_terminal` and answers `processAction:
    none`, which is how a settled stage kept a whole agent process tree resident
    for the life of the run.
    """

    runner = FakeRunner([{"ok": True, "result": {}}, {"ok": True, "result": {}}])

    await OrcaClient(runner).release_worker("dispatch-1", "terminal-1")

    assert runner.calls == [
        ("orchestration", "worker-release", "--dispatch", "dispatch-1", "--json"),
        ("terminal", "close", "--terminal", "terminal-1", "--json"),
    ]


async def test_a_terminal_orca_no_longer_knows_is_already_closed() -> None:
    """A refused close is the outcome asked for, not a reason to retry forever."""

    runner = FakeRunner([{"ok": True, "result": {}}, {"ok": False, "error": {"code": "no_such"}}])

    await OrcaClient(runner).release_worker("dispatch-1", "terminal-1")

    assert len(runner.calls) == 2


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

    dispatch_id, worktree_id, _, _ = await client.start_worker(
        task_id="task-1",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id=None,
        orca_run_id="run-1",
        profile=profile(),
    )

    assert (dispatch_id, worktree_id) == ("dispatch-1", "repo::/tmp/issue-123")
    assert runner.calls[0] == (
        "orchestration",
        "worker-start",
        "--task",
        "task-1",
        "--run",
        "run-1",
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


async def test_create_worktree_pins_the_requested_base_before_worker_start() -> None:
    runner = FakeRunner(
        [{"ok": True, "result": {"worktree": {"id": "repo::/tmp/issue-123"}}}]
    )
    client = OrcaClient(runner)

    worktree_id, _ = await client.create_worktree(
        lane_name="issue-123-worker",
        repo_selector="id:repo",
        base_ref="main",
    )

    assert worktree_id == "repo::/tmp/issue-123"
    assert runner.calls == [
        (
            "worktree",
            "create",
            "--name",
            "issue-123-worker",
            "--no-parent",
            "--repo",
            "id:repo",
            "--setup",
            "run",
            "--base-branch",
            "main",
            "--json",
        )
    ]


async def test_start_worker_reuses_lane_worktree() -> None:
    runner = FakeRunner([{"ok": True, "result": {"dispatchId": "dispatch-2"}}])
    client = OrcaClient(runner)
    _, worktree_id, _, _ = await client.start_worker(
        task_id="task-2",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id="repo::/tmp/issue-123",
        orca_run_id="run-1",
        profile=profile(),
    )
    assert worktree_id == "repo::/tmp/issue-123"
    assert "id:repo::/tmp/issue-123" in runner.calls[0]


async def test_start_worker_rejects_a_reported_failure() -> None:
    runner = FakeRunner(
        [{"ok": True, "result": {"state": "failed", "dispatchId": "dispatch-2"}}]
    )

    with pytest.raises(OrcaError, match="worker start reported failure"):
        await OrcaClient(runner).start_worker(
            task_id="task-2",
            lane_name="issue-123",
            repo_selector="id:repo",
            worktree_id="repo::/tmp/issue-123",
            orca_run_id="run-1",
            profile=profile(),
        )


async def test_start_worker_does_not_require_a_reported_state() -> None:
    runner = FakeRunner([{"ok": True, "result": {"dispatchId": "dispatch-2"}}])

    dispatch_id, _, _, _ = await OrcaClient(runner).start_worker(
        task_id="task-2",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id="repo::/tmp/issue-123",
        orca_run_id="run-1",
        profile=profile(),
    )

    assert dispatch_id == "dispatch-2"


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
    _, worktree_id, _, _ = await client.start_worker(
        task_id="task-3",
        lane_name="finding-1-fixer-r1",
        repo_selector="id:repo",
        worktree_id=None,
        orca_run_id="run-1",
        profile=profile(),
        base_ref="b" * 40,
        parent_worktree_id="repo::/tmp/issue-123",
    )

    assert worktree_id == "repo::/tmp/finding-1"
    assert runner.calls[0][0:14] == (
        "orchestration",
        "worker-start",
        "--task",
        "task-3",
        "--run",
        "run-1",
        "--worktree",
        "new-child",
        "--repo",
        "id:repo",
        "--name",
        "finding-1-fixer-r1",
        "--setup",
        "run",
    )
    assert runner.calls[0][14:16] == ("--base-branch", "b" * 40)


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

    dispatch_id, worktree_id, terminal_handle, _ = await client.start_worker(
        task_id="task-1",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id=None,
        orca_run_id="run-1",
        profile=profile(fast=True),
    )

    assert (dispatch_id, worktree_id) == ("dispatch-1", "repo::/tmp/issue-123")
    # orkastrator opened this pane, so it is the one that has to close it later.
    assert terminal_handle == "terminal-1"
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
        "--run",
        "run-1",
        "--terminal",
        "terminal-1",
        "--worktree",
        "id:repo::/tmp/issue-123",
        # A frozen finding's spec is a large paste, and the default settle wait
        # expires on it while the agent is already reading.
        "--timeout-ms",
        "180000",
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

    _, worktree_id, _, _ = await client.start_worker(
        task_id="task-2",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id="repo::/tmp/issue-123",
        orca_run_id="run-1",
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
            orca_run_id="run-1",
            profile=profile(agent="omp", fast=True),
        )


async def test_failed_fast_worker_start_closes_its_terminal() -> None:
    runner = FakeRunner(
        [
            {"ok": True, "result": {"terminalHandle": "terminal-1"}},
            {"ok": True, "result": {"wait": {"satisfied": True}}},
            {
                "ok": True,
                "result": {"state": "failed", "dispatchId": "dispatch-1"},
            },
            {"ok": True, "result": {}},
        ]
    )

    with pytest.raises(OrcaError, match="worker start reported failure"):
        await OrcaClient(runner).start_worker(
            task_id="task-1",
            lane_name="issue-123",
            repo_selector="id:repo",
            worktree_id="repo::/tmp/issue-123",
            orca_run_id="run-1",
            profile=profile(fast=True),
        )

    assert runner.calls[-1] == (
        "terminal",
        "close",
        "--terminal",
        "terminal-1",
        "--json",
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


async def test_subprocess_runner_reports_the_orca_error_written_to_stdout(
    tmp_path: Path,
) -> None:
    script = tmp_path / "orca-fake"
    script.write_text(
        "#!/bin/sh\n"
        'printf \'{"ok":false,"error":{"code":"selector_not_found",'
        '"message":"selector_not_found"}}\'\n'
        "exit 1\n"
    )
    script.chmod(0o755)
    runner = SubprocessRunner(command=(str(script),), cwd=tmp_path, timeout_seconds=10)

    with pytest.raises(OrcaError) as failure:
        await runner.run(["orchestration", "worker-start"])

    assert "selector_not_found" in str(failure.value)


async def test_ok_surfaces_the_typed_error_code() -> None:
    client = OrcaClient(
        FakeRunner([{"ok": False, "error": {"code": "consumer_fenced", "message": "fenced"}}])
    )

    with pytest.raises(OrcaError) as failure:
        await client.status()

    assert "consumer_fenced: fenced" in str(failure.value)


async def test_use_run_binds_the_coordinator_terminal() -> None:
    runner = FakeRunner([{"ok": True, "result": {}}])

    await OrcaClient(runner).use_run("run-1")

    assert runner.calls[0] == ("orchestration", "run-use", "--id", "run-1", "--json")


async def test_messages_returns_only_the_requested_run() -> None:
    runner = FakeRunner(
        [
            {
                "ok": True,
                "result": {
                    "messages": [
                        {"id": "msg-1", "run_id": "run-1", "type": "question"},
                        {"id": "msg-2", "run_id": "run-2", "type": "question"},
                    ]
                },
            }
        ]
    )
    client = OrcaClient(runner)

    messages = await client.messages("run-1")

    assert [message["id"] for message in messages] == ["msg-1"]
    assert runner.calls[0] == ("orchestration", "inbox", "--limit", "200", "--json")


async def test_messages_rejects_a_response_without_a_message_list() -> None:
    client = OrcaClient(FakeRunner([{"ok": True, "result": {"messages": "none"}}]))

    with pytest.raises(OrcaError, match=r"omitted result\.messages"):
        await client.messages("run-1")


def test_a_reply_is_sent_from_the_handle_the_run_itself_names() -> None:
    """Orca refuses a reply from a terminal it cannot pin to a stable pane.

    The handle it accepts is the Run's own `coordinator_handle`, and it is
    already in `run-show`. Discovering it by trying handles until one is
    accepted is how it was found the first time, and is not a procedure.
    """

    runner = FakeRunner(
        [
            {"ok": True, "result": {"run": {"id": "orca-run-1", "coordinator_handle": "term_abc"}}},
            {"ok": True, "result": {"message": {"id": "msg-2"}}},
        ]
    )

    asyncio.run(OrcaClient(runner).reply("orca-run-1", "msg-1", "do the thing"))

    assert runner.calls[0] == ("orchestration", "run-show", "--id", "orca-run-1", "--json")
    assert runner.calls[1] == (
        "orchestration",
        "reply",
        "--id",
        "msg-1",
        "--run",
        "orca-run-1",
        "--from",
        "term_abc",
        "--body",
        "do the thing",
        "--json",
    )


def test_a_run_without_a_coordinator_handle_is_an_error_not_an_empty_from() -> None:
    """An empty --from is accepted by argv and rejected by Orca, far from the cause."""

    runner = FakeRunner([{"ok": True, "result": {"run": {"id": "orca-run-1"}}}])

    with pytest.raises(OrcaError, match="coordinator_handle"):
        asyncio.run(OrcaClient(runner).coordinator_handle("orca-run-1"))


async def test_worker_terminals_maps_dispatches_to_the_panes_orca_attached() -> None:
    runner = FakeRunner(
        [
            {
                "ok": True,
                "result": {
                    "workers": [
                        {"dispatchId": "ctx-1", "agentTerminalHandle": "term-1"},
                        {"dispatchId": "ctx-2", "agentTerminalHandle": "term-2"},
                        # A dispatch Orca has no terminal for is not an entry.
                        {"dispatchId": "ctx-3", "agentTerminalHandle": None},
                        {"dispatchId": "ctx-4", "agentTerminalHandle": ""},
                    ]
                },
            }
        ]
    )

    attached = await OrcaClient(runner).worker_terminals("run-1")

    assert attached == {"ctx-1": "term-1", "ctx-2": "term-2"}
    assert runner.calls == [
        ("orchestration", "worker-list", "--run", "run-1", "--json"),
    ]


async def test_open_terminals_reports_whether_it_saw_the_whole_listing() -> None:
    listing = await OrcaClient(
        FakeRunner(
            [
                {
                    "ok": True,
                    "result": {
                        "terminals": [{"handle": "term-1"}, {"handle": "term-2"}],
                        "truncated": True,
                    },
                }
            ]
        )
    ).open_terminals()

    assert listing.handles == frozenset({"term-1", "term-2"})
    # Truncated means a handle missing from this set may still be an open pane.
    assert listing.complete is False


async def test_an_untruncated_listing_is_the_whole_set() -> None:
    listing = await OrcaClient(
        FakeRunner([{"ok": True, "result": {"terminals": [{"handle": "term-1"}]}}])
    ).open_terminals()

    assert listing.complete is True


async def test_closing_a_terminal_orca_has_forgotten_is_already_the_outcome() -> None:
    runner = FakeRunner([{"ok": False, "error": {"message": "unknown terminal"}}])

    await OrcaClient(runner).close_terminal("term-gone")

    assert runner.calls == [("terminal", "close", "--terminal", "term-gone", "--json")]


async def test_unread_direction_is_listed_without_marking_it_read() -> None:
    """`check` consumes; this must not, or reading the mailbox destroys the evidence."""

    runner = FakeRunner(
        [
            {
                "ok": True,
                "result": {
                    "messages": [
                        {"sequence": 281, "read": 0, "subject": "decision: upload target"},
                        {"sequence": 259, "read": 1, "subject": "Re: Question"},
                        {"sequence": 262, "read": 0, "subject": "boundary corrections"},
                    ]
                },
            }
        ]
    )

    unread = await OrcaClient(runner).unread_messages("ctx-1")

    # Read messages are dropped, and what is left is in the order it was sent.
    assert [(message.sequence, message.subject) for message in unread] == [
        (262, "boundary corrections"),
        (281, "decision: upload target"),
    ]
    assert runner.calls == [
        (
            "orchestration",
            "inbox",
            "--terminal",
            "dispatch:ctx-1",
            "--limit",
            "20",
            "--json",
        ),
    ]


async def test_a_mailbox_orca_describes_without_messages_is_empty_not_an_error() -> None:
    unread = await OrcaClient(FakeRunner([{"ok": True, "result": {}}])).unread_messages("ctx-1")

    assert unread == []


async def test_a_terminal_that_never_went_idle_refuses_the_start_by_its_reason() -> None:
    """An agent parked on its own prompt must fail attributably, not later and blind.

    Codex asks whether it trusts an unfamiliar repository root. Until somebody
    answers, its TUI is not reading, so `worker-start` fails against it with an
    error that describes the dispatch rather than the prompt holding it up.
    """

    runner = FakeRunner(
        [
            {"ok": True, "result": {"worktree": {"id": "repo::/tmp/issue-123"}}},
            {"ok": True, "result": {"terminal": {"handle": "terminal-1"}}},
            {
                "ok": True,
                "result": {
                    "wait": {
                        "handle": "terminal-1",
                        "condition": "tui-idle",
                        "satisfied": False,
                        "status": "running",
                        "blockedReason": "codex-interactive-prompt",
                    }
                },
            },
            {"ok": True, "result": {}},
            {"ok": True, "result": {}},
        ]
    )

    with pytest.raises(OrcaError) as failure:
        await OrcaClient(runner).start_worker(
            task_id="task-1",
            lane_name="issue-123",
            repo_selector="id:repo",
            worktree_id=None,
            orca_run_id="run-1",
            profile=profile(fast=True),
        )

    assert "codex-interactive-prompt" in str(failure.value)
    # The start never happened, so the checkout it created has no stage to be
    # reclaimed through. Failing without removing it costs one worktree per
    # reconcile for as long as the stage stays unstartable.
    assert runner.calls[-1] == (
        "worktree",
        "rm",
        "--worktree",
        "id:repo::/tmp/issue-123",
        "--force",
        "--json",
    )


async def test_a_failed_start_leaves_a_caller_supplied_worktree_alone() -> None:
    """Only a checkout this start created is this start's to remove."""

    runner = FakeRunner(
        [
            {"ok": True, "result": {"terminal": {"handle": "terminal-1"}}},
            {
                "ok": True,
                "result": {"wait": {"satisfied": False, "blockedReason": "login-required"}},
            },
            {"ok": True, "result": {}},
        ]
    )

    with pytest.raises(OrcaError):
        await OrcaClient(runner).start_worker(
            task_id="task-1",
            lane_name="issue-123",
            repo_selector="id:repo",
            worktree_id="repo::/tmp/existing",
            orca_run_id="run-1",
            profile=profile(fast=True),
        )

    assert all(call[:2] != ("worktree", "rm") for call in runner.calls)


async def test_cleanup_failure_does_not_replace_the_start_failure() -> None:
    """The reason the start failed outranks anything cleanup has to say."""

    runner = FakeRunner(
        [
            {"ok": True, "result": {"worktree": {"id": "repo::/tmp/issue-123"}}},
            {"ok": True, "result": {"terminal": {"handle": "terminal-1"}}},
            {
                "ok": True,
                "result": {"wait": {"satisfied": False, "blockedReason": "codex-trust-prompt"}},
            },
            {"ok": False, "error": {"code": "terminal_busy", "message": "in use"}},
            {"ok": False, "error": {"code": "worktree_busy", "message": "in use"}},
        ]
    )

    with pytest.raises(OrcaError) as failure:
        await OrcaClient(runner).start_worker(
            task_id="task-1",
            lane_name="issue-123",
            repo_selector="id:repo",
            worktree_id=None,
            orca_run_id="run-1",
            profile=profile(fast=True),
        )

    assert "codex-trust-prompt" in str(failure.value)
    assert "worktree_busy" not in str(failure.value)


async def test_worker_start_names_its_run_so_a_drifted_binding_cannot_misroute_it() -> None:
    """`use_run` binds shared state, so the start has to say which run it means.

    Two supervisors driving concurrently take turns binding the coordinator
    terminal. Whichever bound it last decides where an unqualified `worker-start`
    looks for the task, and the loser gets `task_not_found` against a run it never
    asked about. Both the fast and the supervised path name the run explicitly.
    """

    runner = FakeRunner(
        [
            {"ok": True, "result": {"worktree": {"id": "repo::/tmp/issue-123"}}},
            {"ok": True, "result": {"terminal": {"handle": "terminal-1"}}},
            {"ok": True, "result": {"wait": {"satisfied": True}}},
            {"ok": True, "result": {"dispatchId": "dispatch-1"}},
        ]
    )

    await OrcaClient(runner).start_worker(
        task_id="task-1",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id=None,
        orca_run_id="run_8a5347dc0ec9",
        profile=profile(fast=True),
    )

    start = next(call for call in runner.calls if call[:2] == ("orchestration", "worker-start"))
    assert "--run" in start
    assert start[start.index("--run") + 1] == "run_8a5347dc0ec9"

    supervised = FakeRunner([{"ok": True, "result": {"dispatchId": "dispatch-2"}}])
    await OrcaClient(supervised).start_worker(
        task_id="task-2",
        lane_name="issue-124",
        repo_selector="id:repo",
        worktree_id="repo::/tmp/issue-124",
        orca_run_id="run_8a5347dc0ec9",
        profile=profile(),
    )

    assert supervised.calls[0][:2] == ("orchestration", "worker-start")
    assert supervised.calls[0][supervised.calls[0].index("--run") + 1] == "run_8a5347dc0ec9"


class StallingRunner:
    """Answer the fast-start sequence, but fail `worker-start` the way Orca does."""

    def __init__(self, *, dispatch_id: str | None) -> None:
        self.dispatch_id = dispatch_id
        self.calls: list[tuple[str, ...]] = []

    async def run(self, arguments: Sequence[str]) -> JsonObject:
        self.calls.append(tuple(arguments))
        head = arguments[0:2]
        if head == ("worktree", "create"):
            return {"ok": True, "result": {"worktreeId": "repo::/tmp/issue-123"}}
        if head == ("terminal", "create"):
            return {"ok": True, "result": {"terminalHandle": "terminal-1"}}
        if head == ("terminal", "wait"):
            return {"ok": True, "result": {"wait": {"satisfied": True}}}
        if head == ("orchestration", "worker-start"):
            result: JsonObject = {
                "state": "failed",
                "failedStage": "dispatch_input",
                "lastError": "agent_prompt_stalled",
            }
            if self.dispatch_id is not None:
                result["dispatchId"] = self.dispatch_id
            raise OrcaOutcomeUnknown("stalled", {"ok": True, "result": result})
        return {"ok": True, "result": {}}


async def test_subprocess_runner_surfaces_unresolved_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit that still answers `ok` is an outcome, not a refusal."""

    payload = {
        "ok": True,
        "result": {
            "dispatchId": "dispatch-9",
            "state": "failed",
            "failedStage": "dispatch_input",
            "lastError": "agent_prompt_stalled",
        },
    }
    process = FakeProcess(stdout=json.dumps(payload).encode(), returncode=1)

    async def create(*arguments: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(OrcaOutcomeUnknown) as caught:
        await SubprocessRunner(("orca-ide",), Path("/tmp"), 1).run(("status", "--json"))

    # The old path read `error`, found none, and raised with an empty detail.
    assert "agent_prompt_stalled" in str(caught.value)
    assert "dispatch_input" in str(caught.value)
    assert caught.value.payload == payload


async def test_fast_worker_adopts_a_stalled_start() -> None:
    """A stalled prompt keeps its Dispatch and its checkout."""

    runner = StallingRunner(dispatch_id="dispatch-7")
    client = OrcaClient(runner)

    dispatch_id, worktree_id, terminal_handle, _ = await client.start_worker(
        task_id="task-1",
        lane_name="issue-123",
        repo_selector="id:repo",
        worktree_id=None,
        orca_run_id="run-1",
        profile=profile(fast=True),
    )

    assert (dispatch_id, worktree_id, terminal_handle) == (
        "dispatch-7",
        "repo::/tmp/issue-123",
        "terminal-1",
    )
    assert not [call for call in runner.calls if call[0:2] == ("worktree", "rm")]


async def test_fast_worker_discards_a_stall_that_names_no_dispatch() -> None:
    """Without a Dispatch there is nothing to adopt, so the checkout goes back."""

    runner = StallingRunner(dispatch_id=None)
    client = OrcaClient(runner)

    with pytest.raises(OrcaOutcomeUnknown):
        await client.start_worker(
            task_id="task-1",
            lane_name="issue-123",
            repo_selector="id:repo",
            worktree_id=None,
            orca_run_id="run-1",
            profile=profile(fast=True),
        )

    assert runner.calls[-1][0:2] == ("worktree", "rm")
