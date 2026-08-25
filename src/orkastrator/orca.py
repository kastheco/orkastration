"""Narrow, JSON-only access to the public Orca CLI."""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from pydantic import TypeAdapter, ValidationError

from orkastrator.config import AgentProfile
from orkastrator.models import OrcaSnapshot, OrcaWorktree

JsonObject = dict[str, object]

# Linux limits one execve argument to 131,072 bytes. Keep a margin for
# platform differences while still allowing the normal frozen-diff budget to
# fit in the task spec.
MAX_TASK_SPEC_BYTES = 120_000

# `worker-start` waits for the agent TUI to settle around the pasted task spec
# before it calls the Dispatch ready. A frozen finding carries its evidence and
# its schema, so that paste is routinely tens of kilobytes and the default wait
# expires while the agent is already reading it. Orca then records the Task as
# failed, which no amount of recovery on this side can talk back out of. Wait
# long enough that only a real stall reaches that state.
WORKER_START_TIMEOUT_MS = 180_000


class OrcaError(RuntimeError):
    """Raised when an Orca CLI request fails or violates its JSON contract."""


class OrcaTimeout(OrcaError):
    """Raised when an Orca request produced no answer, either way.

    Separate from `OrcaError` because the two mean opposite things to a caller
    that already changed state. A refused command is known not to have run. A
    timed-out one has no outcome at all: `worker-start` can create the Dispatch
    and then lose the reply, so treating it as a failure and handing the slot
    back races the recovery that would have adopted it. Callers that can tell
    the difference should; this is what lets them.
    """


class OrcaOutcomeUnknown(OrcaError):
    """Raised when Orca exits non-zero but still answers with a typed envelope.

    `worker-start` exits 1 for both `failed` and `outcome_unknown`, and its own
    documentation says the JSON still carries the stage, the effects, and the
    Dispatch it created. A stall reported at `dispatch_input` is the clearest
    case: Orca stopped waiting for the TUI to settle, but the agent is already
    holding the prompt and running. Treating that envelope as a refused command
    threw away a live worker and, worse, the checkout its commit lived in.

    The payload is carried so a caller that can adopt the Dispatch does, instead
    of guessing from an error string.
    """

    def __init__(self, message: str, payload: JsonObject) -> None:
        super().__init__(message)
        self.payload = payload


class CommandRunner(Protocol):
    """Execute one Orca command without a shell."""

    async def run(self, arguments: Sequence[str]) -> JsonObject:
        """Return the parsed JSON response."""
        ...


