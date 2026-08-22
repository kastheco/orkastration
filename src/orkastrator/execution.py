"""Dynamic finding scheduling and Orca lifecycle reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from orkastrator.config import AgentProfile, GraphConfig
from orkastrator.git import GitError, LocalGit
from orkastrator.models import (
    AcceptanceAuthorization,
    AllowedWriteScope,
    AttemptKind,
    CiFailureFinding,
    CiReceipt,
    EscalationDecision,
    FindingEvidence,
    FindingLocation,
    FindingPhase,
    FindingReason,
    FindingRecord,
    FixAttempt,
    GraphResult,
    InitialReviewReport,
    LanePhase,
    LaneProposal,
    LaneRecord,
    OrcaWorkerResult,
    PendingQuestion,
    ProposalReceipt,
    PublicationReceipt,
    ReReviewResult,
    ReviewFinding,
    ReviewRevision,
    StageKind,
    StageLaunch,
    StagePhase,
    StageRecord,
    SupervisorPlan,
    ValidationRequirement,
    WorkerResult,
)
from orkastrator.orca import JsonObject, OrcaError
from orkastrator.publication import GitHubPublisher, LanePublisher, PublicationError
from orkastrator.scope import path_allowed
from orkastrator.store import IntegrationBusyError, StateStore


class OrcaGraphController(Protocol):
    """Narrow Orca orchestration surface used by orkastrator."""

    async def create_run(self, objective: str) -> tuple[str, JsonObject]: ...

    async def runs(self) -> list[JsonObject]: ...

    async def use_run(self, orca_run_id: str) -> JsonObject: ...

    async def messages(self, orca_run_id: str, limit: int = 200) -> list[JsonObject]: ...

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
        base_ref: str | None = None,
        parent_worktree_id: str | None = None,
    ) -> tuple[str, str, JsonObject]: ...

    async def release_worker(self, dispatch_id: str) -> JsonObject: ...

    async def worker_dispatch(self, task_id: str) -> tuple[str, str | None] | None: ...


@dataclass(frozen=True, slots=True)
class WorkerPlacement:
    """Named Orca checkout placement for one workflow stage."""

    worktree_id: str | None
    base_ref: str | None = None
    parent_worktree_id: str | None = None


class ExecutionController:
    """Turn accepted proposals into persisted, bounded convergence workflows."""

    def __init__(
        self,
        *,
        config: GraphConfig,
        orca: OrcaGraphController,
        store: StateStore,
        git: LocalGit | None = None,
        publisher: LanePublisher | None = None,
    ):
        self._config = config
        self._orca = orca
        self._store = store
        self._git = git or LocalGit()
        self._publisher = publisher or GitHubPublisher()

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
            self._store.record_acceptance_authorization(
                run_id,
                AcceptanceAuthorization(
                    run_id=run_id,
                    proposal_sha256=_sha256_json(run.proposal.model_dump(mode="json")),
                    config_sha256=_sha256_json(self._config.model_dump(mode="json")),
                ),
            )
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
        else:
            self._require_authorization(run_id)
            orca_run_id = self._store.run(run_id).orca_run_id
            if orca_run_id is None:
                raise ValueError(f"run {run_id} was accepted without an Orca Run")
        # Task and worker commands resolve against the Run bound to this coordinator
        # terminal. Recovering an existing Run, or accepting from a later process,
        # leaves that binding pointing elsewhere, so rebind before touching Tasks.
        await self._orca.use_run(orca_run_id)
        await self._ensure_tasks(run_id)
        return await self.monitor(run_id)

    async def monitor(self, run_id: str) -> GraphResult:
        """Reconcile results, materialize next work, and start one safe wave."""

        run = self._store.run(run_id)
        if run.orca_run_id is None:
            raise ValueError(f"run {run_id} has not been accepted")
        self._require_authorization(run_id)
        await self._orca.use_run(run.orca_run_id)
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
                worktree_id = _recovered_worktree(
                    stage, recovered_worktree, lanes[stage.lane_id].worktree_id
                )
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
                worktree_id = _recovered_worktree(
                    stage, recovered_worktree, lanes[stage.lane_id].worktree_id
                )
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

        await self._process_results(run_id)
        await self._release_settled(run_id)
        await self._reclaim_foreign_reservations(run_id)
        self._ensure_dynamic_stages(run_id)
        await self._ensure_tasks(run_id)
        started = await self._start_ready(run_id)
        await self._advance_publication(run_id)
        self._ensure_dynamic_stages(run_id)
        await self._ensure_tasks(run_id)
        started.extend(await self._start_ready(run_id))
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
            publications=self._store.publications(run_id),
            ci=self._store.ci_receipts(run_id),
            questions=await self._pending_questions(run_id, run.orca_run_id),
        )

    async def _pending_questions(self, run_id: str, orca_run_id: str) -> list[PendingQuestion]:
        """Report every unanswered question or escalation raised inside this run.

        Task state calls a blocked agent dispatched, because it is: sitting in its
        terminal waiting on a decision nobody knows it asked for. Orca already types
        that traffic, so read it here instead of leaving the supervisor to notice by
        accident. Attribution to a stage is best-effort - a message that names no
        known dispatch is still reported, because an unattributable blocked agent is
        the one most worth seeing.
        """

        stages = {
            stage.orca_dispatch_id: stage
            for stage in self._store.stages(run_id)
            if stage.orca_dispatch_id is not None
        }
        lanes = {lane.lane_id: lane for lane in self._store.lanes(run_id)}
        messages = await self._orca.messages(orca_run_id)
        answered = {
            thread
            for message in messages
            if isinstance(thread := message.get("thread_id"), str) and thread != message.get("id")
        }
        pending: list[PendingQuestion] = []
        for message in messages:
            kind = message.get("type")
            message_id = message.get("id")
            if kind not in {"question", "escalation"} or not isinstance(message_id, str):
                continue
            if message_id in answered:
                continue
            handle = str(message.get("from_handle") or "")
            stage = next((found for dispatch, found in stages.items() if dispatch in handle), None)
            pending.append(
                PendingQuestion(
                    message_id=message_id,
                    kind=cast(Literal["question", "escalation"], kind),
                    from_handle=handle,
                    lane=lanes[stage.lane_id].name if stage else None,
                    role=stage.role if stage else None,
                    dispatch_id=stage.orca_dispatch_id if stage else None,
                    asked_at=str(message.get("created_at") or ""),
                    subject=str(message.get("subject") or ""),
                    body=str(message.get("body") or ""),
                )
            )
        pending.sort(key=lambda question: question.asked_at)
        return pending

    async def _process_results(self, run_id: str) -> None:
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
                await self._apply_contract(run_id, stage, result)
            except IntegrationBusyError:
                continue
            except (GitError, ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._reject_stage(run_id, stage, f"invalid structured result: {exc}")
                continue
            self._store.record_lifecycle_receipt(run_id, stage, result)

    async def _apply_contract(
        self, run_id: str, stage: StageRecord, lifecycle: OrcaWorkerResult
    ) -> None:
        if stage.role is StageKind.WORKER:
            result = WorkerResult.model_validate_json(lifecycle.body)
            await self._validate_worker_result(run_id, stage, result)
            self._store.record_worker_result(run_id, stage.lane_id, result)
            return
        if stage.role is StageKind.INITIAL_REVIEWER:
            report = InitialReviewReport.model_validate_json(lifecycle.body)
            worker = self._store.worker_result(stage.lane_id)
            verified = await self._verified_review(stage, report, worker.review_revision)
            self._store.record_initial_review(run_id, stage.lane_id, verified)
            return
        finding = self._finding_for_stage(run_id, stage)
        if stage.round != finding.round:
            raise ValueError(
                f"stage round {stage.round} does not match finding round {finding.round}"
            )
        if stage.role is StageKind.FIXER:
            await self._apply_fix(
                run_id, finding, stage, FixAttempt.model_validate_json(lifecycle.body)
            )
        elif stage.role is StageKind.RE_REVIEWER:
            await self._apply_re_review(
                run_id, finding, stage, ReReviewResult.model_validate_json(lifecycle.body)
            )
        elif stage.role is StageKind.ESCALATION:
            self._apply_escalation(
                run_id, finding, stage, EscalationDecision.model_validate_json(lifecycle.body)
            )

    async def _verified_review(
        self,
        stage: StageRecord,
        report: InitialReviewReport,
        frozen: ReviewRevision,
    ) -> InitialReviewReport:
        """Bind one review to the worker changeset Git can prove it read.

        base_sha and head_sha are facts about commits: a reviewer naming different
        ones reviewed something else, and that stays fatal. diff_sha256 is not a
        fact about commits but a digest of whatever `git diff` invocation the
        reviewer happened to run, and dropping --full-index changes those bytes
        without changing a line of the diff. Requiring two agents to type the same
        command failed whole runs over identical content, so verify the reviewer's
        own checkout against the frozen digest instead, then restate the review on
        the frozen revision so every finding contract downstream carries one.
        """

        reported = report.review_revision
        if (reported.base_sha, reported.head_sha) != (frozen.base_sha, frozen.head_sha):
            raise ValueError("initial review revision does not match the worker changeset")
        if stage.worktree_id is None or not await self._git.is_clean(stage.worktree_id):
            raise ValueError("initial review checkout contains uncommitted changes")
        if await self._git.head(stage.worktree_id) != frozen.head_sha:
            raise ValueError("initial review checkout drifted from the frozen worker head")
        observed = await self._git.diff_sha256(stage.worktree_id, frozen.base_sha, frozen.head_sha)
        if observed != frozen.diff_sha256:
            raise ValueError("initial review checkout does not reproduce the frozen worker diff")
        if reported == frozen:
            return report
        return report.model_copy(
            update={
                "review_revision": frozen,
                "findings": [
                    finding.model_copy(update={"review_revision": frozen})
                    for finding in report.findings
                ],
            }
        )

    async def _apply_fix(
        self, run_id: str, finding: FindingRecord, stage: StageRecord, attempt: FixAttempt
    ) -> None:
        if attempt.finding_id != finding.finding_id or attempt.round != finding.round:
            raise ValueError("fix attempt does not match its persisted finding and round")
        self._store.record_fix_attempt(run_id, finding, stage, attempt)
        if attempt.status == "fixed":
            if not await self._validate_fixer_commit(run_id, finding, stage, attempt):
                self._escalate(run_id, finding, FindingReason.SCOPE_ESCAPE)
                return
            if finding.origin == "ci_failure":
                if stage.worktree_id is None:
                    raise ValueError("CI fixer omitted its isolated worktree")
                await self._integrate_fix(
                    run_id, finding, attempt, fixer_worktree=stage.worktree_id
                )
            else:
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

    async def _apply_re_review(
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
        if stage.worktree_id is None:
            raise ValueError("re-review stage omitted its isolated fixer worktree")
        if not await self._git.is_clean(stage.worktree_id):
            raise ValueError("re-review worktree contains uncommitted changes")
        if await self._git.head(stage.worktree_id) != result.reviewed_commit_sha:
            raise ValueError("re-review worktree is not pinned to the persisted fixer commit")
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
            await self._integrate_fix(run_id, finding, attempt)
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
                    run_id, finding.finding_key, phase=FindingPhase.PENDING_COMPOSITE
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
            self._settle_predecessors(run_id, finding, FindingPhase.DEFERRED)
        else:
            self._store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)
            self._settle_predecessors(run_id, finding, FindingPhase.BLOCKED)

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

    async def _validate_fixer_commit(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        attempt: FixAttempt,
    ) -> bool:
        """Verify the exact fixer head and enforce portable literal path boundaries."""

        if stage.worktree_id is None or attempt.commit_sha is None:
            raise ValueError("fixed attempt omitted its isolated worktree or commit")
        expected_base = self._fix_base_sha(finding)
        actual_head = await self._git.head(stage.worktree_id)
        if not await self._git.is_clean(stage.worktree_id):
            raise ValueError("fixer worktree contains uncommitted changes")
        if attempt.base_sha != expected_base or actual_head != attempt.commit_sha:
            raise ValueError("fixer result is not pinned to its assigned base and exact head")
        if not await self._git.is_ancestor(stage.worktree_id, attempt.base_sha, attempt.commit_sha):
            raise ValueError("fixer commit does not descend from its assigned base")
        if (
            await self._git.commit_count(stage.worktree_id, attempt.base_sha, attempt.commit_sha)
            != 1
        ):
            raise ValueError("fixer attempt must produce exactly one commit")
        actual_paths = await self._git.changed_paths(
            stage.worktree_id, attempt.base_sha, attempt.commit_sha
        )
        declared_paths = sorted(set(attempt.changed_paths))
        allowed = finding.effective_contract.allowed_write_scope.paths
        forbidden = finding.effective_contract.forbidden_scope
        accepted = actual_paths == declared_paths and all(
            path_allowed(path, allowed) and not path_allowed(path, forbidden)
            for path in actual_paths
        )
        self._store.record_scope_check(
            run_id,
            finding,
            declared_paths=declared_paths,
            actual_paths=actual_paths,
            accepted=accepted,
        )
        return accepted

    async def _integrate_fix(
        self,
        run_id: str,
        finding: FindingRecord,
        attempt: FixAttempt,
        *,
        fixer_worktree: str | None = None,
    ) -> None:
        """Serially integrate one re-review-approved commit into the lane checkout."""

        if attempt.commit_sha is None:
            raise ValueError("approved fixer attempt omitted its commit")
        lane = next(item for item in self._store.lanes(run_id) if item.lane_id == finding.lane_id)
        if lane.worktree_id is None or lane.integration_head_sha is None:
            raise ValueError("lane omitted its integration checkout or frozen head")
        fixer_worktree = fixer_worktree or self._fixer_worktree(run_id, finding)
        _, source_commits, source_finding_ids = await self._integration_sources(
            run_id, finding, attempt, fixer_worktree
        )
        receipt = self._store.integration(finding.finding_key, finding.round)
        if receipt is None:
            receipt = self._store.begin_integration(
                run_id,
                finding,
                fixer_commit_sha=attempt.commit_sha,
                source_commits=source_commits,
                source_finding_ids=source_finding_ids,
                base_sha=lane.integration_head_sha,
            )
        elif (
            receipt.fixer_commit_sha != attempt.commit_sha
            or receipt.source_commits != source_commits
            or receipt.source_finding_ids != source_finding_ids
        ):
            raise ValueError("integration sources changed after persistence")
        if receipt.status == "integrated":
            self._settle_source_findings(
                run_id,
                finding.lane_id,
                receipt.source_finding_ids,
                FindingPhase.RESOLVED,
            )
            return
        if receipt.status == "conflict":
            self._escalate(run_id, finding, FindingReason.INTEGRATION_CONFLICT)
            return
        if receipt.status == "validation_failed":
            self._escalate(run_id, finding, FindingReason.VALIDATION_FAILED)
            return

        integrated_sha = await self._git.find_cherry_pick(lane.worktree_id, attempt.commit_sha)
        if integrated_sha is None:
            active_sequence = await self._git.cherry_pick_in_progress_commits(lane.worktree_id)
            if active_sequence is not None:
                if not active_sequence or not set(active_sequence).issubset(receipt.source_commits):
                    self._store.finish_integration(
                        run_id,
                        finding,
                        status="conflict",
                        integrated_sha=None,
                        validation_results=[],
                    )
                    self._escalate(run_id, finding, FindingReason.INTEGRATION_CONFLICT)
                    return
                await self._git.abort_cherry_pick(lane.worktree_id)
            current_head = await self._git.head(lane.worktree_id)
            if current_head != receipt.base_sha or not await self._git.is_clean(lane.worktree_id):
                self._store.finish_integration(
                    run_id,
                    finding,
                    status="conflict",
                    integrated_sha=None,
                    validation_results=[],
                )
                self._escalate(run_id, finding, FindingReason.INTEGRATION_CONFLICT)
                return
            applied = await self._git.cherry_pick_many(lane.worktree_id, source_commits)
            if applied.returncode != 0:
                await self._git.abort_cherry_pick(lane.worktree_id)
                self._store.finish_integration(
                    run_id,
                    finding,
                    status="conflict",
                    integrated_sha=None,
                    validation_results=[],
                )
                self._escalate(run_id, finding, FindingReason.INTEGRATION_CONFLICT)
                return
            integrated_sha = await self._git.head(lane.worktree_id)

        validation = await self._git.validate(
            lane.worktree_id,
            self._integrated_validation_requirements(run_id, finding.lane_id, source_finding_ids),
        )
        if any(result.status != "passed" for result in validation):
            self._store.finish_integration(
                run_id,
                finding,
                status="validation_failed",
                integrated_sha=integrated_sha,
                validation_results=validation,
            )
            self._escalate(run_id, finding, FindingReason.VALIDATION_FAILED)
            return
        self._store.finish_integration(
            run_id,
            finding,
            status="integrated",
            integrated_sha=integrated_sha,
            validation_results=validation,
        )
        self._settle_source_findings(
            run_id,
            finding.lane_id,
            receipt.source_finding_ids,
            FindingPhase.RESOLVED,
        )

    async def _validate_worker_result(
        self, run_id: str, stage: StageRecord, result: WorkerResult
    ) -> None:
        """Freeze only a Git-verified lane head, ancestry, path set, and diff identity."""

        if stage.worktree_id is None:
            raise ValueError("worker result omitted its lane worktree")
        if not await self._git.is_clean(stage.worktree_id):
            raise ValueError("worker worktree contains uncommitted changes")
        revision = result.review_revision
        lane = next(item for item in self._store.lanes(run_id) if item.lane_id == stage.lane_id)
        if await self._git.resolve_ref(stage.worktree_id, lane.base_ref) != revision.base_sha:
            raise ValueError("worker review base does not match the configured lane base_ref")
        if await self._git.head(stage.worktree_id) != revision.head_sha:
            raise ValueError("worker review head does not match the lane checkout")
        if not await self._git.is_ancestor(stage.worktree_id, revision.base_sha, revision.head_sha):
            raise ValueError("worker review head does not descend from its base")
        actual_paths = await self._git.changed_paths(
            stage.worktree_id, revision.base_sha, revision.head_sha
        )
        if actual_paths != sorted(set(result.changed_paths)):
            raise ValueError("worker changed paths do not match the frozen Git diff")
        actual_diff = await self._git.diff_sha256(
            stage.worktree_id, revision.base_sha, revision.head_sha
        )
        if actual_diff != revision.diff_sha256:
            raise ValueError("worker diff digest does not match the frozen Git diff")

    async def _integration_sources(
        self,
        run_id: str,
        finding: FindingRecord,
        attempt: FixAttempt,
        fixer_worktree: str,
    ) -> tuple[str, list[str], list[str]]:
        """Resolve an introduced-fix ancestry chain into auditable source commits."""

        if attempt.commit_sha is None:
            raise ValueError("integration source omitted its fixer commit")
        findings = self._store.findings(run_id, finding.lane_id)
        source_findings = [finding]
        source_base = finding.effective_contract.review_revision.head_sha
        visited = {finding.finding_key}
        while True:
            predecessor = next(
                (
                    item
                    for item in findings
                    if item.finding_key not in visited
                    and (prior := self._store.latest_fix_attempt(item.finding_key)) is not None
                    and prior.commit_sha == source_base
                ),
                None,
            )
            if predecessor is None:
                break
            visited.add(predecessor.finding_key)
            source_findings.insert(0, predecessor)
            source_base = predecessor.effective_contract.review_revision.head_sha
        commits = await self._git.commits_between(fixer_worktree, source_base, attempt.commit_sha)
        if not commits or commits[-1] != attempt.commit_sha:
            raise ValueError("accepted integration source chain does not end at the fixer commit")
        return source_base, commits, [item.finding_id for item in source_findings]

    def _integrated_validation_requirements(
        self, run_id: str, lane_id: str, source_finding_ids: list[str]
    ) -> list[ValidationRequirement]:
        """Return deduplicated checks for the complete integrated fix set."""

        finding_ids = set(source_finding_ids)
        for receipt in self._store.integrations(run_id):
            if receipt.lane_id == lane_id and receipt.status == "integrated":
                finding_ids.update(receipt.source_finding_ids)
        requirements: list[ValidationRequirement] = []
        seen: set[tuple[str, str]] = set()
        for item in self._store.findings(run_id, lane_id):
            if item.finding_id not in finding_ids:
                continue
            for requirement in item.effective_contract.validation:
                key = (requirement.command, requirement.expected)
                if key not in seen:
                    seen.add(key)
                    requirements.append(requirement)
        return sorted(requirements, key=lambda item: (item.command, item.expected))

    def _fixer_worktree(self, run_id: str, finding: FindingRecord) -> str:
        candidates = [
            stage
            for stage in self._store.stages(run_id)
            if stage.finding_id == finding.finding_id
            and stage.round == finding.round
            and stage.role is StageKind.FIXER
            and stage.processed
            and stage.worktree_id is not None
        ]
        if not candidates:
            raise ValueError("integration has no settled fixer worktree")
        assert candidates[-1].worktree_id is not None
        return candidates[-1].worktree_id

    def _settle_predecessors(
        self, run_id: str, finding: FindingRecord, phase: FindingPhase
    ) -> None:
        """Settle unresolved composite predecessors when their correction cannot integrate."""

        source_head = finding.effective_contract.review_revision.head_sha
        findings = self._store.findings(run_id, finding.lane_id)
        visited: set[str] = set()
        while True:
            predecessor = next(
                (
                    item
                    for item in findings
                    if item.finding_key not in visited
                    and item.phase is FindingPhase.PENDING_COMPOSITE
                    and (attempt := self._store.latest_fix_attempt(item.finding_key)) is not None
                    and attempt.commit_sha == source_head
                ),
                None,
            )
            if predecessor is None:
                return
            visited.add(predecessor.finding_key)
            self._store.set_finding_state(run_id, predecessor.finding_key, phase=phase)
            source_head = predecessor.effective_contract.review_revision.head_sha

    def _settle_source_findings(
        self,
        run_id: str,
        lane_id: str,
        finding_ids: list[str],
        phase: FindingPhase,
    ) -> None:
        """Idempotently settle every finding represented by an integration receipt."""

        selected = set(finding_ids)
        for item in self._store.findings(run_id, lane_id):
            if item.finding_id in selected:
                self._store.set_finding_state(run_id, item.finding_key, phase=phase)

    def _fix_base_sha(self, finding: FindingRecord) -> str:
        return finding.effective_contract.review_revision.head_sha

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
            if worker_done and lane.phase is not LanePhase.FAILED and not reviewer_exists:
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

    async def _reclaim_foreign_reservations(self, run_id: str) -> None:
        """Release start reservations another run abandoned before Orca confirmed them.

        A worker start that crashes between reserving capacity and recording its
        Dispatch leaves the stage STARTING. Capacity is counted across every run, so
        an abandoned reservation blocks this run's wave until its own run is
        monitored again, which may never happen. Ask Orca whether a Dispatch exists
        and reset the ones that were never really started.
        """

        for owner_run_id, stage in self._store.foreign_reservations(run_id):
            if stage.orca_task_id is None:
                continue
            try:
                recovered = await self._orca.worker_dispatch(stage.orca_task_id)
            except OrcaError:
                # The other run's Task may be gone entirely; leave its reservation
                # alone rather than guessing, and keep reconciling this run.
                continue
            if recovered is None:
                self._store.reset_stage_reservation(owner_run_id, stage)

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
                max_lane_fixers=self._config.review_cycle.parallel_fixers.max_per_lane,
                write_paths=(
                    tuple(
                        self._finding_for_stage(
                            run_id, stage
                        ).effective_contract.allowed_write_scope.paths
                    )
                    if stage.role is StageKind.FIXER
                    else ()
                ),
            ):
                continue
            if stage.orca_task_id is None:
                raise ValueError(f"ready stage {stage.stage_id} has no Orca task")
            lane = lanes[stage.lane_id]
            placement = self._placement(run_id, lane, stage)
            dispatch_id, worktree_id, payload = await self._orca.start_worker(
                task_id=stage.orca_task_id,
                lane_name=_worker_name(lane.name, stage),
                repo_selector=lane.repo_selector,
                worktree_id=placement.worktree_id,
                base_ref=placement.base_ref,
                parent_worktree_id=placement.parent_worktree_id,
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

    def _placement(self, run_id: str, lane: LaneRecord, stage: StageRecord) -> WorkerPlacement:
        """Resolve the exact existing or isolated checkout for one role."""

        if stage.role is StageKind.WORKER:
            return WorkerPlacement(worktree_id=None, base_ref=lane.base_ref)
        if stage.role is StageKind.FIXER:
            if lane.worktree_id is None:
                raise ValueError("fixer lane has no integration checkout")
            finding = self._finding_for_stage(run_id, stage)
            return WorkerPlacement(
                worktree_id=None,
                base_ref=self._fix_base_sha(finding),
                parent_worktree_id=lane.worktree_id,
            )
        if stage.role is StageKind.RE_REVIEWER:
            candidates = [
                item
                for item in self._store.stages(run_id)
                if item.lane_id == stage.lane_id
                and item.finding_id == stage.finding_id
                and item.round == stage.round
                and item.role is StageKind.FIXER
                and item.processed
                and item.worktree_id is not None
            ]
            if not candidates:
                raise ValueError("re-review has no settled isolated fixer worktree")
            return WorkerPlacement(worktree_id=candidates[-1].worktree_id)
        return WorkerPlacement(worktree_id=lane.worktree_id)

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
            elif lane.phase is LanePhase.BLOCKED or any(
                finding.phase is FindingPhase.BLOCKED for finding in lane_findings
            ):
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
                publications = self._store.publications(run_id, lane.lane_id)
                ci = self._store.ci_receipts(run_id, lane.lane_id)
                published_head = publications[-1].head_sha if publications else None
                passed_head = next(
                    (item.head_sha for item in reversed(ci) if item.status == "passed"), None
                )
                phase = (
                    LanePhase.COMPLETE
                    if reviewed
                    and settled
                    and published_head == lane.integration_head_sha == passed_head
                    and not publications[-1].draft
                    else LanePhase.ACTIVE
                )
            self._store.set_lane_phase(run_id, lane.lane_id, phase)
            lane_phases.append(phase)
        if any(phase is LanePhase.FAILED for phase in lane_phases):
            return "failed"
        if any(phase is LanePhase.BLOCKED for phase in lane_phases):
            return "blocked"
        if lane_phases and all(phase is LanePhase.COMPLETE for phase in lane_phases):
            return "complete"
        return "active"

    async def _advance_publication(self, run_id: str) -> None:
        """Publish locally settled lanes and gate them on checks for the exact head."""

        self._require_authorization(run_id)
        for lane in self._store.lanes(run_id):
            if lane.phase in {LanePhase.BLOCKED, LanePhase.FAILED}:
                continue
            if not self._local_lane_settled(run_id, lane):
                continue
            if lane.integration_head_sha is None:
                self._store.block_lane(run_id, lane.lane_id, "lane has no integrated head")
                continue
            publications = self._store.publications(run_id, lane.lane_id)
            previous = publications[-1] if publications else None
            try:
                if previous is None or previous.head_sha != lane.integration_head_sha:
                    receipt = await self._publisher.publish(
                        run_id=run_id,
                        lane=lane,
                        head_sha=lane.integration_head_sha,
                        previous=previous,
                    )
                    if receipt.run_id != run_id or receipt.lane != lane.name:
                        raise PublicationError("publisher receipt does not match the accepted lane")
                    if receipt.head_sha != lane.integration_head_sha:
                        raise PublicationError(
                            "publisher receipt does not match the integrated head"
                        )
                    self._store.record_publication(run_id, lane.lane_id, receipt)
                    publications.append(receipt)
                else:
                    receipt = previous
                ci = await self._publisher.checks(receipt)
                if ci.head_sha != receipt.head_sha:
                    raise PublicationError("CI observation is not pinned to the published head")
                self._store.record_ci_receipt(run_id, lane.lane_id, ci)
                if ci.status == "passed":
                    ready = await self._publisher.mark_ready(receipt)
                    if ready.head_sha != receipt.head_sha or ready.draft:
                        raise PublicationError("provider did not mark the exact pull request ready")
                    self._store.record_publication(run_id, lane.lane_id, ready)
                elif ci.status == "failed":
                    await self._record_ci_failure(run_id, lane, publications, ci)
            except PublicationError as exc:
                self._store.block_lane(run_id, lane.lane_id, str(exc))

    def _local_lane_settled(self, run_id: str, lane: LaneRecord) -> bool:
        stages = [item for item in self._store.stages(run_id) if item.lane_id == lane.lane_id]
        reviewed = any(
            item.role is StageKind.INITIAL_REVIEWER and item.processed for item in stages
        )
        findings = self._store.findings(run_id, lane.lane_id)
        return reviewed and all(
            item.phase in {FindingPhase.RESOLVED, FindingPhase.DEFERRED} for item in findings
        )

    def _require_authorization(self, run_id: str) -> AcceptanceAuthorization:
        """Reject resumed work when its frozen proposal or graph policy changed."""

        run = self._store.run(run_id)
        expected = AcceptanceAuthorization(
            run_id=run_id,
            proposal_sha256=_sha256_json(run.proposal.model_dump(mode="json")),
            config_sha256=_sha256_json(self._config.model_dump(mode="json")),
        )
        authorization = self._store.acceptance_authorization(run_id)
        if authorization is None:
            raise ValueError(f"run {run_id} has no frozen acceptance authorization")
        if authorization != expected:
            raise ValueError(
                f"run {run_id} proposal or graph policy changed after acceptance; "
                "record and accept a new proposal"
            )
        return authorization

    async def _record_ci_failure(
        self,
        run_id: str,
        lane: LaneRecord,
        publications: list[PublicationReceipt],
        ci: CiReceipt,
    ) -> None:
        failures = self._store.ci_failures(run_id, lane.lane_id)
        if any(item.published_sha == ci.head_sha for item in failures):
            return
        round_number = len(failures) + 1
        if round_number > self._config.final_gate.on_failure.max_fix_rounds:
            self._store.block_lane(run_id, lane.lane_id, "CI fix round limit exhausted")
            return
        previous_head = publications[-2].head_sha if len(publications) > 1 else None
        integrations = [
            item
            for item in self._store.integrations(run_id)
            if item.lane_id == lane.lane_id
            and item.status == "integrated"
            and (previous_head is None or item.base_sha == previous_head)
        ]
        if len(integrations) > 1:
            self._store.block_lane(
                run_id,
                lane.lane_id,
                "CI failure attribution is ambiguous across multiple integrated fixes",
            )
            return
        worker = self._store.worker_result(lane.lane_id)
        if integrations:
            implicated = integrations[0].source_commits
            finding_ids = set(integrations[0].source_finding_ids)
            paths = sorted(
                {
                    path
                    for finding in self._store.findings(run_id, lane.lane_id)
                    if finding.finding_id in finding_ids
                    for path in finding.effective_contract.allowed_write_scope.paths
                }
            )
        else:
            implicated = []
            paths = worker.changed_paths
        if not paths:
            self._store.block_lane(run_id, lane.lane_id, "CI failure has no deterministic scope")
            return
        failing = [item for item in ci.checks if item.status in {"failed", "cancelled"}]
        failure_id = f"ci-finding-{round_number}"
        typed = CiFailureFinding(
            id=failure_id,
            published_sha=ci.head_sha,
            failing_checks=failing,
            implicated_fix_commits=implicated,
            allowed_write_scope=AllowedWriteScope(paths=paths),
            round=round_number,
        )
        if not self._store.record_ci_failure(run_id, lane.lane_id, typed):
            return
        base_sha = worker.review_revision.base_sha
        review_revision = ReviewRevision(
            base_sha=base_sha,
            head_sha=ci.head_sha,
            diff_sha256=await self._git.diff_sha256(lane.worktree_id or "", base_sha, ci.head_sha),
        )
        detail = "\n".join(f"{item.name}: {item.output or item.status}" for item in failing)[:4_000]
        contract = ReviewFinding(
            id=failure_id,
            review_revision=review_revision,
            evidence=[
                FindingEvidence(
                    location=FindingLocation(path=path, start_line=1, end_line=1),
                    claim=detail or "Required CI failed for the published head.",
                )
                for path in paths[:64]
            ],
            failure_mode=detail or "Required CI failed for the published head.",
            required_outcome="Make every required CI check pass for the next exact published head.",
            allowed_write_scope=typed.allowed_write_scope,
        )
        self._store.add_finding(run_id, lane.lane_id, contract, origin="ci_failure")

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
                "Implement the scoped issue, verify it, and commit the result. Compute "
                "diff_sha256 over the raw output of `git diff --binary --full-index "
                "<base>..<head> --` and report the exact sorted changed paths. Send worker_done "
                "with an explicit outcome and body set to only the changeset JSON matching "
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
            prior = self._store.fix_attempt(finding.finding_key, finding.round - 1)
            return (
                f"Fix only this frozen finding at round {finding.round}: {contract}\n"
                f"Prior-round evidence: {prior.model_dump_json() if prior else 'none'}\n"
                "Start from the assigned frozen review head and produce exactly one commit that "
                "fully resolves the finding. Do not widen scope. Send worker_done with body set "
                "to only the JSON contract "
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

    return f"{objective}\n\norkastrator run: {run_id}"


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


def _recovered_worktree(
    stage: StageRecord, recovered: str | None, lane_worktree: str | None
) -> str | None:
    """Never reinterpret a missing isolated fixer identity as the lane checkout."""

    if stage.role in {StageKind.FIXER, StageKind.RE_REVIEWER}:
        return recovered
    return recovered or lane_worktree


def _sha256_json(value: object) -> str:
    """Return a stable digest for one accepted structured value."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
