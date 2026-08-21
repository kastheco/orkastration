"""Accepted graph construction, scheduling, and Orca reconciliation."""

from __future__ import annotations

from typing import Protocol, cast

from kasgraph.config import AgentProfile, GraphConfig
from kasgraph.models import (
    ROLE_ORDER,
    GraphResult,
    LaneProposal,
    ProposalReceipt,
    RoleName,
    StageLaunch,
    StagePhase,
    StageRecord,
    SupervisorPlan,
)
from kasgraph.orca import JsonObject
from kasgraph.store import StateStore


class OrcaGraphController(Protocol):
    """Narrow Orca orchestration surface used by Kasgraph."""

    async def create_run(self, objective: str) -> tuple[str, JsonObject]: ...

    async def create_task(self, spec: str, dependencies: list[str]) -> tuple[str, JsonObject]: ...

    async def tasks(self, orca_run_id: str) -> list[JsonObject]: ...

    async def start_worker(
        self,
        *,
        task_id: str,
        lane_name: str,
        repo_selector: str,
        worktree_id: str | None,
        profile: AgentProfile,
    ) -> tuple[str, str, JsonObject]: ...

    async def release_worker(self, dispatch_id: str) -> JsonObject: ...


class ExecutionController:
    """Turn accepted proposals into monitored Orca Task graphs."""

    def __init__(self, *, config: GraphConfig, orca: OrcaGraphController, store: StateStore):
        self._config = config
        self._orca = orca
        self._store = store

    def propose(self, proposal: SupervisorPlan) -> ProposalReceipt:
        """Record a proposal without mutating Orca."""

        run_id = self._store.record_proposal(proposal)
        return ProposalReceipt(run_id=run_id, proposal=proposal)

    async def accept(self, run_id: str) -> GraphResult:
        """Accept once, construct the Orca DAG, and start the first lane wave."""

        run = self._store.run(run_id)
        if run.proposal.next_action != "propose_lanes":
            raise ValueError(f"run {run_id} has no executable lanes ({run.proposal.next_action})")
        if run.status == "proposed":
            orca_run_id, _ = await self._orca.create_run(run.proposal.objective)
            self._store.mark_accepted(run_id, orca_run_id)
            run = self._store.run(run_id)
        elif run.status not in {"active", "blocked"}:
            raise ValueError(f"run {run_id} cannot be accepted from status {run.status}")
        await self._ensure_tasks(run_id)
        return await self.monitor(run_id)

    async def monitor(self, run_id: str) -> GraphResult:
        """Reconcile Task state, release settled workers, and start newly ready stages."""

        run = self._store.run(run_id)
        if run.orca_run_id is None:
            raise ValueError(f"run {run_id} has not been accepted")
        task_rows = await self._orca.tasks(run.orca_run_id)
        task_phases = {_task_id(task): _task_phase(task) for task in task_rows}
        for stage in self._store.stages(run_id):
            if stage.orca_task_id is None:
                continue
            phase = task_phases.get(stage.orca_task_id)
            if phase is not None and phase is not stage.phase:
                self._store.sync_stage(run_id, stage.lane_id, stage.role, phase)

        await self._release_settled(run_id)
        started = await self._start_ready(run_id)
        status = self._derive_status(run_id)
        if status in {"complete", "failed", "blocked"}:
            current = self._store.run(run_id).status
            if current != status:
                self._store.set_terminal_status(run_id, status)
        return GraphResult(
            run_id=run_id,
            orca_run_id=run.orca_run_id,
            status=status,
            started=started,
            lanes=self._store.lanes(run_id),
            stages=self._store.stages(run_id),
        )

    async def _ensure_tasks(self, run_id: str) -> None:
        run = self._store.run(run_id)
        lanes = {lane.name: lane for lane in self._store.lanes(run_id)}
        stages = self._store.stages(run_id)
        by_lane = _stages_by_lane(stages)
        for proposal in run.proposal.lanes:
            lane = lanes[proposal.name]
            previous_task: str | None = None
            for role in ROLE_ORDER:
                stage = by_lane[lane.lane_id][role]
                if stage.orca_task_id is None:
                    task_id, _ = await self._orca.create_task(
                        _stage_spec(proposal, role),
                        [previous_task] if previous_task else [],
                    )
                    self._store.bind_stage_task(run_id, lane.lane_id, role, task_id)
                    previous_task = task_id
                else:
                    previous_task = stage.orca_task_id

    async def _release_settled(self, run_id: str) -> None:
        for stage in self._store.stages(run_id):
            if (
                stage.phase in {StagePhase.COMPLETED, StagePhase.FAILED}
                and stage.orca_dispatch_id is not None
                and not stage.released
            ):
                await self._orca.release_worker(stage.orca_dispatch_id)
                self._store.mark_released(run_id, stage.lane_id, stage.role)

    async def _start_ready(self, run_id: str) -> list[StageLaunch]:
        lanes = self._store.lanes(run_id)
        by_id = {lane.lane_id: lane for lane in lanes}
        stages = self._store.stages(run_id)
        by_lane = _stages_by_lane(stages)
        active_lanes = {
            lane_id
            for lane_id, lane_stages in by_lane.items()
            if any(stage.orca_dispatch_id is not None for stage in lane_stages.values())
            and not all(stage.phase is StagePhase.COMPLETED for stage in lane_stages.values())
        }
        started: list[StageLaunch] = []
        for stage in stages:
            if stage.phase is not StagePhase.READY or stage.orca_dispatch_id is not None:
                continue
            if stage.lane_id not in active_lanes:
                if len(active_lanes) >= self._config.max_parallel_lanes:
                    continue
                active_lanes.add(stage.lane_id)
            lane = by_id[stage.lane_id]
            lane_has_active_stage = any(
                item.role is not stage.role and item.phase is StagePhase.DISPATCHED
                for item in by_lane[stage.lane_id].values()
            )
            if lane_has_active_stage:
                continue
            if stage.orca_task_id is None:
                raise ValueError(f"ready stage {stage.stage_id} has no Orca task")
            profile = cast(AgentProfile, getattr(self._config.roles, stage.role.value))
            dispatch_id, worktree_id, payload = await self._orca.start_worker(
                task_id=stage.orca_task_id,
                lane_name=lane.name,
                repo_selector=lane.repo_selector,
                worktree_id=lane.worktree_id,
                profile=profile,
            )
            self._store.mark_stage_started(
                run_id,
                lane.lane_id,
                stage.role,
                dispatch_id,
                worktree_id,
                payload,
            )
            started.append(
                StageLaunch(
                    lane=lane.name,
                    role=stage.role,
                    task_id=stage.orca_task_id,
                    dispatch_id=dispatch_id,
                    worktree_id=worktree_id,
                )
            )
        return started

    def _derive_status(self, run_id: str) -> str:
        phases = [stage.phase for stage in self._store.stages(run_id)]
        if phases and all(phase is StagePhase.COMPLETED for phase in phases):
            return "complete"
        if any(phase is StagePhase.FAILED for phase in phases):
            return "failed"
        if any(phase is StagePhase.BLOCKED for phase in phases):
            return "blocked"
        return "active"


