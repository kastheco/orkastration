"""Narrow, JSON-only access to the public Orca CLI."""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import TypeAdapter, ValidationError

from kasgraph.config import AgentProfile
from kasgraph.models import OrcaSnapshot, OrcaWorktree

JsonObject = dict[str, object]


class OrcaError(RuntimeError):
    """Raised when an Orca CLI request fails or violates its JSON contract."""


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
            raise OrcaError(f"Orca command timed out after {self.timeout_seconds:g}s") from exc
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[:2_000]
            raise OrcaError(f"Orca command failed with rc={process.returncode}: {detail}")
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise OrcaError("Orca returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise OrcaError("Orca response must be a JSON object")
        return cast(JsonObject, value)


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
        base_ref: str | None = None,
        parent_worktree_id: str | None = None,
    ) -> tuple[str, str, JsonObject]:
        """Start one supervised worker with an explicit model and effort."""

        if profile.fast:
            return await self._start_fast_worker(
                task_id=task_id,
                lane_name=lane_name,
                repo_selector=repo_selector,
                worktree_id=worktree_id,
                base_ref=base_ref,
                parent_worktree_id=parent_worktree_id,
                profile=profile,
            )

        arguments = ["orchestration", "worker-start", "--task", task_id]
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
        dispatch_id = _required_recursive_string(response, "dispatchId")
        resolved_worktree = worktree_id or _required_recursive_string(
            response, "worktreeId", "workspaceId"
        )
        return dispatch_id, resolved_worktree, response

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
    ) -> tuple[str, str, JsonObject]:
        """Launch fast provider argv, then attach it to a supervised Dispatch."""

        resolved_worktree = worktree_id
        if resolved_worktree is None:
            arguments = ["worktree", "create", "--name", lane_name]
            if parent_worktree_id is None:
                arguments.append("--no-parent")
            else:
                arguments.extend(("--parent-worktree", f"id:{parent_worktree_id}"))
            arguments.extend(("--repo", repo_selector, "--setup", "run"))
            if base_ref is not None:
                arguments.extend(("--base-branch", base_ref))
            arguments.append("--json")
            worktree_response = await self._ok(*arguments)
            resolved_worktree = _worktree_id(worktree_response)

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
        terminal_handle = _required_recursive_string(terminal_response, "terminalHandle", "handle")
        await self._ok(
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
        response = await self._ok(
            "orchestration",
            "worker-start",
            "--task",
            task_id,
            "--terminal",
            terminal_handle,
            "--json",
        )
        dispatch_id = _required_recursive_string(response, "dispatchId")
        return dispatch_id, resolved_worktree, response

    async def release_worker(self, dispatch_id: str) -> JsonObject:
        """Release the exact terminal owned by a settled Dispatch."""

        return await self._ok(
            "orchestration", "worker-release", "--dispatch", dispatch_id, "--json"
        )

    async def _ok(self, *arguments: str) -> JsonObject:
        response = await self._runner.run(arguments)
        if response.get("ok") is not True:
            raise OrcaError("Orca returned a non-success response")
        return response


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
