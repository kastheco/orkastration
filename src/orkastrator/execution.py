"""Dynamic finding scheduling and Orca lifecycle reconciliation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from orkastrator.config import AgentProfile, GraphConfig, config_changes
from orkastrator.git import GitError, LocalGit
from orkastrator.models import (
    SHELL_OPERATORS,
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
    FixAttemptIdentity,
    GraphResult,
    InitialReviewReport,
    LanePhase,
    LaneProposal,
    LaneRecord,
    OrcaWorkerResult,
    OverdueStage,
    PendingQuestion,
    PolicyReauthorization,
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
    ValidationResult,
    WorkerBlocked,
    WorkerResult,
)
from orkastrator.orca import MAX_TASK_SPEC_BYTES, JsonObject, OrcaError, OrcaTimeout
from orkastrator.publication import (
    GitHubPublisher,
    IntegrationConflict,
    LanePublisher,
    PublicationError,
    PullRequestLanded,
)
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

    async def create_worktree(
        self,
        *,
        lane_name: str,
        repo_selector: str,
        base_ref: str | None = None,
        parent_worktree_id: str | None = None,
    ) -> tuple[str, JsonObject]: ...

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
    ) -> tuple[str, str, str | None, JsonObject]: ...

    async def release_worker(
        self, dispatch_id: str, terminal_handle: str | None = None
    ) -> JsonObject: ...

    async def worker_turns(self, dispatch_id: str, limit: int = 50) -> list[str]: ...

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
                config=self._config.model_dump(mode="json"),
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
                await self._prepare_worker_checkout(
                    run_id, lanes[stage.lane_id], stage, worktree_id
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
                await self._prepare_worker_checkout(
                    run_id, lanes[stage.lane_id], stage, worktree_id
                )
                self._store.mark_stage_started(
                    run_id,
                    stage.stage_id,
                    dispatch_id,
                    worktree_id,
                    {"recovered": True, "dispatchId": dispatch_id},
                )
            result_json = _task_result_json(task)
            self._store.sync_stage(
                run_id,
                stage.stage_id,
                phase,
                result_json,
            )
            # Orca hands a Task back to READY once its worker terminal is gone.
            # For a stage that never reported, that worker died: the agent
            # crashed, or the supervisor exited while its workers were live.
            # The Dispatch id it still carries is dead, and `_start_ready`
            # refuses to start a stage that has one, so clear it and let the
            # stage be dispatched again - unless the worktree says the work was
            # already done, because then re-dispatching destroys a run rather
            # than recovering one.
            if phase is StagePhase.READY and stage.orca_dispatch_id is not None:
                if await self._holds_unreported_work(stage):
                    self._store.hold_unreported_dispatch(run_id, stage)
                else:
                    self._store.release_dead_dispatch(run_id, stage)

        overdue = await self._enforce_stage_budgets(run_id)
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
            overdue=overdue,
        )

    async def questions(self, run_id: str) -> list[PendingQuestion]:
        """Return every unanswered question on this run, body included.

        The monitor line reports these as a count and a subject, which is the
        right density for a status line and the wrong one for deciding what to
        say back. This is the read that carries the body and the message id.
        """

        run = self._store.run(run_id)
        if run.orca_run_id is None:
            raise ValueError(f"run {run_id} has not been accepted")
        return await self._pending_questions(run_id, run.orca_run_id)

    async def answer(self, run_id: str, message_id: str, body: str) -> PendingQuestion:
        """Answer one blocked agent, and record that the supervisor did.

        The message has to be pending on this run. Sending to an arbitrary id
        would let a typo direct an agent in a different run, and a question that
        already has an answer must not collect a second one - an agent reading a
        thread cannot tell which of two directions is current.
        """

        pending = await self.questions(run_id)
        question = next((item for item in pending if item.message_id == message_id), None)
        if question is None:
            known = ", ".join(item.message_id for item in pending) or "none"
            raise ValueError(
                f"{message_id} is not an unanswered question on this run; pending: {known}"
            )
        run = self._store.run(run_id)
        assert run.orca_run_id is not None
        await self._orca.reply(run.orca_run_id, message_id, body)
        lanes = {lane.name: lane.lane_id for lane in self._store.lanes(run_id)}
        self._store.record_supervisor_answer(
            run_id, lanes.get(question.lane or ""), message_id, body
        )
        return question

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
            payload = json.loads(lifecycle.body)
            if isinstance(payload, dict) and payload.get("status") == "blocked":
                await self._apply_worker_block(run_id, stage, WorkerBlocked.model_validate(payload))
                return
            result = WorkerResult.model_validate(payload)
            await self._validate_worker_result(run_id, stage, result)
            self._store.record_worker_result(run_id, stage.lane_id, result)
            return
        if stage.role is StageKind.INITIAL_REVIEWER:
            verified = self._store.initial_review(stage.lane_id)
            if verified is None:
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
            await self._apply_escalation(
                run_id, finding, stage, EscalationDecision.model_validate_json(lifecycle.body)
            )

    async def _verified_review(
        self,
        stage: StageRecord,
        report: InitialReviewReport,
        frozen: ReviewRevision,
    ) -> InitialReviewReport:
        """Bind one review to the worker changeset Git can prove it read.

        Which changeset a reviewer read is a question for Git, not for the
        reviewer: its checkout either sits on the frozen head and reproduces the
        frozen diff or it does not, and that proof holds whatever the reviewer
        typed. Asking it to also transcribe the revision only added a way to fail
        - a digest retyped with its middle elided is not a different review - so
        take the reviewer's copy as a hint, verify the checkout, and restate the
        report on the frozen revision so every downstream contract carries one.
        """

        reported = report.review_revision
        if reported is not None and (reported.base_sha, reported.head_sha) != (
            frozen.base_sha,
            frozen.head_sha,
        ):
            raise ValueError("initial review revision does not match the worker changeset")
        governed = report.model_copy(
            update={
                "review_revision": frozen,
                "findings": [
                    finding.model_copy(
                        update={
                            "review_revision": frozen,
                            "validation": [
                                _govern_validation_requirement(requirement)
                                for requirement in finding.validation
                            ],
                        }
                    )
                    for finding in report.findings
                ],
            }
        )
        _reject_unrunnable_validation(governed)
        if stage.worktree_id is None or not await self._git.is_clean(stage.worktree_id):
            raise ValueError("initial review checkout contains uncommitted changes")
        if await self._git.head(stage.worktree_id) != frozen.head_sha:
            raise ValueError("initial review checkout drifted from the frozen worker head")
        observed = await self._git.diff_sha256(stage.worktree_id, frozen.base_sha, frozen.head_sha)
        if observed != frozen.diff_sha256:
            raise ValueError("initial review checkout does not reproduce the frozen worker diff")
        frozen_findings = await self._freeze_validation_baselines(
            stage.worktree_id, governed.findings
        )
        if not await self._git.is_clean(stage.worktree_id):
            raise ValueError("initial review validation dirtied its checkout")
        return governed.model_copy(update={"findings": frozen_findings})

    async def _freeze_validation_baselines(
        self, worktree_id: str, findings: list[ReviewFinding]
    ) -> list[ReviewFinding]:
        """Govern validation argv and observe it once before any fixer runs."""

        baselines: dict[tuple[str, str | None, int], ValidationResult] = {}
        frozen_findings: list[ReviewFinding] = []
        for finding in findings:
            requirements: list[ValidationRequirement] = []
            for requirement in finding.validation:
                key = (requirement.command, requirement.workdir, requirement.expect_exit)
                baseline = baselines.get(key)
                if baseline is None:
                    results = await self._git.validate(worktree_id, [requirement])
                    if not results:
                        raise ValueError("validation runner returned no baseline result")
                    baseline = results[0]
                    baselines[key] = baseline
                requirements.append(requirement.model_copy(update={"baseline_result": baseline}))
            frozen_findings.append(finding.model_copy(update={"validation": requirements}))
        return frozen_findings

    async def _apply_worker_block(
        self, run_id: str, stage: StageRecord, blocked: WorkerBlocked
    ) -> None:
        """Route a worker's undecidable choice through the graph's own adjudicator.

        A decision the lane contract does not answer is exactly the shape the
        finding machinery already carries: escalate it, let the configured
        escalation role adjudicate, and let an approved revision come back as a
        bounded fixer rather than as an unbounded worker resumed by hand.
        """

        if stage.worktree_id is None:
            raise ValueError("blocked worker omitted its isolated worktree")
        if not await self._git.is_clean(stage.worktree_id):
            raise ValueError("blocked worker left uncommitted changes in its lane")
        if await self._git.head(stage.worktree_id) != blocked.head_sha:
            raise ValueError("blocked worker head does not match its reported head")
        lane = next(item for item in self._store.lanes(run_id) if item.lane_id == stage.lane_id)
        finding_id = f"worker-decision-{len(self._store.findings(run_id, lane.lane_id)) + 1}"
        detail = "\n".join(
            [blocked.decision.question, *(f"- {option}" for option in blocked.decision.options)]
        )
        contract = ReviewFinding(
            id=finding_id,
            review_revision=ReviewRevision(
                base_sha=blocked.base_sha,
                head_sha=blocked.head_sha,
                diff_sha256=await self._git.diff_sha256(
                    stage.worktree_id, blocked.base_sha, blocked.head_sha
                ),
            ),
            evidence=[
                FindingEvidence(
                    location=FindingLocation(path=path, start_line=1, end_line=1),
                    claim=blocked.decision.consequence,
                )
                for path in blocked.decision.allowed_write_scope.paths[:64]
            ],
            failure_mode=f"{blocked.summary}\n\n{blocked.decision.consequence}"[:8_000],
            required_outcome=detail[:8_000],
            allowed_write_scope=blocked.decision.allowed_write_scope,
        )
        finding = self._store.add_finding(run_id, lane.lane_id, contract, origin="worker_blocked")
        self._escalate(run_id, finding, FindingReason.WORKER_DECISION)

    async def _apply_fix(
        self, run_id: str, finding: FindingRecord, stage: StageRecord, attempt: FixAttempt
    ) -> None:
        attempt = await self._bound_fix_attempt(finding, stage, attempt)
        persisted = self._store.fix_attempt_for_stage(stage.stage_id)
        if persisted is not None:
            attempt = persisted
        elif attempt.status == "fixed":
            if stage.worktree_id is None:
                raise ValueError("fixed attempt omitted its isolated worktree")
            observed = await self._observe_validation_requirements(
                stage.worktree_id, finding.effective_contract.validation
            )
            required_commands = {
                requirement.command for requirement in finding.effective_contract.validation
            }
            extra_evidence = [
                result
                for result in attempt.validation_results
                if result.command not in required_commands
            ][: 64 - len(observed)]
            attempt = FixAttempt.model_validate(
                {
                    **attempt.model_dump(mode="python"),
                    "validation_results": [*observed, *extra_evidence],
                }
            )
        self._store.record_fix_attempt(run_id, finding, stage, attempt)
        if attempt.status == "fixed":
            if not _validation_satisfied(
                finding, attempt
            ) and not _validation_failed_identically_at_base_and_head(finding, attempt):
                self._escalate(run_id, finding, FindingReason.VALIDATION_FAILED)
                return
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

    async def _observe_validation_requirements(
        self, worktree_id: str, requirements: list[ValidationRequirement]
    ) -> list[ValidationResult]:
        """Observe every requirement even though the runner stops at first failure."""

        observed: list[ValidationResult] = []
        for requirement in requirements:
            result = await self._git.validate(worktree_id, [requirement])
            if len(result) != 1:
                raise ValueError("validation runner returned an incomplete result")
            observed.append(result[0])
        return observed

    @staticmethod
    def _bind_revision(finding: FindingRecord, contract: ReviewFinding) -> ReviewFinding:
        """Stamp the frozen revision onto a contract that omitted or mangled it."""

        return contract.model_copy(
            update={"review_revision": finding.effective_contract.review_revision}
        )

    async def _bound_fix_attempt(
        self, finding: FindingRecord, stage: StageRecord, attempt: FixAttempt
    ) -> FixAttempt:
        """Restate one fixer report on the identity the supervisor assigned it.

        A fixer is the authority on what it did, never on which finding, round,
        or base it was handed: the supervisor chose all three and Git holds the
        resulting head. Rejecting an otherwise sound fix because an agent mistyped
        one of them threw away real work, so overwrite them and spend the rigour
        on `_validate_fixer_commit`, where Git can actually settle the question.
        """

        bound: dict[str, object] = {"finding_id": finding.finding_id, "round": finding.round}
        if attempt.status == "fixed" and stage.worktree_id is not None:
            bound["base_sha"] = self._fix_base_sha(finding)
            bound["commit_sha"] = await self._git.head(stage.worktree_id)
        return attempt.model_copy(update=bound)

    async def _apply_re_review(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        result: ReReviewResult,
    ) -> None:
        attempt = self._store.latest_fix_attempt(finding.finding_key)
        if attempt is None or attempt.commit_sha is None:
            raise ValueError("re-review has no persisted fixer commit to review")
        if stage.worktree_id is None:
            raise ValueError("re-review stage omitted its isolated fixer worktree")
        if not await self._git.is_clean(stage.worktree_id):
            raise ValueError("re-review worktree contains uncommitted changes")
        # The persisted attempt names the commit under review and the checkout
        # either sits on it or does not; a re-reviewer's transcription of any of
        # this decides nothing, so restate the verdict on what is already known.
        if await self._git.head(stage.worktree_id) != attempt.commit_sha:
            raise ValueError("re-review worktree is not pinned to the persisted fixer commit")
        # A finding discovered here is a fact about the fixer commit, not about
        # the lane's original review, so bind it to that commit rather than to
        # the parent's frozen revision - that range is what a later fixer must
        # start from, and it is what chains a composite integration together.
        discovered_revision = ReviewRevision(
            base_sha=self._fix_base_sha(finding),
            head_sha=attempt.commit_sha,
            diff_sha256=await self._git.diff_sha256(
                stage.worktree_id, self._fix_base_sha(finding), attempt.commit_sha
            ),
        )
        result = result.model_copy(
            update={
                "finding_id": finding.finding_id,
                "round": finding.round,
                "reviewed_commit_sha": attempt.commit_sha,
                "new_findings": [
                    discovered.model_copy(
                        update={
                            "finding": discovered.finding.model_copy(
                                update={"review_revision": discovered_revision}
                            )
                        }
                    )
                    for discovered in result.new_findings
                ],
            }
        )
        persisted = self._store.re_review_for_stage(stage.stage_id)
        if persisted is None:
            governed_discovery_findings = [
                item.finding.model_copy(
                    update={
                        "validation": [
                            _govern_validation_requirement(requirement)
                            for requirement in item.finding.validation
                        ]
                    }
                )
                for item in result.new_findings
            ]
            _reject_unrunnable_findings(governed_discovery_findings)
            governed_discoveries = await self._freeze_validation_baselines(
                stage.worktree_id, governed_discovery_findings
            )
            if not await self._git.is_clean(stage.worktree_id):
                raise ValueError("re-review validation dirtied its checkout")
            result = result.model_copy(
                update={
                    "new_findings": [
                        item.model_copy(update={"finding": governed})
                        for item, governed in zip(
                            result.new_findings, governed_discoveries, strict=True
                        )
                    ]
                }
            )
        else:
            result = persisted
        existing = self._store.findings(run_id, finding.lane_id)
        _validate_discovered_findings(existing, result)
        existing_ids = {item.finding_id for item in existing}
        collisions = {
            item.finding.id for item in result.new_findings if item.finding.id in existing_ids
        }
        # Asked of this stage, not of the round. A conflict retry stays in its own
        # round on purpose, and the re-review of the rebased commit is a different
        # verdict about a different commit; keyed on the round, the retry's own
        # re-review read as the first one being rewritten and was refused, which
        # left the rebase unable to finish what it was sent to do.
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

    async def _apply_escalation(
        self,
        run_id: str,
        finding: FindingRecord,
        stage: StageRecord,
        decision: EscalationDecision,
    ) -> None:
        if finding.escalation_reason is None:
            raise ValueError("escalation has no persisted trigger to adjudicate")
        # The supervisor chose the finding, the round, and the trigger before it
        # dispatched this adjudication, and a revised contract is a revision of a
        # frozen one. Take the verdict from the adjudicator and the identity from
        # the record, so a mistyped id or an elided digest cannot kill a finding.
        revised = decision.revised_finding
        decision = decision.model_copy(
            update={
                "finding_id": finding.finding_id,
                "round": finding.round,
                "reason": finding.escalation_reason.value,
                "revised_finding": (
                    None
                    if revised is None
                    else self._bind_revision(
                        finding, revised.model_copy(update={"id": finding.finding_id})
                    )
                ),
            }
        )
        self._store.record_escalation(run_id, finding, stage, decision)
        if decision.action == "accept_fix":
            # The escalation questioned the evidence for a fix, not the fix, and
            # the adjudicator went and established that evidence itself. Settling
            # it here is the same integration the re-reviewer would have driven;
            # sending it back for another round would re-fix working code, and at
            # the final round would block it outright.
            attempt = self._store.latest_fix_attempt(finding.finding_key)
            # A conflict is the one trigger a second acceptance can honestly
            # clear, because the lane head moves under it when another finding
            # lands; the conflict count below is its bound. Every other trigger
            # is a fact about this fix against this head, and the acceptance
            # just re-integrated and raised it again. Nothing an adjudicator
            # could read has changed, so a further round of it would spend
            # another dispatch to reach the same verdict. The ledger has
            # refuted the claim; stop and let an owner look.
            if (
                finding.escalation_reason is not FindingReason.INTEGRATION_CONFLICT
                and self._store.escalation_action_count(
                    finding.finding_key,
                    finding.round,
                    finding.escalation_reason.value,
                    "accept_fix",
                )
                > 1
            ):
                self._store.set_finding_state(
                    run_id, finding.finding_key, phase=FindingPhase.BLOCKED
                )
                self._settle_predecessors(run_id, finding, FindingPhase.BLOCKED)
                return
            if attempt is None or attempt.status != "fixed" or attempt.commit_sha is None:
                self._store.set_finding_state(
                    run_id, finding.finding_key, phase=FindingPhase.BLOCKED
                )
                self._settle_predecessors(run_id, finding, FindingPhase.BLOCKED)
                return
            await self._integrate_fix(run_id, finding, attempt, readjudicated=True)
        elif decision.action in {"approve_unchanged", "approve_scope_revision"}:
            limit = self._config.review_cycle.max_fix_rounds_per_finding
            # A conflict is a fact about the lane head moving, not about the fix,
            # which re-review already approved. Re-landing it is a rebase, so it
            # retries the same round: the ceiling is there to stop a fixer
            # thrashing on one defect, and that is not what happened. Its own
            # bound is how many times the same finding has conflicted.
            if finding.escalation_reason is FindingReason.INTEGRATION_CONFLICT:
                # Count the conflicts from the escalation ledger, not the
                # integration ledger: the latter is unique on (finding, round), so
                # repeated conflicts inside one round collapse into one row and
                # this bound would never be reached.
                conflicts = self._store.integration_conflicts(finding.finding_key)
                retry_round = finding.round if conflicts < limit else finding.round + 1
            else:
                retry_round = finding.round + 1
            if retry_round > limit and self._first_adjudicated_retry(finding, limit):
                # An adjudicator that inspected the head itself and asked for one
                # more attempt is supervising, not watching a fixer thrash on one
                # defect, so it gets a retry at the ceiling round rather than
                # having verified work thrown away under it. One grant per
                # finding: a second would make the ceiling mean nothing.
                retry_round = finding.round
            if retry_round > limit:
                self._store.set_finding_state(
                    run_id, finding.finding_key, phase=FindingPhase.BLOCKED
                )
            else:
                if finding.escalation_reason is FindingReason.INTEGRATION_CONFLICT:
                    self._rebase_conflicted_fix(run_id, finding)
                self._store.set_finding_state(
                    run_id,
                    finding.finding_key,
                    phase=FindingPhase.PENDING_FIX,
                    round=retry_round,
                    effective_contract=decision.revised_finding or finding.effective_contract,
                )
        elif decision.action == "defer":
            self._store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.DEFERRED)
            self._settle_predecessors(run_id, finding, FindingPhase.DEFERRED)
        else:
            self._store.set_finding_state(run_id, finding.finding_key, phase=FindingPhase.BLOCKED)
            self._settle_predecessors(run_id, finding, FindingPhase.BLOCKED)

    def _first_adjudicated_retry(self, finding: FindingRecord, limit: int) -> bool:
        """Say whether this finding still holds its one supervised retry at the ceiling.

        Spend the grant against the attempts the ledger accepted, not against the
        decisions that asked for them and not against the stages that ran. An
        adjudicator repeating itself verbatim records no second verdict, so
        counting verdicts would hand out the same retry forever; and a stage that
        only re-landed an existing commit after a conflict did no new fix work,
        so counting stages would spend the grant on a rebase.
        """

        if finding.round != limit:
            return False
        return self._store.fix_attempt_count(finding.finding_key, limit) == 1

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
        """Verify the exact fixer head and enforce portable literal path boundaries.

        Every question here is one Git answers about the lane checkout, so none of
        it depends on the fixer having transcribed a sha correctly: `_bound_fix_attempt`
        has already restated the attempt on the assigned base and the observed head.
        What still has to hold is that the head descends from that base by exactly
        one commit and touches nothing outside the finding's scope.
        """

        if stage.worktree_id is None or attempt.commit_sha is None:
            raise ValueError("fixed attempt omitted its isolated worktree or commit")
        if not await self._git.is_clean(stage.worktree_id):
            raise ValueError("fixer worktree contains uncommitted changes")
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
        # Scope is a property of the commit, so judge the paths Git reports. The
        # fixer's own list is still recorded, because a fixer that cannot say what
        # it touched is worth seeing in the audit - but it is not the boundary.
        accepted = all(
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
        readjudicated: bool = False,
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
        identity = FixAttemptIdentity(
            fixer_commit_sha=attempt.commit_sha,
            base_sha=lane.integration_head_sha,
            source_commits=source_commits,
            source_finding_ids=source_finding_ids,
        )
        receipt = self._store.integration(finding.finding_key, finding.round)
        if receipt is None:
            receipt = self._store.begin_integration(
                run_id,
                finding,
                fixer_commit_sha=identity.fixer_commit_sha,
                source_commits=identity.source_commits,
                source_finding_ids=identity.source_finding_ids,
                base_sha=identity.base_sha,
            )
        elif (
            receipt.fixer_commit_sha != attempt.commit_sha
            or receipt.source_commits != source_commits
            or receipt.source_finding_ids != source_finding_ids
        ):
            # A conflict retry stays in its own round, and this ledger is unique
            # on (finding, round), so the retry's commit arrives at a slot still
            # describing the attempt it replaces. That is the retry working, not
            # sources changing under a live integration, so re-point the slot at
            # it. Only a settled non-terminal verdict is re-pointed; a `starting`
            # receipt really is mid-flight and still refuses.
            rebound = self._store.reopen_integration(run_id, finding, attempt=identity)
            if rebound is None:
                raise ValueError("integration sources changed after persistence")
            receipt = rebound
        if receipt.status == "integrated":
            self._settle_source_findings(
                run_id,
                finding.lane_id,
                receipt.source_finding_ids,
                FindingPhase.RESOLVED,
            )
            return
        if receipt.status in {"conflict", "validation_failed"}:
            # An adjudicator who has read the commit and accepted it has overruled
            # this verdict, so re-check the fix instead of replaying the stored
            # outcome. Replaying it made accept_fix a no-op: this returned before
            # reaching Git, so a repaired validation contract or a lane head that
            # has since moved was never exercised, and the finding escalated on the
            # same frozen output until it blocked. Only an integrated receipt above
            # is terminal; these two are facts about one attempt.
            if readjudicated:
                receipt = self._store.reopen_integration(run_id, finding) or receipt
            if receipt.status != "starting":
                self._escalate(
                    run_id,
                    finding,
                    FindingReason.INTEGRATION_CONFLICT
                    if receipt.status == "conflict"
                    else FindingReason.VALIDATION_FAILED,
                )
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
        if lane.base_sha != revision.base_sha:
            raise ValueError("worker review base does not match the frozen lane base")
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
        source_base = self._fix_base_sha(finding)
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
            source_base = self._fix_base_sha(predecessor)
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

        source_head = self._fix_base_sha(finding)
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
            source_head = self._fix_base_sha(predecessor)

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
        """The commit this finding's fix is built on, which is not always the review.

        The review revision is where every fix starts and where almost all of them
        stay. It moves only after an integration conflict, which is a fact about
        the lane head having moved rather than about the fix, and which no amount
        of re-fixing on the old base can clear.
        """

        if finding.dispatch_base_sha is not None:
            return finding.dispatch_base_sha
        revision = finding.effective_contract.review_revision
        if revision is None:
            raise ValueError(f"finding {finding.finding_id} was persisted without a revision")
        return revision.head_sha

    def _rebase_conflicted_fix(self, run_id: str, finding: FindingRecord) -> None:
        """Point the next attempt at the head it has to land on, when that is safe.

        A conflict retry rebuilt on the base that just conflicted produces a
        commit that conflicts identically, so the loop spends every remaining
        round re-deriving the same collision and blocks a fix that was correct.
        `finding-escape-drops-search-drilldown-with-job-sheet` did exactly that
        three times on `0706fc6c` while the lane head sat at `f1c5dbd8`; the fix
        itself cherry-picked cleanly by hand. No adjudicator can rebase a commit,
        and the supervisor knows both shas, so it does it here instead of asking.

        A fix with a composite predecessor is left alone. Its integration replays
        the whole chain, and a rebase onto the lane head would silently drop the
        predecessor's commit from the range - a wrong landing is worse than a
        blocked one.
        """

        lane = next(item for item in self._store.lanes(run_id) if item.lane_id == finding.lane_id)
        head = lane.integration_head_sha
        if head is None or head == self._fix_base_sha(finding):
            return
        if self._composite_predecessor(run_id, finding) is not None:
            return
        self._store.advance_fix_base(run_id, finding.finding_key, head)

    def _composite_predecessor(self, run_id: str, finding: FindingRecord) -> FindingRecord | None:
        """The finding whose fix commit this one was built on, if it was built on one."""

        base = self._fix_base_sha(finding)
        return next(
            (
                item
                for item in self._store.findings(run_id, finding.lane_id)
                if item.finding_key != finding.finding_key
                and (prior := self._store.latest_fix_attempt(item.finding_key)) is not None
                and prior.commit_sha == base
            ),
            None,
        )

    def _ensure_dynamic_stages(self, run_id: str) -> None:
        stages = self._store.stages(run_id)
        # Every round can be re-run: a fix rebased after a conflict, a re-review
        # of that rebase, an adjudication of a fix that conflicted again. Each
        # such stage names work already settled under its own key, and
        # `ensure_stage` keys off `stage_key`, so a repeat needs a fresh one or
        # the graph silently never dispatches it. Count the settled keys of the
        # same shape and let the base key carry that ordinal.
        settled = {stage.stage_key for stage in stages if stage.phase in _SETTLED_STAGES}
        by_lane: dict[str, list[StageRecord]] = {}
        for stage in stages:
            by_lane.setdefault(stage.lane_id, []).append(stage)
        for lane in self._store.lanes(run_id):
            lane_stages = by_lane.get(lane.lane_id, [])
            # A blocked worker is processed but produced no changeset, so review
            # only what the lane actually recorded a worker result for.
            worker_done = lane.review_head_sha is not None and any(
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
                    base = (
                        f"{lane.lane_id}:{finding.finding_id}:fix:"
                        f"{finding.round}:{attempt_kind.value}"
                    )
                    self._store.ensure_stage(
                        run_id,
                        lane.lane_id,
                        stage_key=_next_key(settled, base),
                        role=StageKind.FIXER,
                        finding_key=finding.finding_key,
                        finding_id=finding.finding_id,
                        round=finding.round,
                        attempt_kind=attempt_kind,
                    )
                elif finding.phase is FindingPhase.PENDING_RE_REVIEW:
                    base = f"{lane.lane_id}:{finding.finding_id}:re-review:{finding.round}"
                    self._store.ensure_stage(
                        run_id,
                        lane.lane_id,
                        stage_key=_next_key(settled, base),
                        role=StageKind.RE_REVIEWER,
                        finding_key=finding.finding_key,
                        finding_id=finding.finding_id,
                        round=finding.round,
                    )
                elif finding.phase is FindingPhase.PENDING_ESCALATION:
                    reason = finding.escalation_reason or FindingReason.AMBIGUOUS_RESULT
                    base = (
                        f"{lane.lane_id}:{finding.finding_id}:escalate:"
                        f"{finding.round}:{reason.value}"
                    )
                    stage_key = _next_key(settled, base)
                    # A repeat under this base is the same finding, at the same
                    # round, on the same trigger: the adjudication that just ran
                    # left the graph exactly where it found it. The retry suffix
                    # makes every such repeat a fresh dispatchable key, and
                    # nothing else bounds it, so an adjudicator that cannot
                    # settle this finding re-runs for as long as anyone monitors
                    # the run. Spend the same ceiling the fix rounds use, then
                    # hand the finding to the owner.
                    if (
                        _retry_ordinal(stage_key)
                        > self._config.review_cycle.max_fix_rounds_per_finding
                    ):
                        self._store.set_finding_state(
                            run_id, finding.finding_key, phase=FindingPhase.BLOCKED
                        )
                        self._settle_predecessors(run_id, finding, FindingPhase.BLOCKED)
                        continue
                    self._store.ensure_stage(
                        run_id,
                        lane.lane_id,
                        stage_key=stage_key,
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
            try:
                spec = await self._stage_spec(proposals[lane.name], lane, stage, run_id)
            except (OrcaError, GitError, ValueError, KeyError) as exc:
                # A stage whose frozen input cannot be rendered is refused on
                # its own. Leave it unbound so the next tick can retry after
                # the worktree or persisted result becomes readable again.
                self._store.record_stage_start_failure(run_id, stage, str(exc), released=True)
                continue
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

    async def _holds_unreported_work(self, stage: StageRecord) -> bool:
        """Whether this stage's worktree carries commits the stage never reported.

        `release_dead_dispatch` treats "never reported a result" as "never got
        anywhere", and for a worker that crashed on the way in that is right.
        It is wrong for a worker that did the whole job and died on the way out.
        `application-lifecycle-kas-580` is the second case: the stage was
        released, a fresh agent was dispatched into a worktree already holding
        the finished work, it found nothing to do and returned no result, and
        the lane failed - three hours after the pull request that stage produced
        had already been merged. Releasing recovered nothing there and cost the
        run.

        The worktree is the only witness, and the question has to be asked
        against the head this dispatch started from, not against the lane base.
        A lane worktree accumulates commits across its stages, so a later stage
        that genuinely did nothing still sits well ahead of the base, and
        measuring from there would hold every dispatch in a lane that had ever
        produced a commit.

        Fails toward releasing, in all three ways it can fail. A stage with no
        recorded baseline was adopted rather than launched, so there is no
        before to compare against. A worktree that cannot be read has usually
        been removed, and its work is gone with it. Either way re-dispatching is
        the right answer and the stage keeps today's recovery behaviour. Only a
        stage that knows where it started, can still read where it is, and finds
        the two different holds its dispatch.
        """

        if stage.worktree_id is None or stage.start_head_sha is None:
            return False
        try:
            head = await self._git.head(stage.worktree_id)
        except GitError:
            return False
        return head != stage.start_head_sha

    async def _enforce_stage_budgets(self, run_id: str) -> list[OverdueStage]:
        """Answer "has this stage been running longer than its budget" from the clock.

        The only liveness signal before this was Orca handing a Task back to
        READY when its worker terminal disappeared, which catches a *dead*
        agent. A wedged one keeps its terminal, so the Task never comes back and
        the lane waits forever - `application-lifecycle-kas-580` sat dispatched
        for two hours with a child process at zero CPU, and the thing that
        noticed was a human reading `ps`. That answer is arithmetic on a
        timestamp already in the database.

        Soft says so and changes nothing. Hard releases the worker terminal, and
        that is the whole mechanism: Orca returns the Task to READY, the
        existing dead-dispatch path clears the Dispatch id, and the stage is
        dispatched again next tick. Nothing here records a result, because the
        stage produced none.
        """

        budgets = self._config.stage_budgets
        clocks = self._store.stage_clocks(run_id)
        lanes = {lane.lane_id: lane.name for lane in self._store.lanes(run_id)}
        now = datetime.now(UTC)
        overdue: list[OverdueStage] = []
        for stage in self._store.stages(run_id):
            if stage.phase is not StagePhase.DISPATCHED or stage.orca_dispatch_id is None:
                continue
            clock = clocks.get(stage.stage_id)
            if clock is None:
                continue
            budget = budgets.for_role(stage.role.value)
            minutes = int((now - _aware(clock.started_at)).total_seconds() // 60)
            hard = budget.hard_minutes
            if hard is not None and minutes >= hard:
                if clock.timeouts >= budgets.max_timeouts:
                    self._store.block_lane(
                        run_id,
                        stage.lane_id,
                        f"stage {stage.stage_key} exceeded its {hard}-minute budget "
                        f"{clock.timeouts + 1} times",
                    )
                    continue
                await self._orca.release_worker(stage.orca_dispatch_id, stage.orca_terminal_handle)
                self._store.note_stage_timed_out(run_id, stage, minutes)
                overdue.append(
                    OverdueStage(
                        stage_key=stage.stage_key,
                        lane=lanes.get(stage.lane_id, stage.lane_id),
                        role=stage.role,
                        minutes=minutes,
                        budget="hard",
                    )
                )
                continue
            soft = budget.soft_minutes
            if soft is None or minutes < soft:
                continue
            activity = await self._stage_activity(stage.orca_dispatch_id)
            if not clock.warned:
                self._store.note_stage_overdue(run_id, stage, minutes, activity)
            overdue.append(
                OverdueStage(
                    stage_key=stage.stage_key,
                    lane=lanes.get(stage.lane_id, stage.lane_id),
                    role=stage.role,
                    minutes=minutes,
                    budget="soft",
                    activity=activity,
                )
            )
        return overdue

    async def _stage_activity(self, dispatch_id: str) -> str | None:
        """Say what an overdue stage is doing, by comparing its turns to each other.

        A stage that is slow and a stage that is stuck in a poll loop both read
        as "late" from the ledger. The difference is visible without judgement:
        an agent making progress calls different things, and an agent waiting on
        a subprocess by burning a turn every thirty seconds calls the same thing
        over and over. That is a string comparison, so it is done here rather
        than asked of a model.
        """

        try:
            turns = await self._orca.worker_turns(dispatch_id)
        except OrcaError:
            # Unreadable is not the same as idle, and reporting it as idle would
            # be worse than reporting nothing.
            return None
        if not turns:
            return None
        last = turns[-1]
        tool = last.split(":", 1)[0]
        repeated = sum(1 for turn in turns if turn == last)
        if repeated < 3 or repeated * 2 <= len(turns):
            # Deliberately not "n turns": the page size is fixed, so that number
            # moves every tick without the stage changing, and a status line that
            # reprints on noise stops being read.
            return tool
        # The whole point of the reading: the same call, over and over, is a
        # stage spending the budget without moving.
        return f"{tool} repeated {repeated}/{len(turns)} turns unchanged"

    async def _release_settled(self, run_id: str) -> None:
        for stage in self._store.stages(run_id):
            if (
                stage.phase in {StagePhase.COMPLETED, StagePhase.FAILED}
                and stage.orca_dispatch_id is not None
                and not stage.released
            ):
                await self._orca.release_worker(stage.orca_dispatch_id, stage.orca_terminal_handle)
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
            if stage.orca_task_id is None:
                raise ValueError(f"ready stage {stage.stage_id} has no Orca task")
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
            try:
                started.append(await self._launch(run_id, lanes[stage.lane_id], stage))
            except OrcaTimeout as exc:
                # A timed-out start has no outcome. Orca may have created the
                # Dispatch and lost the reply, so handing the slot back here
                # races reconciliation, which is already the thing that looks
                # up a STARTING stage's Dispatch and either adopts it or frees
                # the slot. Leave the stage where it is and let that decide.
                self._store.record_stage_start_failure(run_id, stage, str(exc), released=False)
            except (OrcaError, GitError, ValueError) as exc:
                # A refused start did not happen, so give the slot back and try
                # again next tick. Then keep walking: one stage that cannot
                # start must not park every stage queued behind it, which is
                # what raising out of this loop did.
                self._store.reset_stage_reservation(run_id, stage)
                self._store.record_stage_start_failure(run_id, stage, str(exc), released=True)
        return started

    async def _launch(self, run_id: str, lane: LaneRecord, stage: StageRecord) -> StageLaunch:
        """Start one already-reserved stage, or raise without having started it."""

        if stage.orca_task_id is None:  # already checked before the slot was reserved
            raise ValueError(f"stage {stage.stage_id} has no Orca task")
        placement = self._placement(run_id, lane, stage)
        if stage.role is StageKind.WORKER:
            worker_worktree = placement.worktree_id
            if worker_worktree is None:
                worker_worktree, _ = await self._orca.create_worktree(
                    lane_name=_worker_name(lane.name, stage),
                    repo_selector=lane.repo_selector,
                    base_ref=lane.base_ref,
                )
            await self._prepare_worker_checkout(
                run_id, lane, stage, worker_worktree, before_dispatch=True
            )
            placement = WorkerPlacement(worktree_id=worker_worktree)
        # `use_run` binds the coordinator terminal, and that binding is one piece
        # of shared Orca state: a second supervisor process binding its own run
        # moves it out from under this one, after which `worker-start` resolves
        # the task against the wrong run and fails. Name the run on the call so
        # the start does not depend on who bound the terminal last.
        orca_run_id = self._store.run(run_id).orca_run_id
        if orca_run_id is None:
            raise ValueError(f"run {run_id} has no Orca run to start stages against")
        dispatch_id, worktree_id, terminal_handle, payload = await self._orca.start_worker(
            task_id=stage.orca_task_id,
            lane_name=_worker_name(lane.name, stage),
            repo_selector=lane.repo_selector,
            worktree_id=placement.worktree_id,
            base_ref=placement.base_ref,
            parent_worktree_id=placement.parent_worktree_id,
            profile=self._profile(stage),
            orca_run_id=orca_run_id,
        )
        # Read before the agent has had a chance to commit. A stage that later
        # dies without reporting is asked whether it moved this, which is the
        # only way to tell a worker that crashed on the way in from one that
        # finished and crashed on the way out.
        try:
            start_head_sha = await self._git.head(worktree_id)
        except GitError:
            start_head_sha = None
        self._store.mark_stage_started(
            run_id,
            stage.stage_id,
            dispatch_id,
            worktree_id,
            payload,
            terminal_handle,
            start_head_sha,
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
        return StageLaunch(
            lane=lane.name,
            role=stage.role,
            task_id=stage.orca_task_id,
            dispatch_id=dispatch_id,
            worktree_id=worktree_id,
        )

    async def _prepare_worker_checkout(
        self,
        run_id: str,
        lane: LaneRecord,
        stage: StageRecord,
        worktree_id: str,
        *,
        before_dispatch: bool = False,
    ) -> None:
        """Persist a worker checkout and freeze its base before dispatch."""

        if stage.role is not StageKind.WORKER:
            return
        self._store.record_worker_checkout(run_id, stage.stage_id, worktree_id)
        if lane.base_sha is not None:
            return
        if before_dispatch:
            # Worktree creation already resolved the requested ref. HEAD is that
            # exact result and cannot move with the shared branch ref.
            base_sha = await self._git.resolve_ref(worktree_id, "HEAD")
        else:
            base_sha = stage.start_head_sha or await self._git.resolve_ref(
                worktree_id, lane.base_ref
            )
        self._store.record_worker_checkout(run_id, stage.stage_id, worktree_id, base_sha)

    def _placement(self, run_id: str, lane: LaneRecord, stage: StageRecord) -> WorkerPlacement:
        """Resolve the exact existing or isolated checkout for one role."""

        if stage.role is StageKind.WORKER:
            # A worker stage that already recorded a checkout is being started a
            # second time, and that checkout holds whatever it committed before
            # it stopped. Handing it a fresh one from the base ref abandons that
            # commit, so a lane that blocked on a decision would pay for the
            # whole implementation again to answer it.
            return WorkerPlacement(worktree_id=stage.worktree_id, base_ref=lane.base_ref)
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
        if stage.role is StageKind.ESCALATION:
            # An escalation is asked whether a fixer commit answers its finding.
            # Fixers commit in isolated child checkouts and only reach the lane
            # checkout once integration runs, so reading the lane worktree shows
            # the adjudicator the code as it stood *before* the fix. Every verdict
            # it gave was about the wrong tree; approve_unchanged in particular
            # was unanimous because the fix was genuinely absent from what it read.
            # A rejected fixer is excluded on purpose. Its checkout holds no
            # commit worth adjudicating, and pointing the escalation at one is
            # how a finding whose fixer never committed became permanently
            # unstartable once that checkout was reclaimed.
            fixed = [
                item
                for item in self._store.stages(run_id)
                if item.lane_id == stage.lane_id
                and item.finding_id == stage.finding_id
                and item.role is StageKind.FIXER
                and item.processed
                and item.phase is not StagePhase.FAILED
                and item.worktree_id is not None
            ]
            if fixed:
                return WorkerPlacement(worktree_id=fixed[-1].worktree_id)
            # Nothing was ever committed for this finding - a scope escape caught
            # before the first commit, say - so the lane checkout is the subject.
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
            # Only a failure nothing has answered condemns the lane. A rejected
            # stage is marked processed by the rejection that routed it onward,
            # and its finding then runs another round; counting that stage
            # forever left a lane permanently failed while its own findings were
            # resolving, which is a status no later event could clear.
            if lane.phase is LanePhase.FAILED or any(
                stage.phase is StagePhase.FAILED and not stage.processed for stage in lane_stages
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
                passed_head = next(
                    (item.head_sha for item in reversed(ci) if item.status == "passed"), None
                )
                published = publications[-1] if publications else None
                # A landed head has already passed whatever the base branch
                # required of it, and its pull request cannot be taken out of
                # draft after a merge. Holding a merged lane open waiting for
                # those two facts waits for something that can never arrive.
                gated = (
                    published is not None
                    and published.head_sha == lane.integration_head_sha
                    and (
                        published.landed
                        or (
                            not self._config.publication.merge
                            and published.head_sha == passed_head
                            and not published.draft
                        )
                    )
                )
                phase = LanePhase.COMPLETE if reviewed and settled and gated else LanePhase.ACTIVE
            self._store.set_lane_phase(run_id, lane.lane_id, phase)
            lane_phases.append(phase)
        if any(phase is LanePhase.ACTIVE for phase in lane_phases):
            # One lane blocking has no bearing on a lane that is still working,
            # and calling the whole run terminal because of it stops the ones
            # that were fine. `assistant-kas-576` blocked on a CI query it made
            # one second too early and took `ui-kas-564` and
            # `application-lifecycle-kas-580` down with it, neither of which had
            # anything to do with that pull request. The run is terminal when
            # nothing is left that could still move; until then the per-lane
            # phases carry the block, which is where it belongs.
            return "active"
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
            if lane.phase in {LanePhase.BLOCKED, LanePhase.FAILED, LanePhase.COMPLETE}:
                # A complete lane is finished: its head is published, its pull
                # request is out of draft and its required checks passed for that
                # exact head. Coming back to it every tick re-edits the pull
                # request body and re-queries checks for no decision, and it hands
                # the lane a way to fail after it has already succeeded - an owner
                # who merges the pull request turns the next pass into
                # "the authorized lane pull request is no longer open", which
                # blocks the lane and takes the whole run terminal. Merging is the
                # outcome this lane was working toward, so stop here.
                continue
            if not self._local_lane_settled(run_id, lane):
                continue
            unintegrated = self._unintegrated_resolved(run_id, lane)
            if unintegrated:
                # Every automatic path to RESOLVED goes through an integrated
                # receipt, so reaching here means something outside the graph put
                # the phase there - `settle --phase resolved` is the one that can.
                # Publishing now pushes the lane head as it stood before the fix
                # and calls it fixed, which is the one outcome the whole review
                # loop exists to prevent.
                self._store.block_lane(
                    run_id,
                    lane.lane_id,
                    "lane is settled but "
                    + ", ".join(sorted(unintegrated))
                    + " has no integrated fix",
                )
                continue
            if lane.integration_head_sha is None:
                self._store.block_lane(run_id, lane.lane_id, "lane has no integrated head")
                continue
            publications = self._store.publications(run_id, lane.lane_id)
            previous = publications[-1] if publications else None
            # Bound before the attempt so the landed handler can name what was
            # merged even when the merge is discovered while observing checks.
            receipt: PublicationReceipt | None = previous
            try:
                if previous is None or previous.head_sha != lane.integration_head_sha:
                    published = await self._publisher.publish(
                        run_id=run_id,
                        lane=lane,
                        head_sha=lane.integration_head_sha,
                        previous=previous,
                    )
                    if published.run_id != run_id or published.lane != lane.name:
                        raise PublicationError("publisher receipt does not match the accepted lane")
                    if published.head_sha != lane.integration_head_sha:
                        raise PublicationError(
                            "publisher receipt does not match the integrated head"
                        )
                    self._store.record_publication(run_id, lane.lane_id, published)
                    publications.append(published)
                    receipt = published
                else:
                    receipt = previous
                if receipt.landed:
                    # The branch is in the base branch. Nothing is left to push,
                    # edit, or observe: checks for this head belong to the base
                    # branch's history now, and asking for them again is how a
                    # merged lane used to report itself as failed.
                    self._store.note_publication_progress(run_id, lane.lane_id)
                    continue
                ci = await self._publisher.checks(receipt)
                if ci.head_sha != receipt.head_sha:
                    raise PublicationError("CI observation is not pinned to the published head")
                self._store.record_ci_receipt(run_id, lane.lane_id, ci)
                if ci.status == "passed":
                    ready = await self._publisher.mark_ready(receipt)
                    if ready.head_sha != receipt.head_sha or ready.draft:
                        raise PublicationError("provider did not mark the exact pull request ready")
                    self._store.record_publication(run_id, lane.lane_id, ready)
                    if self._config.publication.merge:
                        landed = await self._publisher.land(ready)
                        if (
                            landed.head_sha != ready.head_sha
                            or not landed.landed
                            or landed.merge_sha is None
                        ):
                            raise PublicationError(
                                "provider did not land the exact pull request with a merge commit"
                            )
                        self._store.record_publication(run_id, lane.lane_id, landed)
                elif ci.status == "failed":
                    await self._record_ci_failure(run_id, lane, publications, ci)
                self._store.note_publication_progress(run_id, lane.lane_id)
            except PullRequestLanded:
                # Merged between publishing this head and observing it. The head
                # is the one this lane published, so the merge is this lane
                # succeeding; record it and stop publishing the lane.
                if receipt is not None:
                    self._store.record_publication(
                        run_id,
                        lane.lane_id,
                        receipt.model_copy(update={"draft": False, "landed": True}),
                    )
                self._store.note_publication_progress(run_id, lane.lane_id)
            except IntegrationConflict as exc:
                await self._record_publication_conflict(run_id, lane, str(exc))
                self._store.note_publication_progress(run_id, lane.lane_id)
            except PublicationError as exc:
                # One failed observation is not knowledge. `assistant-kas-576`
                # blocked one second after pushing its branch, on a required-check
                # query GitHub had not caught up with yet, and every check was
                # green by the time anyone looked. Give the remote a few ticks to
                # answer before calling that the lane's failure.
                attempts = self._store.note_publication_error(run_id, lane.lane_id, str(exc))
                if attempts >= _PUBLICATION_ATTEMPTS:
                    self._store.block_lane(
                        run_id,
                        lane.lane_id,
                        f"{exc} (unchanged over {attempts} publication passes)",
                    )

    async def _record_publication_conflict(
        self, run_id: str, lane: LaneRecord, detail: str
    ) -> None:
        """Freeze a lane-scoped finding and route a base-branch conflict to escalation."""

        if lane.worktree_id is None or lane.integration_head_sha is None:
            self._store.block_lane(
                run_id, lane.lane_id, "publication conflict has no integrated lane checkout"
            )
            return
        worker = self._store.worker_result(lane.lane_id)
        base_sha = worker.review_revision.base_sha
        head_sha = lane.integration_head_sha
        try:
            paths = await self._git.changed_paths(lane.worktree_id, base_sha, head_sha)
        except GitError as exc:
            self._store.block_lane(
                run_id, lane.lane_id, f"publication conflict scope unavailable: {exc}"
            )
            return
        if not paths:
            paths = worker.changed_paths
        if not paths:
            self._store.block_lane(
                run_id, lane.lane_id, "publication conflict has no deterministic lane scope"
            )
            return
        finding_id = f"publication-conflict-{head_sha[:12]}"
        try:
            diff_sha256 = await self._git.diff_sha256(lane.worktree_id, base_sha, head_sha)
        except GitError as exc:
            self._store.block_lane(
                run_id, lane.lane_id, f"publication conflict scope unavailable: {exc}"
            )
            return
        review_revision = ReviewRevision(
            base_sha=base_sha, head_sha=head_sha, diff_sha256=diff_sha256
        )
        contract = ReviewFinding(
            id=finding_id,
            review_revision=review_revision,
            evidence=[
                FindingEvidence(
                    location=FindingLocation(path=path, start_line=1, end_line=1),
                    claim=detail[:4_000],
                )
                for path in paths[:64]
            ],
            failure_mode=detail[:4_000],
            required_outcome=(
                f"Integrate the lane with current {lane.base_ref} without dropping accepted work."
            ),
            allowed_write_scope=AllowedWriteScope(paths=paths[:128]),
        )
        finding = self._store.add_finding(
            run_id, lane.lane_id, contract, origin="publication_conflict"
        )
        self._escalate(run_id, finding, FindingReason.INTEGRATION_CONFLICT)

    def _unintegrated_resolved(self, run_id: str, lane: LaneRecord) -> list[str]:
        """Resolved findings in one lane whose fix never reached the lane checkout."""

        integrated = self._store.integrated_finding_ids(run_id, lane.lane_id)
        return [
            item.finding_id
            for item in self._store.findings(run_id, lane.lane_id)
            if item.phase is FindingPhase.RESOLVED and item.finding_id not in integrated
        ]

    def _local_lane_settled(self, run_id: str, lane: LaneRecord) -> bool:
        stages = [item for item in self._store.stages(run_id) if item.lane_id == lane.lane_id]
        reviewed = any(
            item.role is StageKind.INITIAL_REVIEWER and item.processed for item in stages
        )
        findings = self._store.findings(run_id, lane.lane_id)
        return reviewed and all(
            item.phase in {FindingPhase.RESOLVED, FindingPhase.DEFERRED} for item in findings
        )

    def reauthorize(self, run_id: str, note: str, *, apply: bool = True) -> PolicyReauthorization:
        """Read a mid-run policy change field by field, and freeze it once the owner sees it.

        The authorization digests the proposal and the config together, so
        editing `orkastrator.yaml` while a run is in flight fails every tick
        afterwards. Recording a new proposal is the honest answer when the
        *plan* changed. When only the policy changed, and the owner meant it,
        this is the answer: same lanes, same findings, same worktrees, one
        audited note saying which policy the rest of the run ran under.

        `apply=False` reads the change without making it, which is the whole
        point of the split: an owner authorizing a policy change should be
        judging `final_gate.advisory_checks: [] -> [...]`, not two digests. A
        note typed before seeing the diff is a claim about the change rather
        than a reading of it.
        """

        run = self._store.run(run_id)
        current = self._config.model_dump(mode="json")
        authorization = AcceptanceAuthorization(
            run_id=run_id,
            proposal_sha256=_sha256_json(run.proposal.model_dump(mode="json")),
            config_sha256=_sha256_json(current),
        )
        frozen = self._store.acceptance_authorization(run_id)
        if frozen is None:
            raise KeyError(f"run {run_id} has no frozen acceptance authorization")
        if (
            self._store.accepted_config(run_id) is None
            and frozen.config_sha256 == authorization.config_sha256
        ):
            # The digest already proves the config on disk is the one this run
            # was accepted under, so it can be recorded without authorizing
            # anything. That is how a run started before the payload existed
            # becomes readable: the first time it is looked at while unchanged.
            self._store.record_acceptance_authorization(run_id, frozen, config=current)
        accepted = self._store.accepted_config(run_id)
        changes = [] if accepted is None else config_changes(accepted, current)
        result = PolicyReauthorization(
            authorization=authorization,
            changes=changes,
            comparable=accepted is not None,
            applied=apply,
        )
        if apply:
            self._store.reauthorize_acceptance(
                run_id, authorization, note, config=current, changes=changes
            )
        return result

    def resume(self, run_id: str, lane_name: str | None, note: str) -> list[LaneRecord]:
        """Clear a lane block the owner has looked at, and let the run advance again.

        `reopen` and `settle` both act on findings. A lane that blocked after
        every one of its findings had settled - on a CI query, on a pull request
        somebody merged - is reachable by neither, which is why recovering one
        has meant hand-written UPDATE statements against `lanes` and
        `supervisor_runs`. Those two commands exist to make that unnecessary;
        this is the third case they missed.

        The run's recorded status is cleared with the block. Leaving it terminal
        would be half a recovery: the next tick derives the real status anyway,
        and a stale `blocked` on the row is exactly what makes an owner think
        the run is still stopped.
        """

        lanes = [
            lane
            for lane in self._store.lanes(run_id)
            if lane.phase is LanePhase.BLOCKED and lane_name in {None, lane.name}
        ]
        if not lanes:
            named = f"lane {lane_name} of " if lane_name else ""
            raise ValueError(f"{named}run {run_id} has no blocked lane to resume")
        for lane in lanes:
            self._store.resume_lane(run_id, lane.lane_id, note)
        return [
            lane for lane in self._store.lanes(run_id) if lane.lane_id in {x.lane_id for x in lanes}
        ]

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

    async def _stage_spec(
        self,
        lane: LaneProposal,
        lane_record: LaneRecord,
        stage: StageRecord,
        run_id: str,
    ) -> str:
        context = (
            f"Lane: {lane.name}\nIssue: {lane.issue_id}\nObjective:\n{lane.prompt}\n\n"
            f"Stop condition:\n{lane.stop_condition}"
        )
        if stage.role is StageKind.WORKER:
            schema = json.dumps(WorkerResult.model_json_schema(), sort_keys=True)
            blocked = json.dumps(WorkerBlocked.model_json_schema(), sort_keys=True)
            return (
                "Implement the scoped issue, verify it, and commit the result. Compute "
                "diff_sha256 over the raw output of `git diff --binary --full-index "
                "<base>..<head> --` and report the exact sorted changed paths. Send worker_done "
                "with an explicit outcome and body set to only the changeset JSON matching "
                f"this schema: {schema}\n"
                "If you reach a decision this contract does not answer, do not ask anyone and do "
                "not wait. Commit what you have, then send worker_done with body set to only the "
                f"blocked JSON matching this schema: {blocked}\n\n{context}"
            )
        if stage.role is StageKind.INITIAL_REVIEWER:
            schema = json.dumps(InitialReviewReport.model_json_schema(), sort_keys=True)
            prefix = (
                "Review the exact worker changeset once, read-only. Freeze every actionable "
                "finding. Omit review_revision everywhere: the supervisor binds it and verifies "
                "your checkout itself. Every validation command you write is executed directly "
                "without a shell, so name one executable per requirement: no `cd`, `&&`, pipes or "
                "redirection. Split a chain into one requirement each and set that requirement's "
                "workdir instead of `cd`. This review is rejected outright if any command uses "
                "that syntax. A check is satisfied by its exit status alone, so set expect_exit "
                "when the passing outcome is a non-zero one, as an absence check with rg is. "
                "Name pytest scope as usual but omit baseline_result: the supervisor pins the "
                "project environment, disables repository-wide coverage for scoped pytest runs, "
                "and records the base observation before freezing the finding. "
                "Send worker_done with body set to only the JSON "
                f"contract matching this schema: {schema}"
            )
            return await self._stage_spec_with_frozen_diff(
                prefix, context, lane_record, stage, run_id
            )
        finding = self._finding_for_stage(run_id, stage)
        contract = finding.effective_contract.model_dump_json()
        if stage.role is StageKind.FIXER:
            schema = json.dumps(FixAttempt.model_json_schema(), sort_keys=True)
            prior = self._store.fix_attempt(finding.finding_key, finding.round - 1)
            prefix = (
                f"Fix only this frozen finding at round {finding.round}: {contract}\n"
                f"Prior-round evidence: {prior.model_dump_json() if prior else 'none'}\n"
                "Start from the assigned frozen review head and produce exactly one commit that "
                "fully resolves the finding. Do not widen scope. Run exactly the validation "
                "commands this finding names and report each one; they are the whole obligation, "
                "so do not re-run the lane's wider suite. finding_id, round, base_sha and "
                "commit_sha are read from the record and from Git, so send any placeholder there "
                "and spend your effort on status, changed_paths and validation_results. Send "
                f"worker_done with body set to only the JSON matching this schema: {schema}"
            )
            return await self._stage_spec_with_frozen_diff(
                prefix, context, lane_record, stage, run_id
            )
        if stage.role is StageKind.RE_REVIEWER:
            attempt = self._store.latest_fix_attempt(finding.finding_key)
            schema = json.dumps(ReReviewResult.model_json_schema(), sort_keys=True)
            prefix = (
                f"Re-review only this finding at round {finding.round}: {contract}\n"
                f"Fixer evidence: {attempt.model_dump_json() if attempt else 'missing'}\n"
                "Unrelated findings must be returned with origin unrelated so they are deferred. "
                "finding_id, round, reviewed_commit_sha and every review_revision are read from "
                "the record and from Git, so send any placeholder there and spend your effort on "
                "the verdict. Send worker_done with body set to only the JSON matching this "
                f"schema: {schema}"
            )
            return await self._stage_spec_with_frozen_diff(
                prefix, context, lane_record, stage, run_id
            )
        schema = json.dumps(EscalationDecision.model_json_schema(), sort_keys=True)
        return (
            f"Adjudicate this finding without silently widening it: {contract}\n"
            f"Trigger: {finding.escalation_reason}. Choose accept_fix when you established "
            "yourself that the committed fix resolves the finding and only its evidence was "
            "missing, approve_unchanged when the finding stands as frozen and wants another "
            "attempt, approve_scope_revision only when the scope itself must change, and defer "
            "when the finding belongs to another run or its own premise is invalid: a deferred "
            "finding is dismissed and its lane lands without it. Choose block only when the lane "
            "must not land at all until an owner has looked, because every other finding in the "
            "lane stops with it. finding_id, round, reason and every "
            "review_revision are read from the record, so send any placeholder there. Send "
            f"worker_done with body set to only the JSON matching this schema: {schema}\n\n"
            f"{context}"
        )

    async def _stage_spec_with_frozen_diff(
        self,
        prefix: str,
        context: str,
        lane: LaneRecord,
        stage: StageRecord,
        run_id: str,
    ) -> str:
        """Build a frozen-diff stage spec within the subprocess argv limit."""

        empty_diff_spec = f"{prefix}\n\n\n\n{context}"
        diff_bytes = MAX_TASK_SPEC_BYTES - len(empty_diff_spec.encode())
        if diff_bytes < 1:
            raise ValueError(f"stage {stage.stage_id} has no room for frozen diff input")
        frozen_diff = await self._frozen_diff_input(lane, stage, run_id, max_spec_bytes=diff_bytes)
        spec = f"{prefix}\n\n{frozen_diff}\n\n{context}"
        if len(spec.encode()) > MAX_TASK_SPEC_BYTES:
            raise ValueError(f"stage {stage.stage_id} frozen diff spec exceeds delivery limit")
        return spec

    async def _frozen_diff_input(
        self,
        lane: LaneRecord,
        stage: StageRecord,
        run_id: str,
        *,
        max_spec_bytes: int,
    ) -> str:
        """Render the exact scoped diff once, then package all of it under the byte budget."""

        if lane.worktree_id is None:
            raise ValueError(f"stage {stage.stage_id} has no lane worktree for its frozen diff")
        if stage.role is StageKind.INITIAL_REVIEWER:
            worker = self._store.worker_result(lane.lane_id)
            revision = worker.review_revision
            paths = worker.changed_paths
        else:
            finding = self._finding_for_stage(run_id, stage)
            paths = finding.effective_contract.allowed_write_scope.paths
            if stage.role is StageKind.FIXER:
                frozen = finding.effective_contract.review_revision
                if frozen is None:
                    raise ValueError(f"finding {finding.finding_id} has no frozen revision")
                revision = frozen
            elif stage.role is StageKind.RE_REVIEWER:
                attempt = self._store.latest_fix_attempt(finding.finding_key)
                if attempt is None or attempt.commit_sha is None:
                    raise ValueError("re-review has no committed fixer diff to render")
                revision = ReviewRevision(
                    base_sha=self._fix_base_sha(finding),
                    head_sha=attempt.commit_sha,
                    diff_sha256=await self._git.diff_sha256(
                        lane.worktree_id, self._fix_base_sha(finding), attempt.commit_sha
                    ),
                )
            else:
                raise ValueError(f"stage {stage.stage_id} does not consume a frozen diff")

        rendered = await self._git.render_diff(
            lane.worktree_id, revision.base_sha, revision.head_sha, paths
        )
        file_index = await self._git.changed_paths(
            lane.worktree_id, revision.base_sha, revision.head_sha, paths
        )
        return _format_frozen_diff(
            revision=revision,
            rendered=rendered,
            paths=file_index,
            budget_bytes=self._config.frozen_diff_budget_bytes,
            max_spec_bytes=max_spec_bytes,
        )


def _format_frozen_diff(
    *,
    revision: ReviewRevision,
    rendered: bytes,
    paths: list[str],
    budget_bytes: int,
    max_spec_bytes: int,
) -> str:
    """Package diff content without exceeding the task's argv delivery limit."""

    size = len(rendered)
    index = "\n".join(f"- {path}" for path in paths) or "(no changed files in scope)"
    identity = (
        "Frozen diff input (supervisor-rendered; do not re-derive it):\n"
        f"base_sha: {revision.base_sha}\n"
        f"head_sha: {revision.head_sha}\n"
        f"diff_sha256: {revision.diff_sha256}\n"
        f"rendered_bytes: {size}\n"
        f"chunk_budget_bytes: {budget_bytes}\n"
        f"Complete file index ({len(paths)} files):\n{index}"
    )

    # A rendering that is not valid UTF-8 is delivered as base64 rather than
    # decoded, so the bytes an agent reads still hash to the advertised
    # diff_sha256 instead of carrying substituted replacement characters.
    try:
        text: str | None = rendered.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    if text is not None:
        complete = f"{identity}\nFrozen diff (complete):\n<frozen_diff>\n{text}</frozen_diff>"
        if size <= budget_bytes and len(complete.encode()) <= max_spec_bytes:
            return complete
        prefix = (
            f"{identity}\nFrozen diff is over budget or exceeds the task delivery limit. "
            "Included content consists only of complete, deterministic file-boundary chunks; "
            "no content is truncated.\n"
        )
    else:
        detail_lines = "\n".join(
            f"- {path}: invalid UTF-8 bytes {bytes_hex}"
            for path, bytes_hex in _non_utf8_file_details(rendered, paths)
        )
        prefix = (
            f"{identity}\nFrozen diff contains bytes that are not valid UTF-8. No bytes were "
            "substituted; the content follows as base64 over complete file-boundary chunks, "
            f"and each affected file is listed below:\n{detail_lines}\n"
        )

    chunks = _chunk_diff_by_file(rendered, min(budget_bytes, max_spec_bytes))
    included: list[str] = []
    for number, chunk in enumerate(chunks, start=1):
        packaged = _package_diff_chunk(number, len(chunks), chunk, as_text=text is not None)
        candidate = prefix + "\n".join([*included, packaged])
        if len(candidate.encode()) > max_spec_bytes:
            break
        included.append(packaged)

    omission = _omitted_diff_message(paths[_records_in(chunks, len(included)) :])
    while len((prefix + "\n".join(included) + "\n" + omission).encode()) > max_spec_bytes:
        if not included:
            raise ValueError("frozen diff file index exceeds task spec delivery limit")
        included.pop()
        omission = _omitted_diff_message(paths[_records_in(chunks, len(included)) :])
    return prefix + "\n".join(included) + "\n" + omission


