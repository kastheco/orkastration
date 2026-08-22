"""Dynamic finding scheduling and Orca lifecycle reconciliation."""

from __future__ import annotations

import json
from typing import Protocol, cast

from pydantic import ValidationError

from kasgraph.config import AgentProfile, GraphConfig
from kasgraph.models import (
    AttemptKind,
    EscalationDecision,
    FindingPhase,
    FindingReason,
    FindingRecord,
    FixAttempt,
    GraphResult,
    InitialReviewReport,
    LanePhase,
    LaneProposal,
    OrcaWorkerResult,
    ProposalReceipt,
    ReReviewResult,
    StageKind,
    StageLaunch,
    StagePhase,
    StageRecord,
    SupervisorPlan,
    WorkerResult,
)
from kasgraph.orca import JsonObject
from kasgraph.store import StateStore


class OrcaGraphController(Protocol):
    """Narrow Orca orchestration surface used by Kasgraph."""

    async def create_run(self, objective: str) -> tuple[str, JsonObject]: ...

    async def runs(self) -> list[JsonObject]: ...

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

    async def worker_dispatch(self, task_id: str) -> tuple[str, str | None] | None: ...


class ExecutionController:
    """Turn accepted proposals into persisted, bounded convergence workflows."""

    def __init__(self, *, config: GraphConfig, orca: OrcaGraphController, store: StateStore):
        self._config = config
        self._orca = orca
        self._store = store

    def propose(self, proposal: SupervisorPlan) -> ProposalReceipt:
        """Record a proposal without mutating Orca."""

        run_id = self._store.record_proposal(proposal)
        return ProposalReceipt(run_id=run_id, proposal=proposal)

    async def accept(self, run_id: str) -> GraphResult:
        """Accept once, bind a Run, and start the bounded first wave."""

        run = self._store.run(run_id)
        if run.proposal.next_action != "propose_lanes":
            raise ValueError(f"run {run_id} has no executable lanes ({run.proposal.next_action})")
        if run.status == "proposed":
            objective = _orca_run_objective(run_id, run.proposal.objective)
            matches = [
                item for item in await self._orca.runs() if item.get("objective") == objective
            ]
            if len(matches) > 1:
                raise ValueError(f"multiple Orca Runs match local run {run_id}")
            if matches:
                orca_run_id = _task_id(matches[0])
            else:
                orca_run_id, _ = await self._orca.create_run(objective)
            self._store.mark_accepted(run_id, orca_run_id)
        elif run.status not in {"active", "blocked"}:
            raise ValueError(f"run {run_id} cannot be accepted from status {run.status}")
        await self._ensure_tasks(run_id)
        return await self.monitor(run_id)

    async def monitor(self, run_id: str) -> GraphResult:
        """Reconcile results, materialize next work, and start one safe wave."""

        run = self._store.run(run_id)
        if run.orca_run_id is None:
            raise ValueError(f"run {run_id} has not been accepted")
        tasks = await self._orca.tasks(run.orca_run_id)
        by_id = {_task_id(task): task for task in tasks}
        lanes = {lane.lane_id: lane for lane in self._store.lanes(run_id)}
        for stage in self._store.stages(run_id):
            if stage.orca_task_id is None:
                continue
            task_id = stage.orca_task_id
            task = by_id.get(task_id)
            if task is None:
                continue
            phase = _task_phase(task)
            if stage.phase is StagePhase.STARTING and phase is StagePhase.READY:
                recovered = await self._orca.worker_dispatch(task_id)
                if recovered is None:
                    self._store.reset_stage_reservation(run_id, stage)
                    continue
                dispatch_id, recovered_worktree = recovered
                worktree_id = recovered_worktree or lanes[stage.lane_id].worktree_id
                if worktree_id is None:
                    raise ValueError(
                        f"Orca Dispatch {dispatch_id} omitted its worker worktree identity"
                    )
                self._store.mark_stage_started(
                    run_id,
                    stage.stage_id,
                    dispatch_id,
                    worktree_id,
                    {"recovered": True, "dispatchId": dispatch_id},
                )
                stage = stage.model_copy(update={"orca_dispatch_id": dispatch_id})
                phase = StagePhase.DISPATCHED
            if stage.orca_dispatch_id is None and phase in {
                StagePhase.DISPATCHED,
                StagePhase.COMPLETED,
                StagePhase.FAILED,
            }:
                recovered = await self._orca.worker_dispatch(task_id)
                if recovered is None:
                    raise ValueError(
                        f"Orca task {stage.orca_task_id} has {phase.value} state without a Dispatch"
                    )
                dispatch_id, recovered_worktree = recovered
                worktree_id = recovered_worktree or lanes[stage.lane_id].worktree_id
                if worktree_id is None:
                    raise ValueError(
                        f"Orca Dispatch {dispatch_id} omitted its worker worktree identity"
                    )
                self._store.mark_stage_started(
                    run_id,
                    stage.stage_id,
                    dispatch_id,
                    worktree_id,
                    {"recovered": True, "dispatchId": dispatch_id},
                )
            self._store.sync_stage(
                run_id,
                stage.stage_id,
                phase,
                _task_result_json(task),
            )

        self._process_results(run_id)
        await self._release_settled(run_id)
        self._ensure_dynamic_stages(run_id)
        await self._ensure_tasks(run_id)
        started = await self._start_ready(run_id)
        status = self._derive_status(run_id)
        current = self._store.run(run_id).status
        if status in {"complete", "failed", "blocked"} and current != status:
            self._store.set_terminal_status(run_id, status)
        return GraphResult(
            run_id=run_id,
            orca_run_id=run.orca_run_id,
            status=status,
            started=started,
            lanes=self._store.lanes(run_id),
            stages=self._store.stages(run_id),
            findings=self._store.findings(run_id),
        )

    def _process_results(self, run_id: str) -> None:
        for stage in self._store.stages(run_id):
            if stage.processed or stage.phase not in {StagePhase.COMPLETED, StagePhase.FAILED}:
                continue
            if stage.phase is StagePhase.FAILED or stage.result_json is None:
                self._reject_stage(run_id, stage, "failed or missing worker lifecycle result")
                continue
            try:
                result = OrcaWorkerResult.model_validate_json(stage.result_json)
                if result.outcome != "succeeded":
                    raise ValueError("worker reported a failed outcome")
                self._apply_contract(run_id, stage, result)
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._reject_stage(run_id, stage, f"invalid structured result: {exc}")
                continue
            self._store.record_lifecycle_receipt(run_id, stage, result)

    def _apply_contract(self, run_id: str, stage: StageRecord, lifecycle: OrcaWorkerResult) -> None:
        if stage.role is StageKind.WORKER:
            result = WorkerResult.model_validate_json(lifecycle.body)
            self._store.record_worker_result(run_id, stage.lane_id, result)
            return
        if stage.role is StageKind.INITIAL_REVIEWER:
            report = InitialReviewReport.model_validate_json(lifecycle.body)
            worker = self._store.worker_result(stage.lane_id)
            if report.review_revision != worker.review_revision:
                raise ValueError("initial review revision does not match the worker changeset")
            self._store.record_initial_review(run_id, stage.lane_id, report)
            return
        finding = self._finding_for_stage(run_id, stage)
        if stage.round != finding.round:
            raise ValueError(
                f"stage round {stage.round} does not match finding round {finding.round}"
            )
        if stage.role is StageKind.FIXER:
            self._apply_fix(run_id, finding, stage, FixAttempt.model_validate_json(lifecycle.body))
        elif stage.role is StageKind.RE_REVIEWER:
            self._apply_re_review(
                run_id, finding, stage, ReReviewResult.model_validate_json(lifecycle.body)
            )
        elif stage.role is StageKind.ESCALATION:
            self._apply_escalation(
                run_id, finding, stage, EscalationDecision.model_validate_json(lifecycle.body)
            )

    def _apply_fix(
        self, run_id: str, finding: FindingRecord, stage: StageRecord, attempt: FixAttempt
    ) -> None:
        if attempt.finding_id != finding.finding_id or attempt.round != finding.round:
            raise ValueError("fix attempt does not match its persisted finding and round")
        self._store.record_fix_attempt(run_id, finding, stage, attempt)
        if attempt.status == "fixed":
            self._store.set_finding_state(
                run_id, finding.finding_key, phase=FindingPhase.PENDING_RE_REVIEW
            )
        elif attempt.status == "blocked_scope":
            self._escalate(run_id, finding, FindingReason.SCOPE_ESCAPE)
        elif stage.attempt_kind is AttemptKind.PRIMARY and self._config.roles.fixer.fallback:
            self._store.set_finding_state(
                run_id,
                finding.finding_key,
                phase=FindingPhase.PENDING_FIX,
                escalation_reason=FindingReason.CAPABILITY_FALLBACK,
            )
        else:
            self._escalate(run_id, finding, FindingReason.AMBIGUOUS_RESULT)

    def _apply_re_review(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        result: ReReviewResult,
    ) -> None:
        if result.finding_id != finding.finding_id or result.round != finding.round:
            raise ValueError("re-review does not match its persisted finding and round")
        attempt = self._store.latest_fix_attempt(finding.finding_key)
        if attempt is None or attempt.commit_sha != result.reviewed_commit_sha:
            raise ValueError("re-review is not pinned to the persisted fixer commit")
        existing = self._store.findings(run_id, finding.lane_id)
        _validate_discovered_findings(existing, result)
        existing_ids = {item.finding_id for item in existing}
        collisions = {
            item.finding.id for item in result.new_findings if item.finding.id in existing_ids
        }
        persisted = self._store.re_review(finding.finding_key, result.round)
        if persisted is not None and persisted != result:
            raise ValueError("re-review result changed after persistence")
        if collisions and persisted is None:
            raise ValueError(f"re-review reused persisted finding IDs: {sorted(collisions)}")
        self._store.record_re_review(run_id, finding, stage, result)
        for discovered in result.new_findings:
            if discovered.origin == "introduced_by_fix":
                self._store.add_finding(
                    run_id,
                    finding.lane_id,
                    discovered.finding,
                    origin="introduced_by_fix",
                )
            else:
                self._store.add_finding(
                    run_id, finding.lane_id, discovered.finding, origin="unrelated"
                )
        if result.verdict == "resolved":
            self._store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.RESOLVED)
        elif result.verdict == "still_open":
            if finding.round < self._config.review_cycle.max_fix_rounds_per_finding:
                self._store.set_finding_state(
                    run_id,
                    finding.finding_key,
                    phase=FindingPhase.PENDING_FIX,
                    round=finding.round + 1,
                )
            else:
                self._escalate(run_id, finding, FindingReason.ROUNDS_EXHAUSTED)
        elif result.verdict in {"regression_introduced_by_fix", "interaction_failure"}:
            introduced = [
                item for item in result.new_findings if item.origin == "introduced_by_fix"
            ]
            if not introduced:
                self._escalate(run_id, finding, FindingReason.AMBIGUOUS_RESULT)
            else:
                self._store.set_finding_state(
                    run_id, finding.finding_key, phase=FindingPhase.RESOLVED
                )
        else:
            self._escalate(run_id, finding, FindingReason.AMBIGUOUS_RESULT)

    def _apply_escalation(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        decision: EscalationDecision,
    ) -> None:
        if decision.finding_id != finding.finding_id or decision.round != finding.round:
            raise ValueError("escalation does not match its persisted finding and round")
        if finding.escalation_reason is None or decision.reason != finding.escalation_reason.value:
            raise ValueError("escalation reason does not match the persisted trigger")
        self._store.record_escalation(run_id, finding, stage, decision)
        if decision.action == "approve_scope_revision":
            assert decision.revised_finding is not None
            if decision.revised_finding.id != finding.finding_id:
                raise ValueError("scope revision must preserve the finding ID")
            if finding.round >= self._config.review_cycle.max_fix_rounds_per_finding:
                self._store.set_finding_state(
                    run_id, finding.finding_key, phase=FindingPhase.BLOCKED
                )
            else:
                self._store.set_finding_state(
                    run_id,
                    finding.finding_key,
                    phase=FindingPhase.PENDING_FIX,
                    round=finding.round + 1,
                    effective_contract=decision.revised_finding,
                )
        elif decision.action == "defer":
            self._store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.DEFERRED)
        else:
            self._store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)

    def _reject_stage(self, run_id: str, stage: StageRecord, reason: str) -> None:
        if stage.finding_id is None:
            self._store.set_lane_phase(run_id, stage.lane_id, LanePhase.FAILED)
        elif stage.role is StageKind.ESCALATION:
            finding = self._finding_for_stage(run_id, stage)
            self._store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)
        else:
            self._escalate(
                run_id,
                self._finding_for_stage(run_id, stage),
                FindingReason.AMBIGUOUS_RESULT,
            )
        self._store.mark_stage_processed(run_id, stage, reason)

    def _escalate(self, run_id: str, finding: FindingRecord, reason: FindingReason) -> None:
        self._store.set_finding_state(
            run_id,
            finding.finding_key,
            phase=FindingPhase.PENDING_ESCALATION,
            escalation_reason=reason,
        )

    def _ensure_dynamic_stages(self, run_id: str) -> None:
        stages = self._store.stages(run_id)
        by_lane: dict[str, list[StageRecord]] = {}
        for stage in stages:
            by_lane.setdefault(stage.lane_id, []).append(stage)
        for lane in self._store.lanes(run_id):
            lane_stages = by_lane.get(lane.lane_id, [])
            worker_done = any(
                stage.role is StageKind.WORKER and stage.processed for stage in lane_stages
            )
            reviewer_exists = any(stage.role is StageKind.INITIAL_REVIEWER for stage in lane_stages)
            if worker_done and not reviewer_exists:
                self._store.ensure_stage(
                    run_id,
                    lane.lane_id,
                    stage_key=f"{lane.lane_id}:initial-review",
                    role=StageKind.INITIAL_REVIEWER,
                )
            findings = self._store.findings(run_id, lane.lane_id)
            phases = {item.finding_id: item.phase for item in findings}
            for finding in findings:
                dependencies_ready = all(
                    phases.get(dependency) in {FindingPhase.RESOLVED, FindingPhase.DEFERRED}
                    for dependency in finding.effective_contract.dependencies
                )
                if finding.phase is FindingPhase.PENDING_FIX and dependencies_ready:
                    attempt_kind = (
                        AttemptKind.FALLBACK
                        if finding.escalation_reason is FindingReason.CAPABILITY_FALLBACK
                        else AttemptKind.PRIMARY
                    )
                    self._store.ensure_stage(
                        run_id,
                        lane.lane_id,
                        stage_key=(
                            f"{lane.lane_id}:{finding.finding_id}:fix:"
                            f"{finding.round}:{attempt_kind.value}"
                        ),
                        role=StageKind.FIXER,
                        finding_key=finding.finding_key,
                        finding_id=finding.finding_id,
                        round=finding.round,
                        attempt_kind=attempt_kind,
                    )
                elif finding.phase is FindingPhase.PENDING_RE_REVIEW:
                    self._store.ensure_stage(
                        run_id,
                        lane.lane_id,
                        stage_key=(
                            f"{lane.lane_id}:{finding.finding_id}:re-review:{finding.round}"
                        ),
                        role=StageKind.RE_REVIEWER,
                        finding_key=finding.finding_key,
                        finding_id=finding.finding_id,
                        round=finding.round,
                    )
                elif finding.phase is FindingPhase.PENDING_ESCALATION:
                    reason = finding.escalation_reason or FindingReason.AMBIGUOUS_RESULT
                    self._store.ensure_stage(
                        run_id,
                        lane.lane_id,
                        stage_key=(
                            f"{lane.lane_id}:{finding.finding_id}:escalate:"
                            f"{finding.round}:{reason.value}"
                        ),
                        role=StageKind.ESCALATION,
                        finding_key=finding.finding_key,
                        finding_id=finding.finding_id,
                        round=finding.round,
                    )

    async def _ensure_tasks(self, run_id: str) -> None:
        run = self._store.run(run_id)
        if run.orca_run_id is None:
            raise ValueError(f"run {run_id} has not been accepted")
        proposals = {lane.name: lane for lane in run.proposal.lanes}
        lanes = {lane.lane_id: lane for lane in self._store.lanes(run_id)}
        remote_tasks = await self._orca.tasks(run.orca_run_id)
        locally_bound = {
            stage.orca_task_id
            for stage in self._store.stages(run_id)
            if stage.orca_task_id is not None
        }
        for stage in self._store.stages(run_id):
            if stage.orca_task_id is not None:
                continue
            lane = lanes[stage.lane_id]
            dependencies = _completed_dependency(stage, self._store.stages(run_id))
            spec = self._stage_spec(proposals[lane.name], stage, run_id)
            recoverable = [
                task
                for task in remote_tasks
                if task.get("spec") == spec and _task_id(task) not in locally_bound
            ]
            if len(recoverable) > 1:
                raise ValueError(f"multiple unbound Orca Tasks match stage {stage.stage_key}")
            if recoverable:
                task_id = _task_id(recoverable[0])
            else:
                task_id, response = await self._orca.create_task(spec, dependencies)
                created = response.get("result")
                if isinstance(created, dict):
                    remote_tasks.append(cast(JsonObject, created))
            self._store.bind_stage_task(run_id, stage.stage_id, task_id)
            locally_bound.add(task_id)

    async def _release_settled(self, run_id: str) -> None:
        for stage in self._store.stages(run_id):
            if (
                stage.phase in {StagePhase.COMPLETED, StagePhase.FAILED}
                and stage.orca_dispatch_id is not None
                and not stage.released
            ):
                await self._orca.release_worker(stage.orca_dispatch_id)
                self._store.mark_released(run_id, stage.stage_id)

    async def _start_ready(self, run_id: str) -> list[StageLaunch]:
        lanes = {lane.lane_id: lane for lane in self._store.lanes(run_id)}
        stages = self._store.stages(run_id)
        started: list[StageLaunch] = []
        for stage in stages:
            if stage.phase is not StagePhase.READY or stage.orca_dispatch_id is not None:
                continue
            if not self._store.reserve_stage_start(
                run_id,
                stage,
                max_workers=self._config.max_parallel_workers,
                max_lanes=self._config.max_parallel_lanes,
                max_lane_fixers=1,
            ):
                continue
            if stage.orca_task_id is None:
                raise ValueError(f"ready stage {stage.stage_id} has no Orca task")
            lane = lanes[stage.lane_id]
            dispatch_id, worktree_id, payload = await self._orca.start_worker(
                task_id=stage.orca_task_id,
                lane_name=_worker_name(lane.name, stage),
                repo_selector=lane.repo_selector,
                worktree_id=lane.worktree_id,
                profile=self._profile(stage),
            )
            self._store.mark_stage_started(
                run_id, stage.stage_id, dispatch_id, worktree_id, payload
            )
            if stage.finding_id is not None:
                finding = self._finding_for_stage(run_id, stage)
                phase = {
                    StageKind.FIXER: FindingPhase.FIXING,
                    StageKind.RE_REVIEWER: FindingPhase.RE_REVIEWING,
                    StageKind.ESCALATION: FindingPhase.ESCALATING,
                }[stage.role]
                self._store.set_finding_state(
                    run_id,
                    finding.finding_key,
                    phase=phase,
                    escalation_reason=finding.escalation_reason,
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
        stages = self._store.stages(run_id)
        findings = self._store.findings(run_id)
        by_lane_findings: dict[str, list[FindingRecord]] = {}
        for finding in findings:
            by_lane_findings.setdefault(finding.lane_id, []).append(finding)
        lane_phases: list[LanePhase] = []
        for lane in self._store.lanes(run_id):
            lane_stages = [stage for stage in stages if stage.lane_id == lane.lane_id]
            lane_findings = by_lane_findings.get(lane.lane_id, [])
            if lane.phase is LanePhase.FAILED or any(
                stage.phase is StagePhase.FAILED for stage in lane_stages
            ):
                phase = LanePhase.FAILED
            elif any(finding.phase is FindingPhase.BLOCKED for finding in lane_findings):
                phase = LanePhase.BLOCKED
            else:
                reviewed = any(
                    stage.role is StageKind.INITIAL_REVIEWER and stage.processed
                    for stage in lane_stages
                )
                settled = all(
                    finding.phase in {FindingPhase.RESOLVED, FindingPhase.DEFERRED}
                    for finding in lane_findings
                )
                phase = LanePhase.COMPLETE if reviewed and settled else LanePhase.ACTIVE
            self._store.set_lane_phase(run_id, lane.lane_id, phase)
            lane_phases.append(phase)
        if any(phase is LanePhase.FAILED for phase in lane_phases):
            return "failed"
        if any(phase is LanePhase.BLOCKED for phase in lane_phases):
            return "blocked"
        if lane_phases and all(phase is LanePhase.COMPLETE for phase in lane_phases):
            return "complete"
        return "active"

    def _finding_for_stage(self, run_id: str, stage: StageRecord) -> FindingRecord:
        matches = [
            finding
            for finding in self._store.findings(run_id, stage.lane_id)
            if finding.finding_id == stage.finding_id
        ]
        if len(matches) != 1:
            raise ValueError(f"stage {stage.stage_id} has no unique finding")
        return matches[0]

    def _profile(self, stage: StageRecord) -> AgentProfile:
        if stage.role is StageKind.ESCALATION:
            return self._config.review_cycle.escalation
        if stage.role is StageKind.FIXER and stage.attempt_kind is AttemptKind.FALLBACK:
            fallback = self._config.roles.fixer.fallback
            if fallback is None:
                raise ValueError("fallback fixer stage has no configured fallback")
            return fallback
        return cast(AgentProfile, getattr(self._config.roles, stage.role.value))

    def _stage_spec(self, lane: LaneProposal, stage: StageRecord, run_id: str) -> str:
        context = (
            f"Lane: {lane.name}\nIssue: {lane.issue_id}\nObjective:\n{lane.prompt}\n\n"
            f"Stop condition:\n{lane.stop_condition}"
        )
        if stage.role is StageKind.WORKER:
            schema = json.dumps(WorkerResult.model_json_schema(), sort_keys=True)
            return (
                "Implement the scoped issue, verify it, and commit the result. Send worker_done "
                "with an explicit outcome and body set to only the exact changeset JSON matching "
                f"this schema: {schema}\n\n{context}"
            )
        if stage.role is StageKind.INITIAL_REVIEWER:
            schema = json.dumps(InitialReviewReport.model_json_schema(), sort_keys=True)
            return (
                "Review the exact worker changeset once, read-only. Freeze every actionable "
                "finding. Send worker_done with body set to only the JSON contract matching this "
                f"schema: {schema}\n\n{context}"
            )
        finding = self._finding_for_stage(run_id, stage)
        contract = finding.effective_contract.model_dump_json()
        if stage.role is StageKind.FIXER:
            schema = json.dumps(FixAttempt.model_json_schema(), sort_keys=True)
            return (
                f"Fix only this frozen finding at round {finding.round}: {contract}\n"
                "Do not widen scope. Send worker_done with body set to only the JSON contract "
                f"matching this schema: {schema}\n\n{context}"
            )
        if stage.role is StageKind.RE_REVIEWER:
            attempt = self._store.latest_fix_attempt(finding.finding_key)
            schema = json.dumps(ReReviewResult.model_json_schema(), sort_keys=True)
            return (
                f"Re-review only this finding at round {finding.round}: {contract}\n"
                f"Fixer evidence: {attempt.model_dump_json() if attempt else 'missing'}\n"
                "Unrelated findings must be returned with origin unrelated so they are deferred. "
                "Send worker_done with body set to only the JSON contract matching this schema: "
                f"{schema}\n\n{context}"
            )
        schema = json.dumps(EscalationDecision.model_json_schema(), sort_keys=True)
        return (
            f"Adjudicate this finding without silently widening it: {contract}\n"
            f"Trigger: {finding.escalation_reason}. Send worker_done with body set to only the "
            f"JSON contract matching this schema: {schema}\n\n{context}"
        )


def _task_id(task: JsonObject) -> str:
    for key in ("taskId", "id"):
        value = task.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError("Orca Task omitted its ID")


def _orca_run_objective(run_id: str, objective: str) -> str:
    """Embed a stable local correlation key in the remote Run objective."""

    return f"{objective}\n\nKasgraph run: {run_id}"


def _task_phase(task: JsonObject) -> StagePhase:
    raw = task.get("status")
    if not isinstance(raw, str):
        raise ValueError("Orca Task omitted status")
    try:
        return StagePhase(raw)
    except ValueError as exc:
        raise ValueError(f"unknown Orca Task status {raw!r}") from exc


def _task_result_json(task: JsonObject) -> str | None:
    value = task.get("result")
    if value is None:
        return None
    if isinstance(value, str):
        json.loads(value)
        return value
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    raise ValueError("Orca Task result must be a JSON object or encoded object")


def _completed_dependency(stage: StageRecord, stages: list[StageRecord]) -> list[str]:
    candidates = [
        item
        for item in stages
        if item.lane_id == stage.lane_id
        and item.created_at <= stage.created_at
        and item.stage_id != stage.stage_id
        and item.phase is StagePhase.COMPLETED
        and item.orca_task_id is not None
    ]
    if stage.finding_id is not None:
        finding_candidates = [item for item in candidates if item.finding_id == stage.finding_id]
        if finding_candidates:
            return [finding_candidates[-1].orca_task_id]  # type: ignore[list-item]
    return [candidates[-1].orca_task_id] if candidates else []  # type: ignore[list-item]


def _validate_discovered_findings(existing: list[FindingRecord], result: ReReviewResult) -> None:
    """Reject unknown or cyclic dependencies before persisting re-review discoveries."""

    dependencies = {
        finding.finding_id: list(finding.effective_contract.dependencies) for finding in existing
    }
    for discovered in result.new_findings:
        dependencies[discovered.finding.id] = list(discovered.finding.dependencies)
    known = set(dependencies)
    for finding_id, required in dependencies.items():
        unknown = set(required).difference(known)
        if unknown:
            raise ValueError(
                f"discovered finding {finding_id} depends on unknown findings: {sorted(unknown)}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(finding_id: str) -> None:
        if finding_id in visited:
            return
        if finding_id in visiting:
            raise ValueError("discovered finding dependencies must be acyclic")
        visiting.add(finding_id)
        for dependency in dependencies[finding_id]:
            visit(dependency)
        visiting.remove(finding_id)
        visited.add(finding_id)

    for finding_id in dependencies:
        visit(finding_id)


def _worker_name(lane_name: str, stage: StageRecord) -> str:
    suffix = stage.role.value.replace("_", "-")
    if stage.finding_id is not None:
        suffix = f"{stage.finding_id}-{suffix}-r{stage.round}"
    return f"{lane_name}-{suffix}"[:64]