@dataclass(frozen=True, slots=True)
class SubprocessRunner:
    """Async subprocess runner for a resolved Orca CLI command."""

    command: tuple[str, ...]
    cwd: Path
    timeout_seconds: float

    async def run(self, arguments: Sequence[str]) -> JsonObject:
        """Run Orca with fixed argv and parse its JSON response."""

        process = await asyncio.create_subprocess_exec(
            *self.command,
            *arguments,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise OrcaTimeout(f"Orca command timed out after {self.timeout_seconds:g}s") from exc
        if process.returncode != 0:
            # Orca reports command failures as a JSON envelope on stdout and leaves
            # stderr empty, so stdout is the only place the typed error code lives.
            envelope = _envelope(stdout)
            detail = _failure_detail(stdout) or stderr.decode(errors="replace").strip()
            message = f"Orca command failed with rc={process.returncode}: {detail[:2_000]}"
            # A non-zero exit that still reports `ok` is Orca describing an
            # outcome, not refusing the command. Hand the envelope back so the
            # caller can read what actually happened; the empty detail this used
            # to produce is what made a stalled prompt look like a dead lane.
            if envelope is not None and envelope.get("ok") is True:
                raise OrcaOutcomeUnknown(
                    f"Orca command reported an unresolved outcome with"
                    f" rc={process.returncode}: {_outcome_detail(envelope)[:2_000]}",
                    envelope,
                )
            # Orca times out internally too, and reports it as its own typed code
            # rather than by making this call hang. Same undefined outcome.
            if detail.startswith("timeout"):
                raise OrcaTimeout(message)
            raise OrcaError(message)
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise OrcaError("Orca returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise OrcaError("Orca response must be a JSON object")
        return cast(JsonObject, value)


@dataclass(frozen=True, slots=True)
class UnreadMessage:
    """One message sitting in a dispatch's mailbox that nobody has read."""

    sequence: int
    subject: str


@dataclass(frozen=True, slots=True)
class OpenTerminals:
    """The panes Orca currently lists, and whether that listing was the whole set."""

    handles: frozenset[str]
    complete: bool


class OrcaClient:
    """Typed read and narrowly gated mutation operations."""

    def __init__(self, runner: CommandRunner):
        self._runner = runner

    async def status(self) -> JsonObject:
        """Return Orca runtime status."""

        return await self._ok("status", "--json")

    async def snapshot(self) -> OrcaSnapshot:
        """Return the stable worktree subset used by planning and reconciliation."""

        response = await self._ok("worktree", "ps", "--json")
        result = _object(response.get("result"), "result")
        raw_worktrees = result.get("worktrees")
        if not isinstance(raw_worktrees, list):
            raise OrcaError("Orca worktree response omitted result.worktrees")
        try:
            worktrees = TypeAdapter(list[OrcaWorktree]).validate_python(raw_worktrees)
        except ValidationError as exc:
            raise OrcaError("Orca worktree response did not match the expected schema") from exc
        return OrcaSnapshot(worktrees=worktrees)

    async def create_run(self, objective: str) -> tuple[str, JsonObject]:
        """Create and bind one Orca orchestration Run."""

        response = await self._ok("orchestration", "run-create", "--objective", objective, "--json")
        return _orchestration_id(response, "run", "runId"), response

    async def use_run(self, orca_run_id: str) -> JsonObject:
        """Bind this coordinator terminal to the Run that owns the graph's Tasks."""

        return await self._ok("orchestration", "run-use", "--id", orca_run_id, "--json")

    async def runs(self) -> list[JsonObject]:
        """List Orca Runs so acceptance can recover a create-before-bind crash."""

        response = await self._ok("orchestration", "run-list", "--json")
        result = _object(response.get("result"), "result")
        runs = result.get("runs")
        if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
            raise OrcaError("Orca run-list response omitted result.runs")
        return cast(list[JsonObject], runs)

    async def create_task(self, spec: str, dependencies: list[str]) -> tuple[str, JsonObject]:
        """Create one Orca Task with optional task-ID dependencies."""

        spec_bytes = len(spec.encode())
        if spec_bytes > MAX_TASK_SPEC_BYTES:
            raise OrcaError(
                f"Orca task spec is {spec_bytes} bytes; maximum is {MAX_TASK_SPEC_BYTES}"
            )
        arguments = ["orchestration", "task-create", "--spec", spec]
        if dependencies:
            arguments.extend(("--deps", json.dumps(dependencies, separators=(",", ":"))))
        arguments.append("--json")
        response = await self._ok(*arguments)
        return _orchestration_id(response, "task", "taskId"), response

    async def tasks(self, orca_run_id: str) -> list[JsonObject]:
        """Read authoritative Task state for one Orca Run."""

        response = await self._ok("orchestration", "task-list", "--run", orca_run_id, "--json")
        result = _object(response.get("result"), "result")
        tasks = result.get("tasks")
        if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
            raise OrcaError("Orca task-list response omitted result.tasks")
        return cast(list[JsonObject], tasks)

    async def messages(self, orca_run_id: str, limit: int = 200) -> list[JsonObject]:
        """Read the Run's message log so blocked agents cannot go unnoticed.

        Orca already types a stage's out-of-band traffic: `question` when an agent
        is waiting on a decision, `escalation` when it gave up waiting. Reconciling
        Tasks alone reports such a stage as healthily dispatched forever.
        """

        response = await self._ok("orchestration", "inbox", "--limit", str(limit), "--json")
        result = _object(response.get("result"), "result")
        messages = result.get("messages")
        if not isinstance(messages, list) or not all(isinstance(row, dict) for row in messages):
            raise OrcaError("Orca inbox response omitted result.messages")
        return [
            cast(JsonObject, row)
            for row in messages
            if cast(JsonObject, row).get("run_id") == orca_run_id
        ]

    async def coordinator_handle(self, orca_run_id: str) -> str:
        """Resolve the handle a supervisor reply has to be sent from.

        Orca refuses `orchestration reply` from any terminal it cannot pin to a
        stable pane, and the handle that satisfies it is the Run's own
        `coordinator_handle`. That field is already in `run-show`, so read it
        rather than leaving an operator to discover it by trying handles until
        one is accepted - which is how it was found the first time.
        """

        response = await self._ok("orchestration", "run-show", "--id", orca_run_id, "--json")
        run = _object(_object(response.get("result"), "result").get("run"), "result.run")
        handle = run.get("coordinator_handle")
        if not isinstance(handle, str) or not handle:
            raise OrcaError("Orca run-show response omitted result.run.coordinator_handle")
        return handle

    async def reply(self, orca_run_id: str, message_id: str, body: str) -> JsonObject:
        """Answer one blocked agent on the Run that owns its dispatch."""

        return await self._ok(
            "orchestration",
            "reply",
            "--id",
            message_id,
            "--run",
            orca_run_id,
            "--from",
            await self.coordinator_handle(orca_run_id),
            "--body",
            body,
            "--json",
        )

    async def worker_dispatch(self, task_id: str) -> tuple[str, str | None] | None:
        """Recover the supervised Dispatch and worktree already attached to a Task."""

        response = await self._ok("orchestration", "dispatch-show", "--task", task_id, "--json")
        result = _object(response.get("result"), "result")
        raw_dispatch = result.get("dispatch")
        if raw_dispatch is None:
            return None
        dispatch = _object(raw_dispatch, "result.dispatch")
        dispatch_id = _required_string(dispatch, "id")
        worker_response = await self._ok(
            "orchestration", "worker-show", "--dispatch", dispatch_id, "--json"
        )
        worker_result = _object(worker_response.get("result"), "result")
        worker = _object(worker_result.get("worker"), "result.worker")
        worktree_id = worker.get("worktree_id")
        if worktree_id is not None and not isinstance(worktree_id, str):
            raise OrcaError("Orca worker-show returned an invalid worktree_id")
        return dispatch_id, worktree_id

    async def start_worker(
        self,
        *,
        task_id: str,
        lane_name: str,
        repo_selector: str,
        worktree_id: str | None,
        profile: AgentProfile,
        orca_run_id: str,
        base_ref: str | None = None,
        parent_worktree_id: str | None = None,
    ) -> tuple[str, str, str | None, JsonObject]:
        """Start one supervised worker with an explicit model and effort.

        The third element is a terminal handle only when orkastrator created the
        terminal itself. Orca refuses to close a terminal it did not open, so
        whoever opened one has to be the one that closes it; recording the handle
        here is what lets `release_worker` reclaim it later without guessing at
        panes that belong to the owner.
        """

        if profile.fast:
            return await self._start_fast_worker(
                task_id=task_id,
                lane_name=lane_name,
                repo_selector=repo_selector,
                worktree_id=worktree_id,
                base_ref=base_ref,
                parent_worktree_id=parent_worktree_id,
                profile=profile,
                orca_run_id=orca_run_id,
            )

        # Naming the run keeps the start independent of whichever run the
        # coordinator terminal happens to be bound to when it fires.
        arguments = ["orchestration", "worker-start", "--task", task_id, "--run", orca_run_id]
        if worktree_id is None:
            placement = "new-child" if parent_worktree_id is not None else "new-top-level"
            arguments.extend(
                (
                    "--worktree",
                    placement,
                    "--repo",
                    repo_selector,
                    "--name",
                    lane_name,
                    "--setup",
                    "run",
                )
            )
            if base_ref is not None:
                arguments.extend(("--base-branch", base_ref))
        else:
            arguments.extend(("--worktree", f"id:{worktree_id}"))
        arguments.extend(
            (
                "--agent",
                profile.agent,
                "--model",
                profile.model,
                "--effort",
                profile.strength,
                "--json",
            )
        )
        response = await self._ok(*arguments)
        _require_start_succeeded(response)
        dispatch_id = _required_recursive_string(response, "dispatchId")
        resolved_worktree = worktree_id or _required_recursive_string(
            response, "worktreeId", "workspaceId"
        )
        # Orca opened this terminal, so Orca owns tearing it down: worker-release
        # is enough and no handle needs carrying.
        return dispatch_id, resolved_worktree, None, response

    async def create_worktree(
        self,
        *,
        lane_name: str,
        repo_selector: str,
        base_ref: str | None = None,
        parent_worktree_id: str | None = None,
    ) -> tuple[str, JsonObject]:
        """Create a checkout before any worker can mutate it."""

        arguments = ["worktree", "create", "--name", lane_name]
        if parent_worktree_id is None:
            arguments.append("--no-parent")
        else:
            arguments.extend(("--parent-worktree", f"id:{parent_worktree_id}"))
        arguments.extend(("--repo", repo_selector, "--setup", "run"))
        if base_ref is not None:
            arguments.extend(("--base-branch", base_ref))
        arguments.append("--json")
        response = await self._ok(*arguments)
        return _worktree_id(response), response

    async def _start_fast_worker(
        self,
        *,
        task_id: str,
        lane_name: str,
        repo_selector: str,
        worktree_id: str | None,
        base_ref: str | None,
        parent_worktree_id: str | None,
        profile: AgentProfile,
        orca_run_id: str,
    ) -> tuple[str, str, str | None, JsonObject]:
        """Launch fast provider argv, then attach it to a supervised Dispatch."""

        resolved_worktree = worktree_id
        created_worktree: str | None = None
        if resolved_worktree is None:
            resolved_worktree, _ = await self.create_worktree(
                lane_name=lane_name,
                repo_selector=repo_selector,
                base_ref=base_ref,
                parent_worktree_id=parent_worktree_id,
            )
            created_worktree = resolved_worktree

        # A start that fails after this point has no stage to record the checkout
        # against, so nothing else will ever reclaim it. The caller retries the
        # stage every reconcile, which turns one unstartable stage into one
        # abandoned worktree per tick until somebody notices the disk.
        terminal_handle: str | None = None
        try:
            terminal_response = await self._ok(
                "terminal",
                "create",
                "--worktree",
                f"id:{resolved_worktree}",
                "--title",
                f"{lane_name}-{profile.agent}-fast",
                "--command",
                shlex.join(_fast_agent_command(profile)),
                "--json",
            )
            terminal_handle = _required_recursive_string(
                terminal_response, "terminalHandle", "handle"
            )
            wait_response = await self._ok(
                "terminal",
                "wait",
                "--terminal",
                terminal_handle,
                "--for",
                "tui-idle",
                "--timeout-ms",
                "20000",
                "--json",
            )
            # `ok` here only says the wait ran, not that it was satisfied. An agent
            # parked on its own prompt - an untrusted directory, an expired login -
            # reports `satisfied: false` and a reason, and every command after this
            # one then fails against a TUI that is not listening. Read the reason
            # while it is still attributable instead of surfacing whatever the next
            # call happens to return.
            _require_idle(wait_response, terminal_handle)
            # worker-start defaults its worktree selector to the calling terminal's own
            # checkout. orkastrator drives Orca from a floating coordinator terminal that
            # has none, so the placement has to be named explicitly even when --terminal
            # already identifies the agent pane.
            try:
                response = await self._ok(
                    "orchestration",
                    "worker-start",
                    "--task",
                    task_id,
                    "--run",
                    orca_run_id,
                    "--terminal",
                    terminal_handle,
                    "--worktree",
                    f"id:{resolved_worktree}",
                    "--timeout-ms",
                    str(WORKER_START_TIMEOUT_MS),
                    "--json",
                )
            except OrcaOutcomeUnknown as exc:
                # Orca gave up waiting for the TUI to settle around a large
                # prompt and called the start failed. The Dispatch it names is
                # still real and the agent is still holding the prompt, so adopt
                # it and let reconcile decide from the Task, which is the only
                # place a genuine stall stays visible. Discarding here is what
                # turned one slow paste into a dead lane and a deleted checkout.
                response = exc.payload
                if _optional_recursive_string(response, "dispatchId") is None:
                    raise
            else:
                _require_start_succeeded(response)
            dispatch_id = _required_recursive_string(response, "dispatchId")
        except BaseException:
            if terminal_handle is not None:
                await self.close_terminal(terminal_handle)
            if created_worktree is not None:
                await self._discard_worktree(created_worktree)
            raise
        return dispatch_id, resolved_worktree, terminal_handle, response

    async def _discard_worktree(self, worktree_id: str) -> None:
        """Drop a checkout this start created and will never hand to a stage.

        Best effort on purpose: the caller is already raising the failure that
        matters, and losing a worktree is not worth replacing that failure with
        a cleanup error nobody asked about.
        """

        try:
            await self._ok("worktree", "rm", "--worktree", f"id:{worktree_id}", "--force", "--json")
        except (OrcaError, OrcaTimeout):
            return

    async def worker_turns(self, dispatch_id: str, limit: int = 50) -> list[str]:
        """Signatures of the recent assistant tool calls on one dispatched worker.

        One string per turn, `tool:input`, bounded. This is deliberately not the
        transcript: the caller is deciding whether a stage is making progress,
        and for that question the only thing that matters is whether consecutive
        turns are distinguishable from each other. Returning the text instead
        would put a stalled agent's whole conversation into the supervisor's.
        """

        response = await self._ok(
            "orchestration",
            "worker-read",
            "--dispatch",
            dispatch_id,
            "--limit",
            str(limit),
            "--json",
        )
        result = _object(response.get("result"), "result")
        transcript = result.get("transcript")
        if not isinstance(transcript, dict):
            return []
        messages = transcript.get("messages")
        if not isinstance(messages, list):
            return []
        turns: list[str] = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            blocks = message.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool-call":
                    continue
                name = str(block.get("name") or "?")
                turns.append(f"{name}:{str(block.get('input') or '')[:400]}")
        return turns

    async def release_worker(
        self, dispatch_id: str, terminal_handle: str | None = None
    ) -> JsonObject:
        """Release a settled Dispatch, and close the terminal orkastrator opened for it.

        `worker-release` alone reclaims nothing when orkastrator started the agent
        itself: Orca reports such a terminal as `external_terminal` and answers
        `processAction: none`, leaving the whole agent process tree resident for
        the life of the run. Passing the handle this client recorded at start
        closes the exact pane it opened, which is the only one it may close.
        """

        response = await self._ok(
            "orchestration", "worker-release", "--dispatch", dispatch_id, "--json"
        )
        if terminal_handle is None:
            return response
        # A handle Orca no longer knows is a handle that is already closed, which
        # is the outcome asked for. Raising here would park the stage unreleased
        # forever and retry the same refusal every tick.
        with suppress(OrcaError):
            await self._ok("terminal", "close", "--terminal", terminal_handle, "--json")
        return response

    async def worker_terminals(self, orca_run_id: str) -> dict[str, str]:
        """Map every Dispatch in one Orca Run to the agent terminal it is attached to.

        Orca is the authority on this and orkastrator's own ledger is not: a
        stage dispatched before the handle was recorded, or by a supervisor that
        died between opening the terminal and writing the row, has no local
        record at all. What makes reading it back safe is that orkastrator never
        attaches a worker to a terminal it did not create, so a handle on one of
        its own dispatches is unambiguously its own pane.
        """

        response = await self._ok("orchestration", "worker-list", "--run", orca_run_id, "--json")
        result = _object(response.get("result"), "result")
        workers = result.get("workers")
        if not isinstance(workers, list):
            return {}
        attached: dict[str, str] = {}
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            dispatch_id = worker.get("dispatchId")
            handle = worker.get("agentTerminalHandle")
            if isinstance(dispatch_id, str) and isinstance(handle, str) and handle:
                attached[dispatch_id] = handle
        return attached

    async def open_terminals(self) -> OpenTerminals:
        """Handles Orca still knows about, so a closed pane is never named twice.

        `complete` carries whether Orca truncated the listing. A handle missing
        from a truncated listing is not necessarily a closed pane, and the caller
        skips what it cannot see - which is safe but silent, and a sweep that
        quietly reclaims half of what it should is worse than one that says so.
        """

        response = await self._ok("terminal", "list", "--json")
        result = _object(response.get("result"), "result")
        terminals = result.get("terminals")
        handles = (
            frozenset(
                terminal["handle"]
                for terminal in terminals
                if isinstance(terminal, dict) and isinstance(terminal.get("handle"), str)
            )
            if isinstance(terminals, list)
            else frozenset()
        )
        return OpenTerminals(handles=handles, complete=result.get("truncated") is not True)

    async def unread_messages(self, dispatch_id: str, limit: int = 20) -> list[UnreadMessage]:
        """Messages addressed to one dispatch that its agent has not read.

        Reads the mailbox rather than consuming it, so this is safe to run
        against a live run: `check` marks messages read, and doing that from the
        supervisor would hide the very thing being looked for.

        The subject is carried and the body is not. The question this answers is
        whether direction arrived, not what it said - and a worker that has
        stopped reading has usually stopped reading everything, so the useful
        output is a short list of subjects rather than one message in full.
        """

        response = await self._ok(
            "orchestration",
            "inbox",
            "--terminal",
            f"dispatch:{dispatch_id}",
            "--limit",
            str(limit),
            "--json",
        )
        result = _object(response.get("result"), "result")
        messages = result.get("messages")
        if not isinstance(messages, list):
            return []
        unread: list[UnreadMessage] = []
        for message in messages:
            if not isinstance(message, dict) or message.get("read"):
                continue
            sequence = message.get("sequence")
            unread.append(
                UnreadMessage(
                    sequence=sequence if isinstance(sequence, int) else 0,
                    subject=str(message.get("subject") or "")[:200],
                )
            )
        return sorted(unread, key=lambda item: item.sequence)

    async def close_terminal(self, terminal_handle: str) -> None:
        """Close one pane, treating a handle Orca has forgotten as already closed."""

        with suppress(OrcaError):
            await self._ok("terminal", "close", "--terminal", terminal_handle, "--json")

    async def _ok(self, *arguments: str) -> JsonObject:
        response = await self._runner.run(arguments)
        if response.get("ok") is not True:
            detail = _error_detail(response.get("error"))
            raise OrcaError(f"Orca returned a non-success response{detail}")
        return response


def _require_idle(response: JsonObject, terminal_handle: str) -> None:
    """Refuse a terminal whose wait reported that it never went idle.

    Only an explicit `satisfied: false` is refused. An Orca that reports no wait
    outcome at all is left alone deliberately: the failure being caught here is a
    stated negative, and treating a missing field as one would make the supervisor
    depend on a response shape it has no reason to require.
    """

    result = response.get("result")
    wait = result.get("wait") if isinstance(result, dict) else None
    if not isinstance(wait, dict) or wait.get("satisfied") is not False:
        return
    reason = wait.get("blockedReason") or wait.get("status") or "unknown"
    raise OrcaError(
        f"agent terminal {terminal_handle} never became idle before its task was "
        f"dispatched: {reason}"
    )


def _require_start_succeeded(response: JsonObject) -> None:
    """Refuse a worker start only when Orca states that it failed."""

    result = response.get("result")
    if not isinstance(result, dict) or result.get("state") != "failed":
        return
    detail = _outcome_detail(response)
    raise OrcaError(f"Orca worker start reported failure: {detail}")


def _error_detail(error: object) -> str:
    """Render an Orca error envelope as a bounded suffix."""

    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        parts = [str(part) for part in (code, message) if isinstance(part, str) and part]
        if parts:
            unique = parts[:1] if len(parts) == 2 and parts[0] == parts[1] else parts
            return ": " + ": ".join(unique)[:2_000]
    if isinstance(error, str) and error:
        return f": {error[:2_000]}"
    return ""


def _envelope(stdout: bytes) -> JsonObject | None:
    """Parse an Orca response envelope, or None when stdout was not one."""

    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return cast(JsonObject, value) if isinstance(value, dict) else None


def _failure_detail(stdout: bytes) -> str:
    """Read the typed error out of an Orca failure envelope printed on stdout."""

    value = _envelope(stdout)
    if value is None:
        return ""
    return _error_detail(value.get("error")).removeprefix(": ")


def _outcome_detail(envelope: JsonObject) -> str:
    """Describe a non-zero exit that still answered with a typed result."""

    result = envelope.get("result")
    if not isinstance(result, dict):
        return "no result"
    parts = [
        f"{key}={result[key]}"
        for key in ("state", "failedStage", "lastError")
        if isinstance(result.get(key), str)
    ]
    return " ".join(parts) or "no outcome fields"


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise OrcaError(f"Orca response omitted {field}")
    return cast(JsonObject, value)


def _required_string(value: JsonObject, key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise OrcaError(f"Orca response omitted {key}")
    return candidate


def _required_recursive_string(value: object, *keys: str) -> str:
    """Find a non-empty string field in a bounded JSON response tree."""

    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for candidate in value.values():
            try:
                return _required_recursive_string(candidate, *keys)
            except OrcaError:
                continue
    elif isinstance(value, list):
        for candidate in value:
            try:
                return _required_recursive_string(candidate, *keys)
            except OrcaError:
                continue
    joined = "/".join(keys)
    raise OrcaError(f"Orca response omitted {joined}")


def _optional_recursive_string(value: object, *keys: str) -> str | None:
    """Same search as `_required_recursive_string`, but absence is an answer."""

    try:
        return _required_recursive_string(value, *keys)
    except OrcaError:
        return None


def _worktree_id(response: JsonObject) -> str:
    """Read a full worktree ID from a worktree-create response."""

    result = _object(response.get("result"), "result")
    for key in ("worktreeId", "workspaceId"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    worktree = result.get("worktree")
    if isinstance(worktree, dict):
        value = worktree.get("id")
        if isinstance(value, str) and value:
            return value
    raise OrcaError("Orca response omitted the created worktree ID")


def _fast_agent_command(profile: AgentProfile) -> tuple[str, ...]:
    """Build provider-native argv with fast mode active before task injection."""

    if profile.agent == "codex":
        return (
            "codex",
            "--model",
            profile.model,
            "-c",
            f'model_reasoning_effort="{profile.strength}"',
            "-c",
            'service_tier="priority"',
        )
    if profile.agent == "claude":
        return (
            "claude",
            "--model",
            profile.model,
            "--effort",
            profile.strength,
            "--settings",
            '{"fastMode":true}',
        )
    raise OrcaError(f"fast mode is unsupported for worker agent: {profile.agent}")


def _orchestration_id(response: JsonObject, container: str, explicit_key: str) -> str:
    """Read an orchestration entity ID without confusing it with the RPC request ID."""

    result = _object(response.get("result"), "result")
    explicit = result.get(explicit_key)
    if isinstance(explicit, str) and explicit:
        return explicit
    nested = result.get(container)
    if isinstance(nested, dict):
        identifier = nested.get("id")
        if isinstance(identifier, str) and identifier:
            return identifier
    raise OrcaError(f"Orca response omitted result.{explicit_key}")