def _records_in(chunks: list[bytes], count: int) -> int:
    """Count the complete file records carried by the first ``count`` chunks."""

    return sum(chunk.count(b"diff --git ") for chunk in chunks[:count])


def _package_diff_chunk(number: int, total: int, chunk: bytes, *, as_text: bool) -> str:
    """Render one complete file-boundary chunk for delivery inside a task spec."""

    if as_text:
        return (
            f"Frozen diff chunk {number}/{total} ({len(chunk)} bytes):\n"
            f"<frozen_diff_chunk>\n{chunk.decode('utf-8')}</frozen_diff_chunk>"
        )
    encoded = base64.b64encode(chunk).decode("ascii")
    return (
        f"Frozen diff chunk {number}/{total} ({len(chunk)} raw bytes; base64):\n"
        f'<frozen_diff_chunk encoding="base64">{encoded}</frozen_diff_chunk>'
    )


def _omitted_diff_message(paths: list[str]) -> str:
    """Name every file whose complete diff record was not embedded."""

    if not paths:
        return "No diff content was omitted; all complete file records are included."
    return (
        "Frozen diff content omitted to keep this task spec creatable. The complete file index "
        "above remains authoritative; omitted complete file records are:\n"
        + "\n".join(f"- {path}" for path in paths)
    )


