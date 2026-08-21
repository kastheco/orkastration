"""Narrow, JSON-only access to the public Orca CLI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from pydantic import TypeAdapter, ValidationError

from kasgraph.models import LaneProposal, OrcaSnapshot, OrcaWorktree

JsonObject = dict[str, object]


class OrcaError(RuntimeError):
    """Raised when an Orca CLI request fails or violates its JSON contract."""


class CommandRunner(Protocol):
    """Execute one Orca command without a shell."""

    async def run(self, arguments: Sequence[str]) -> JsonObject:
        """Return the parsed JSON response."""


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

    async def create_lane(self, lane: LaneProposal) -> tuple[str, JsonObject]:
        """Create one independent Orca worktree and launch its selected agent."""

        existing = next(
            (item for item in (await self.snapshot()).worktrees if item.display_name == lane.name),
            None,
        )
        if existing is not None:
            return existing.worktree_id, {"reused": True, "worktreeId": existing.worktree_id}
        response = await self._ok(
            "worktree",
            "create",
            "--repo",
            lane.repo_selector,
            "--name",
            lane.name,
            "--no-parent",
            "--agent",
            lane.agent_id,
            "--prompt",
            lane.prompt,
            "--json",
        )
        result = _object(response.get("result"), "result")
        worktree = _object(result.get("worktree"), "result.worktree")
        worktree_id = worktree.get("id")
        if not isinstance(worktree_id, str) or not worktree_id:
            raise OrcaError("Orca create response omitted result.worktree.id")
        return worktree_id, response

    async def _ok(self, *arguments: str) -> JsonObject:
        response = await self._runner.run(arguments)
        if response.get("ok") is not True:
            raise OrcaError("Orca returned a non-success response")
        return response


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise OrcaError(f"Orca response omitted {field}")
    return cast(JsonObject, value)