def _stages_by_lane(stages: list[StageRecord]) -> dict[str, dict[RoleName, StageRecord]]:
    grouped: dict[str, dict[RoleName, StageRecord]] = {}
    for stage in stages:
        grouped.setdefault(stage.lane_id, {})[stage.role] = stage
    return grouped


def _task_id(task: JsonObject) -> str:
    for key in ("taskId", "id"):
        value = task.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError("Orca Task omitted its ID")


def _task_phase(task: JsonObject) -> StagePhase:
    raw = task.get("status")
    if not isinstance(raw, str):
        raise ValueError("Orca Task omitted status")
    try:
        return StagePhase(raw)
    except ValueError as exc:
        raise ValueError(f"unknown Orca Task status {raw!r}") from exc


def _stage_spec(lane: LaneProposal, role: RoleName) -> str:
    context = (
        f"Lane: {lane.name}\nIssue: {lane.issue_id}\n"
        f"Objective:\n{lane.prompt}\n\nStop condition:\n{lane.stop_condition}"
    )
    instructions = {
        RoleName.WORKER: (
            "Implement the scoped issue in this worktree. Preserve unrelated changes, run the "
            "narrowest useful checks followed by broader checks when warranted, and make a focused "
            "local commit when the work is verified."
        ),
        RoleName.INITIAL_REVIEWER: (
            "Review the worker's committed change against the issue/spec and repository standards. "
            "Stay read-only. Report concrete findings with file locations and verification "
            "evidence; state explicitly when there are no findings."
        ),
        RoleName.FIXER: (
            "Read the initial review result, fix every actionable finding in this same worktree, "
            "run relevant verification, and commit the corrections. If there are no findings, "
            "verify the existing result and report a no-op."
        ),
        RoleName.RE_REVIEWER: (
            "Perform a fresh read-only review of the final exact head against the issue/spec and "
            "repository standards. Report any remaining findings and the checks used."
        ),
    }
    return f"{instructions[role]}\n\n{context}"