def _chunk_diff_by_file(rendered: bytes, budget_bytes: int) -> list[bytes]:
    """Greedily group complete ``diff --git`` records without losing any bytes."""

    records: list[bytes] = []
    current: list[bytes] = []
    for line in rendered.splitlines(keepends=True):
        if line.startswith(b"diff --git ") and current:
            records.append(b"".join(current))
            current = []
        current.append(line)
    if current:
        records.append(b"".join(current))
    if not records:
        return [b""]

    chunks: list[bytes] = []
    chunk = b""
    for record in records:
        candidate = chunk + record
        if chunk and len(candidate) > budget_bytes:
            chunks.append(chunk)
            chunk = record
        else:
            chunk = candidate
    chunks.append(chunk)
    return chunks


def _non_utf8_file_details(raw: bytes, paths: list[str]) -> list[tuple[str, str]]:
    """Report invalid UTF-8 byte sequences against their diff file records."""

    details: list[tuple[str, str]] = []
    for index, record in enumerate(_chunk_diff_by_file(raw, len(raw) or 1)):
        invalid: list[str] = []
        offset = 0
        while offset < len(record):
            try:
                record[offset:].decode("utf-8")
                break
            except UnicodeDecodeError as error:
                invalid.append(record[offset + error.start : offset + error.end].hex())
                offset += error.end
        if invalid:
            named = f"a/{paths[index]}".encode() if index < len(paths) else b""
            path = (
                paths[index]
                if index < len(paths) and named in record
                else f"file record {index + 1}"
            )
            details.append((path, ", ".join(f"0x{value}" for value in invalid)))
    return details


_SETTLED_STAGES = frozenset({StagePhase.COMPLETED, StagePhase.FAILED, StagePhase.BLOCKED})
"""Stage phases that will never advance again, so a retry needs a new key."""

# How many consecutive failed publication passes make a lane's problem. A remote
# that has not answered yet and a remote that answered badly produce the same
# exception, and classifying GitHub's error strings to tell them apart is
# guesswork that goes stale. Counting them needs no taxonomy: the transient case
# clears itself on the next tick, and the real one does not.
_PUBLICATION_ATTEMPTS = 3
_PYTEST_SUMMARY_DURATION = re.compile(
    r"^(?P<prefix>(?:\d+ (?:failed|passed|errors?|skipped|warnings?|xfailed|xpassed|"
    r"deselected|subtests passed)\b|no tests ran\b)[^\n]*?)"
    r"\bin \d+(?:\.\d+)?s(?: \([^\n)]*\))?(?P<suffix>[ \t]*)$",
)

_RETRY_MARKER = ":retry"
"""Separates a stage key from the ordinal that makes a repeat of it dispatchable."""


def _next_key(settled: set[str], base: str) -> str:
    """Name the next stage under `base`, keeping the first attempt's key intact.

    Only keys still under `base` count. A stage retired by a reopen was renamed
    out of this family on purpose, which frees the base key again - and the
    rename appends its marker, so membership has to be decided on the whole
    remainder rather than on a prefix. Matching the prefix counted every
    retired stage as well, which handed a reopened finding an ordinal it had
    never reached and blocked it on the tick right after the reopen.
    """

    retried = sum(1 for key in settled if _under_base(key, base))
    return f"{base}{_RETRY_MARKER}{retried}" if retried else base


def _under_base(stage_key: str, base: str) -> bool:
    """Say whether a key is `base` itself or one of its own retries."""

    if stage_key == base:
        return True
    prefix = f"{base}{_RETRY_MARKER}"
    return stage_key.startswith(prefix) and stage_key[len(prefix) :].isdigit()


def _retry_ordinal(stage_key: str) -> int:
    """Read how many times this stage key has already been retried.

    A key carries the ordinal `_next_key` gave it, so the count survives a
    restart without a separate ledger. A reopen renames a stage out of the
    family on purpose, and the trailing marker it adds is not an ordinal.
    """

    _, marker, tail = stage_key.rpartition(_RETRY_MARKER)
    if not marker or not tail.isdigit():
        return 0
    return int(tail)


def _reject_unrunnable_validation(report: InitialReviewReport) -> None:
    """Refuse a review whose validation a shell-free runner can never execute.

    A command carrying an operator is not a check that fails, it is a check
    that cannot run, and the fix loop cannot tell those apart: run 1f13dd37
    spent five adjudications on a fix that was already correct because its
    finding required `cd app/ui && npx tsc -b`. Catch it while the reviewer is
    still dispatched and can restate the requirement.
    """

    _reject_unrunnable_findings(report.findings)


def _reject_unrunnable_findings(findings: list[ReviewFinding]) -> None:
    """Reject finding commands whose syntax the shell-free runner cannot honor."""

    for finding in findings:
        for requirement in finding.validation:
            found = sorted({token for token in SHELL_OPERATORS if token in requirement.command})
            if found:
                raise ValueError(
                    f"finding {finding.id} requires a validation command using shell syntax "
                    f"({', '.join(found)}), but validation runs without a shell: give one "
                    "executable per requirement and set workdir instead of `cd`"
                )


def _govern_validation_requirement(requirement: ValidationRequirement) -> ValidationRequirement:
    """Turn reviewer pytest intent into the repository's runnable invocation."""

    try:
        arguments = shlex.split(requirement.command)
    except ValueError:
        return requirement.model_copy(update={"baseline_result": None})
    pytest_arguments = _pytest_arguments(arguments)
    if pytest_arguments is None:
        return requirement.model_copy(update={"baseline_result": None})
    already_safe = _has_supervisor_pytest_flags(pytest_arguments)
    pytest_arguments = _without_supervisor_pytest_flags(pytest_arguments)
    governed = ["uv", "run", "--extra", "dev", "pytest"]
    if already_safe or _is_scoped_pytest(pytest_arguments):
        governed.extend(("--no-cov", "-p", "no:cacheprovider"))
    governed.extend(pytest_arguments)
    return requirement.model_copy(
        update={"command": shlex.join(governed), "baseline_result": None}
    )


def _pytest_arguments(arguments: list[str]) -> list[str] | None:
    """Return pytest intent without trusting the reviewer's environment wrapper."""

    if not arguments:
        return None
    if arguments[0] == "pytest":
        return arguments[1:]
    if len(arguments) >= 3 and arguments[0] in {"python", "python3"}:
        return arguments[3:] if arguments[1:3] == ["-m", "pytest"] else None
    if arguments[0] == "uv" and "run" in arguments[1:]:
        run_index = arguments.index("run", 1)
        try:
            pytest_index = arguments.index("pytest", run_index + 1)
        except ValueError:
            return None
        return arguments[pytest_index + 1 :]
    return None


def _is_scoped_pytest(arguments: list[str]) -> bool:
    """Recognize a test/module selection that cannot satisfy whole-package coverage."""

    selectors = {
        "-k",
        "-m",
        "--co",
        "--collect-only",
        "--deselect",
        "--ff",
        "--ignore",
        "--ignore-glob",
        "--lf",
        "--pyargs",
        "--setup-only",
    }
    options_with_values = {
        "-n",
        "-o",
        "-p",
        "-W",
        "--assert",
        "--basetemp",
        "--capture",
        "--color",
        "--confcutdir",
        "--cov",
        "--cov-config",
        "--cov-context",
        "--cov-fail-under",
        "--cov-report",
        "--dist",
        "--durations",
        "--durations-min",
        "--import-mode",
        "--junit-prefix",
        "--junitxml",
        "--log-cli-level",
        "--log-date-format",
        "--log-file",
        "--log-file-date-format",
        "--log-file-format",
        "--log-file-level",
        "--log-format",
        "--log-level",
        "--maxfail",
        "--numprocesses",
        "--override-ini",
        "--pastebin",
        "--rootdir",
        "--tb",
        "--verbosity",
    }
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if (
            argument in selectors
            or argument.startswith(
                ("-k=", "-m=", "--deselect=", "--ignore=", "--ignore-glob=")
            )
            or "::" in argument
        ):
            return True
        if argument in options_with_values:
            index += 2
            continue
        if not argument.startswith("-"):
            return True
        index += 1
    return False


def _has_supervisor_pytest_flags(arguments: list[str]) -> bool:
    """Preserve an explicit safe invocation even when its scope is unconventional."""

    return "--no-cov" in arguments or any(
        arguments[index : index + 2] == ["-p", "no:cacheprovider"]
        or argument == "-pno:cacheprovider"
        for index, argument in enumerate(arguments)
    )


def _without_supervisor_pytest_flags(arguments: list[str]) -> list[str]:
    """Remove flags orkastrator owns before adding its canonical form once."""

    cleaned: list[str] = []
    index = 0
    while index < len(arguments):
        if arguments[index] == "--no-cov":
            index += 1
            continue
        if arguments[index : index + 2] == ["-p", "no:cacheprovider"]:
            index += 2
            continue
        if arguments[index] == "-pno:cacheprovider":
            index += 1
            continue
        cleaned.append(arguments[index])
        index += 1
    return cleaned


def _validation_satisfied(finding: FindingRecord, attempt: FixAttempt) -> bool:
    """Require the finding's own validation list, and treat it as the whole obligation.

    The finding names the checks that prove it resolved, so a fix that skips one
    has not been shown to work. Nothing here demands more: re-running a whole lane
    suite to close one bounded finding is the cost this workflow exists to avoid.
    """

    requirements = finding.effective_contract.validation
    observed = attempt.validation_results[: len(requirements)]
    return len(observed) == len(requirements) and all(
        result.command == requirement.command and result.status == "passed"
        for requirement, result in zip(requirements, observed, strict=True)
    )


def _validation_failed_identically_at_base_and_head(
    finding: FindingRecord, attempt: FixAttempt
) -> bool:
    """Ignore only failed gates whose supervisor-frozen base result is unchanged."""

    requirements = finding.effective_contract.validation
    observed_results = attempt.validation_results[: len(requirements)]
    if len(observed_results) != len(requirements):
        return False
    matched_failure = False
    for requirement, observed in zip(requirements, observed_results, strict=True):
        if observed.command != requirement.command:
            return False
        if observed.status == "passed":
            continue
        baseline = requirement.baseline_result
        if (
            observed.status != "failed"
            or baseline is None
            or baseline.status != "failed"
            or baseline.command != observed.command
            or _validation_failure_fingerprint(baseline.output)
            != _validation_failure_fingerprint(observed.output)
        ):
            return False
        matched_failure = True
    return matched_failure


def _validation_failure_fingerprint(output: str) -> str:
    """Remove pytest summary timing while preserving assertion evidence."""

    summary, separator, evidence = output.partition("\n")
    stable_summary = _PYTEST_SUMMARY_DURATION.sub(
        r"\g<prefix>in <duration>\g<suffix>", summary
    )
    return separator.join((stable_summary, evidence)) if separator else stable_summary


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


def _aware(value: datetime) -> datetime:
    """Read a ledger timestamp as UTC, which is the only thing it was ever written in."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sha256_json(value: object) -> str:
    """Return a stable digest for one accepted structured value."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
