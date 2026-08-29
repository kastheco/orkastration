import type { PolicyV1 } from "./policy.ts";

type RoleName = "worker" | "initial_reviewer" | "fixer" | "re_reviewer";
type RoleSettings = { model: string; thinking: string; fast: boolean };
type FindingCategory = "correctness" | "security" | "data_loss" | "scope" | "acceptance";
type Incident =
  | "worker_blocked"
  | "review_bound_reached"
  | "fix_scope_escape"
  | "validation_failed"
  | "policy_limit_reached"
  | "identity_mismatch"
  | "integration_conflict"
  | "cleanup_unverified"
  | "integration_conflict_unresolved"
  | "policy_override";
type Observation = { elapsedMs: number; totalTokens: number; totalCostMicros: number };
type Finding = {
  id: string;
  category: FindingCategory;
  groupId: string;
  rounds: number;
  state: "queued" | "active" | "accepted";
};
type GroupScope = { groupId: string; findingIds: string[] };
type WorkCursor =
  | { phase: "worker"; attempt: number }
  | { phase: "initial_review" }
  | { phase: "fix_batch"; findings: Finding[]; groups: GroupScope[] }
  | { phase: "re_review"; findings: Finding[]; groups: GroupScope[] }
  | { phase: "validation"; reviewAccepted: boolean };
type TerminalOutcome = "ready_for_manual_integration" | "stopped";
type PolicyAction =
  | { actionId: string; type: "run_worker"; attempt: number; role: RoleSettings }
  | { actionId: string; type: "run_initial_review"; role: RoleSettings }
  | { actionId: string; type: "run_fixers"; role: RoleSettings; groups: Array<{ groupId: string; findings: Array<{ id: string; round: number }> }> }
  | { actionId: string; type: "run_re_review"; role: RoleSettings; groups: GroupScope[] }
  | { actionId: string; type: "run_validation"; profile: "repo-default"; requirements: { commit: true; cleanWorktree: true; reviewAcceptance: true } }
  | { actionId: string; type: "wait"; target: "supervisor" | "owner"; incident: Incident; allowedDecisions: Array<"resume" | "stop"> }
  | { actionId: string; type: "outcome"; outcome: TerminalOutcome; incident?: Incident };
type Cursor =
  | WorkCursor
  | {
    phase: "waiting";
    incident: Incident;
    target: "supervisor" | "owner";
    allowedDecisions: Array<"resume" | "stop">;
    resumeCursor: WorkCursor;
    resumeAction: PolicyAction;
  }
  | { phase: "terminal"; outcome: TerminalOutcome; incident?: Incident };

/** Durable, bounded policy state. Trace data intentionally does not live here. */
export interface PolicyCheckpoint {
  revision: number;
  elapsedMs: number;
  totalTokens: number;
  totalCostMicros: number;
  cursor: Cursor;
}

type EventBase = { sequence: number; observation: Observation };
type FindingInput = { id: string; category: FindingCategory; groupId: string };
type FixGroupResult = { groupId: string; findingIds: string[]; scopePreserved: boolean };
type ReviewGroupResult = { groupId: string; findings: Array<{ id: string; accepted: boolean }> };

/** One closed observation consumed by one reduction. */
export type PolicyEvent =
  | (EventBase & { type: "worker_completed" })
  | (EventBase & { type: "worker_retryable_failure" })
  | (EventBase & { type: "worker_blocked" })
  | (EventBase & { type: "initial_review_completed"; findings: FindingInput[] })
  | (EventBase & { type: "fix_batch_completed"; groups: FixGroupResult[] })
  | (EventBase & { type: "re_review_completed"; groups: ReviewGroupResult[] })
  | (EventBase & { type: "validation_completed"; passed: boolean; commitPresent: boolean; cleanWorktree: boolean; reviewAccepted: boolean })
  | (EventBase & { type: "incident"; incident: Incident })
  | (EventBase & { type: "decision"; target: "supervisor" | "owner"; decision: "resume" | "stop" });

/** The next durable checkpoint and exactly one deterministic business action. */
export interface PolicyReduction {
  checkpoint: PolicyCheckpoint;
  action: PolicyAction;
  ruleId: string;
  occurrenceId: string;
  trace: string[];
}

const CATEGORIES: readonly FindingCategory[] = ["acceptance", "correctness", "data_loss", "scope", "security"];
const INCIDENTS: readonly Incident[] = [
  "cleanup_unverified",
  "fix_scope_escape",
  "identity_mismatch",
  "integration_conflict",
  "integration_conflict_unresolved",
  "policy_limit_reached",
  "policy_override",
  "review_bound_reached",
  "validation_failed",
  "worker_blocked",
];
const EVENT_PHASES: Readonly<Record<PolicyEvent["type"], readonly Cursor["phase"][]>> = {
  worker_completed: ["worker"],
  worker_retryable_failure: ["worker"],
  worker_blocked: ["worker"],
  initial_review_completed: ["initial_review"],
  fix_batch_completed: ["fix_batch"],
  re_review_completed: ["re_review"],
  validation_completed: ["validation"],
  incident: ["worker", "initial_review", "fix_batch", "re_review", "validation"],
  decision: ["waiting"],
};
const MAX_FINDINGS = 1_000;
const MAX_TEXT_LENGTH = 256;

/** Purely reduce one sequenced event under the supplied immutable v1 policy. */
export function reducePolicy({
  policy,
  checkpoint,
  event,
}: {
  policy: PolicyV1;
  checkpoint: PolicyCheckpoint;
  event: PolicyEvent;
}): PolicyReduction {
  validatePolicy(policy);
  validateCheckpoint(checkpoint);
  validateEventEnvelope(checkpoint, event);
  validateEventPayload(event);

  const base = observationCheckpoint(checkpoint, event.observation);
  const hardLimit = reachedLimit(policy, event.observation);
  if (hardLimit !== undefined && !(checkpoint.cursor.phase === "waiting" && checkpoint.cursor.incident === "policy_limit_reached")) {
    return incidentReduction(policy, base, "policy_limit_reached", `limit.${hardLimit}`, [
      "event.legal",
      "observation.valid",
      `limit.${hardLimit}.reached`,
    ], true);
  }

  switch (event.type) {
    case "worker_completed":
      return roleReduction(base, { phase: "initial_review" }, "worker.completed", "run_initial_review", policy, "initial_reviewer");
    case "worker_retryable_failure":
      return reduceWorkerRetry(policy, base);
    case "worker_blocked":
      return incidentReduction(policy, base, "worker_blocked", "worker.blocked", ["event.legal", "observation.valid", "worker.blocked"], false);
    case "initial_review_completed":
      return reduceInitialReview(policy, base, event.findings);
    case "fix_batch_completed":
      return reduceFixBatch(policy, base, event.groups);
    case "re_review_completed":
      return reduceReReview(policy, base, event.groups);
    case "validation_completed":
      return reduceValidation(policy, base, event);
    case "incident":
      return incidentReduction(
        policy,
        base,
        event.incident,
        `incident.${event.incident}`,
        ["event.legal", "observation.valid", `incident.${event.incident}`],
        event.incident === "policy_limit_reached",
      );
    case "decision":
      return reduceDecision(base, event);
  }
}

function reduceWorkerRetry(policy: PolicyV1, checkpoint: PolicyCheckpoint): PolicyReduction {
  const cursor = checkpoint.cursor;
  if (cursor.phase !== "worker") throw new Error("internal worker cursor mismatch");
  if (cursor.attempt >= policy.limits.max_worker_attempts) {
    return incidentReduction(policy, checkpoint, "worker_blocked", "worker.attempt_bound", [
      "event.legal",
      "observation.valid",
      "worker.attempt_bound",
    ], false);
  }
  const attempt = cursor.attempt + 1;
  const nextCursor: WorkCursor = { phase: "worker", attempt };
  return reduction(checkpoint, nextCursor, "worker.retry", ["event.legal", "observation.valid", "worker.retry"], (actionId) => ({
    actionId,
    type: "run_worker",
    attempt,
    role: role(policy, "worker"),
  }));
}

function reduceInitialReview(policy: PolicyV1, checkpoint: PolicyCheckpoint, inputs: FindingInput[]): PolicyReduction {
  const blocking = new Set<FindingCategory>(policy.review.blocking as readonly FindingCategory[]);
  const findings: Finding[] = inputs
    .filter((finding) => blocking.has(finding.category))
    .map((finding) => ({ ...finding, rounds: 0, state: "queued" as const }))
    .sort(compareFinding);
  if (findings.length === 0) {
    return validationAction(policy, checkpoint, "review.initial.accepted", ["event.legal", "observation.valid", "review.no_blocking_findings"]);
  }
  return scheduleFixers(policy, checkpoint, findings, "review.initial.blocking", [
    "event.legal",
    "observation.valid",
    "review.blocking_findings",
  ]);
}

function reduceFixBatch(policy: PolicyV1, checkpoint: PolicyCheckpoint, results: FixGroupResult[]): PolicyReduction {
  const cursor = checkpoint.cursor;
  if (cursor.phase !== "fix_batch") throw new Error("internal fixer cursor mismatch");
  assertFixScope(cursor.groups, results);
  if (results.some((group) => !group.scopePreserved)) {
    return incidentReduction(policy, checkpoint, "fix_scope_escape", "fix.scope_escape", [
      "event.legal",
      "observation.valid",
      "fix.scope_escape",
    ], false);
  }
  const groups = cloneScopes(cursor.groups);
  const findings = cloneFindings(cursor.findings);
  return reduction(checkpoint, { phase: "re_review", findings, groups }, "fix.completed", [
    "event.legal",
    "observation.valid",
    "fix.scope_preserved",
  ], (actionId) => ({
    actionId,
    type: "run_re_review",
    role: role(policy, "re_reviewer"),
    groups: cloneScopes(groups),
  }));
}

function reduceReReview(policy: PolicyV1, checkpoint: PolicyCheckpoint, results: ReviewGroupResult[]): PolicyReduction {
  const cursor = checkpoint.cursor;
  if (cursor.phase !== "re_review") throw new Error("internal re-review cursor mismatch");
  assertReviewScope(cursor.groups, results);

  const decisions = new Map<string, boolean>();
  for (const group of results) for (const finding of group.findings) decisions.set(finding.id, finding.accepted);
  const findings = cloneFindings(cursor.findings).map((finding): Finding => {
    const accepted = decisions.get(finding.id);
    if (accepted === undefined) return finding;
    return { ...finding, state: accepted ? "accepted" : "queued" };
  });
  const rejected = findings.filter((finding) => decisions.get(finding.id) === false);
  if (rejected.some((finding) => finding.rounds >= policy.review.max_fix_rounds_per_finding)) {
    return incidentReduction(policy, checkpoint, "review_bound_reached", "review.round_bound", [
      "event.legal",
      "observation.valid",
      "review.round_bound",
    ], false);
  }
  if (rejected.length > 0) {
    return scheduleFixers(
      policy,
      checkpoint,
      findings,
      "review.rejected",
      ["event.legal", "observation.valid", "review.rejected"],
      new Set(rejected.map((finding) => finding.groupId)),
    );
  }
  if (findings.some((finding) => finding.state === "queued")) {
    return scheduleFixers(policy, checkpoint, findings, "review.next_groups", ["event.legal", "observation.valid", "review.batch_accepted", "review.next_groups"]);
  }
  return validationAction(policy, checkpoint, "review.all_accepted", ["event.legal", "observation.valid", "review.all_accepted"]);
}

function reduceValidation(
  policy: PolicyV1,
  checkpoint: PolicyCheckpoint,
  event: Extract<PolicyEvent, { type: "validation_completed" }>,
): PolicyReduction {
  if (!event.passed) {
    return incidentReduction(policy, checkpoint, "validation_failed", "validation.failed", [
      "event.legal",
      "observation.valid",
      "validation.failed",
    ], false);
  }
  const cursor = checkpoint.cursor;
  if (cursor.phase !== "validation") throw new Error("internal validation cursor mismatch");
  if (
    (policy.completion.require_commit && !event.commitPresent)
    || (policy.completion.require_clean_worktree && !event.cleanWorktree)
    || (policy.completion.require_review_acceptance && (!event.reviewAccepted || !cursor.reviewAccepted))
  ) {
    return incidentReduction(policy, checkpoint, "cleanup_unverified", "validation.cleanup_unverified", [
      "event.legal",
      "observation.valid",
      "validation.passed",
      "completion.proof_missing",
    ], false);
  }
  const terminal: Cursor = { phase: "terminal", outcome: "ready_for_manual_integration" };
  return reduction(checkpoint, terminal, "completion.ready_for_manual_integration", [
    "event.legal",
    "observation.valid",
    "validation.passed",
    "completion.proofs_verified",
    "completion.manual_integration",
  ], (actionId) => ({ actionId, type: "outcome", outcome: "ready_for_manual_integration" }));
}

function reduceDecision(
  checkpoint: PolicyCheckpoint,
  event: Extract<PolicyEvent, { type: "decision" }>,
): PolicyReduction {
  const cursor = checkpoint.cursor;
  if (cursor.phase !== "waiting") throw new Error("internal waiting cursor mismatch");
  if (event.target !== cursor.target) throw new Error(`decision target ${event.target} does not match recorded target ${cursor.target}`);
  if (!cursor.allowedDecisions.includes(event.decision)) throw new Error(`decision ${event.decision} is not allowed`);
  if (event.decision === "stop") {
    const terminal: Cursor = { phase: "terminal", outcome: "stopped", incident: cursor.incident };
    return reduction(checkpoint, terminal, "decision.stop", ["event.legal", "observation.valid", "decision.target_verified", "decision.stop"], (actionId) => ({
      actionId,
      type: "outcome",
      outcome: "stopped",
      incident: cursor.incident,
    }));
  }
  const resumeCursor = cloneWorkCursor(cursor.resumeCursor);
  return reduction(checkpoint, resumeCursor, "decision.resume", ["event.legal", "observation.valid", "decision.target_verified", "decision.resume_exact_work"], (actionId) => withActionId(cursor.resumeAction, actionId));
}

function scheduleFixers(
  policy: PolicyV1,
  checkpoint: PolicyCheckpoint,
  source: Finding[],
  ruleId: string,
  trace: string[],
  onlyGroups?: ReadonlySet<string>,
): PolicyReduction {
  const findings = cloneFindings(source);
  const queuedGroups = [...new Set(findings
    .filter((finding) => finding.state === "queued" && (onlyGroups === undefined || onlyGroups.has(finding.groupId)))
    .map((finding) => finding.groupId))]
    .sort(compareText)
    .slice(0, policy.review.max_parallel_fixer_groups);
  if (queuedGroups.length === 0) throw new Error("no queued finding group to schedule");
  const selected = new Set(queuedGroups);
  for (const finding of findings) {
    if (finding.state === "queued" && selected.has(finding.groupId)) {
      finding.state = "active";
      finding.rounds += 1;
    }
  }
  findings.sort(compareFinding);
  const groups = scopesFor(findings.filter((finding) => finding.state === "active" && selected.has(finding.groupId)));
  return reduction(checkpoint, { phase: "fix_batch", findings, groups }, ruleId, [...trace, "fix.round_charged"], (actionId) => ({
    actionId,
    type: "run_fixers",
    role: role(policy, "fixer"),
    groups: groups.map((group) => ({
      groupId: group.groupId,
      findings: group.findingIds.map((id) => {
        const finding = findings.find((candidate) => candidate.id === id);
        if (finding === undefined) throw new Error("internal missing finding");
        return { id, round: finding.rounds };
      }),
    })),
  }));
}

function validationAction(policy: PolicyV1, checkpoint: PolicyCheckpoint, ruleId: string, trace: string[]): PolicyReduction {
  return reduction(checkpoint, { phase: "validation", reviewAccepted: true }, ruleId, trace, (actionId) => ({
    actionId,
    type: "run_validation",
    profile: policy.validation.profile,
    requirements: { commit: true, cleanWorktree: true, reviewAcceptance: true },
  }));
}

function roleReduction(
  checkpoint: PolicyCheckpoint,
  cursor: WorkCursor,
  ruleId: string,
  actionType: "run_initial_review",
  policy: PolicyV1,
  roleName: "initial_reviewer",
): PolicyReduction {
  return reduction(checkpoint, cursor, ruleId, ["event.legal", "observation.valid", ruleId], (actionId) => ({
    actionId,
    type: actionType,
    role: role(policy, roleName),
  }));
}

function incidentReduction(
  policy: PolicyV1,
  checkpoint: PolicyCheckpoint,
  incident: Incident,
  ruleId: string,
  trace: string[],
  hardLimit: boolean,
): PolicyReduction {
  const cursor = checkpoint.cursor;
  if (cursor.phase === "terminal" || cursor.phase === "waiting") throw new Error("incident requires active work");
  const human = policy.supervision.require_human_on.includes(incident as never);
  const wake = policy.supervision.wake_on.includes(incident as never);
  if (!human && !wake) {
    const terminal: Cursor = { phase: "terminal", outcome: "stopped", incident };
    return reduction(checkpoint, terminal, `${ruleId}.fail_closed`, [...trace, "supervision.unconfigured", "supervision.fail_closed"], (actionId) => ({
      actionId,
      type: "outcome",
      outcome: "stopped",
      incident,
    }));
  }
  const target = human ? "owner" : "supervisor";
  const allowedDecisions: Array<"resume" | "stop"> = hardLimit ? ["stop"] : ["resume", "stop"];
  const resumeCursor = cloneWorkCursor(cursor);
  const resumeAction = actionForCursor(policy, cursor, "resume-pending");
  const waiting: Cursor = {
    phase: "waiting",
    incident,
    target,
    allowedDecisions: [...allowedDecisions],
    resumeCursor,
    resumeAction,
  };
  return reduction(checkpoint, waiting, ruleId, [...trace, human ? "supervision.require_human" : "supervision.wake"], (actionId) => ({
    actionId,
    type: "wait",
    target,
    incident,
    allowedDecisions: [...allowedDecisions],
  }));
}

function actionForCursor(policy: PolicyV1, cursor: WorkCursor, actionId: string): PolicyAction {
  switch (cursor.phase) {
    case "worker": return { actionId, type: "run_worker", attempt: cursor.attempt, role: role(policy, "worker") };
    case "initial_review": return { actionId, type: "run_initial_review", role: role(policy, "initial_reviewer") };
    case "fix_batch": return {
      actionId,
      type: "run_fixers",
      role: role(policy, "fixer"),
      groups: cursor.groups.map((group) => ({
        groupId: group.groupId,
        findings: group.findingIds.map((id) => {
          const finding = cursor.findings.find((candidate) => candidate.id === id);
          if (finding === undefined) throw new Error("fix scope references an unknown finding");
          return { id, round: finding.rounds };
        }),
      })),
    };
    case "re_review": return { actionId, type: "run_re_review", role: role(policy, "re_reviewer"), groups: cloneScopes(cursor.groups) };
    case "validation": return {
      actionId,
      type: "run_validation",
      profile: policy.validation.profile,
      requirements: { commit: true, cleanWorktree: true, reviewAcceptance: true },
    };
  }
}

function reduction(
  previous: PolicyCheckpoint,
  cursor: Cursor,
  ruleId: string,
  trace: string[],
  makeAction: (actionId: string) => PolicyAction,
): PolicyReduction {
  const occurrenceId = `${previous.revision}:${ruleId}`;
  const actionId = `${occurrenceId}:action`;
  return {
    checkpoint: {
      revision: previous.revision,
      elapsedMs: previous.elapsedMs,
      totalTokens: previous.totalTokens,
      totalCostMicros: previous.totalCostMicros,
      cursor,
    },
    action: makeAction(actionId),
    ruleId,
    occurrenceId,
    trace: [...trace],
  };
}

function observationCheckpoint(checkpoint: PolicyCheckpoint, observation: Observation): PolicyCheckpoint {
  return {
    revision: checkpoint.revision + 1,
    elapsedMs: observation.elapsedMs,
    totalTokens: observation.totalTokens,
    totalCostMicros: observation.totalCostMicros,
    cursor: checkpoint.cursor,
  };
}

function reachedLimit(policy: PolicyV1, observation: Observation): "wall_clock" | "tokens" | "cost" | undefined {
  if (observation.elapsedMs >= policy.limits.wall_clock_minutes * 60_000) return "wall_clock";
  if (observation.totalTokens >= policy.limits.total_tokens) return "tokens";
  const costLimitMicros = Math.round(policy.limits.total_cost_usd * 1_000_000);
  if (observation.totalCostMicros >= costLimitMicros) return "cost";
  return undefined;
}

function validateEventEnvelope(checkpoint: PolicyCheckpoint, event: PolicyEvent): void {
  if (!isRecord(event) || typeof event.type !== "string" || !(event.type in EVENT_PHASES)) throw new Error("unknown policy event");
  if (!Number.isSafeInteger(event.sequence) || event.sequence !== checkpoint.revision + 1) {
    throw new Error(`event sequence must equal ${checkpoint.revision + 1}`);
  }
  if (checkpoint.cursor.phase === "terminal") throw new Error("terminal checkpoint rejects all events");
  const phases = EVENT_PHASES[event.type];
  if (!phases.includes(checkpoint.cursor.phase)) throw new Error(`event ${event.type} is illegal in phase ${checkpoint.cursor.phase}`);
  validateObservation(checkpoint, event.observation);
}

function validateObservation(checkpoint: PolicyCheckpoint, observation: Observation): void {
  if (!isRecord(observation)) throw new Error("event observation is required");
  for (const key of ["elapsedMs", "totalTokens", "totalCostMicros"] as const) {
    const value = observation[key];
    if (!Number.isFinite(value) || !Number.isSafeInteger(value) || value < 0) throw new Error(`observation ${key} must be a finite nonnegative safe integer`);
    if (value < checkpoint[key]) throw new Error(`observation ${key} must be monotonic`);
  }
}

function validateEventPayload(event: PolicyEvent): void {
  switch (event.type) {
    case "initial_review_completed": {
      if (!Array.isArray(event.findings) || event.findings.length > MAX_FINDINGS) throw new Error(`findings must contain at most ${MAX_FINDINGS} items`);
      const ids = new Set<string>();
      for (const finding of event.findings) {
        validateText(finding.id, "finding id");
        validateText(finding.groupId, "finding groupId");
        if (!CATEGORIES.includes(finding.category)) throw new Error(`unknown finding category ${String(finding.category)}`);
        if (ids.has(finding.id)) throw new Error(`duplicate finding id ${finding.id}`);
        ids.add(finding.id);
      }
      return;
    }
    case "fix_batch_completed":
      if (!Array.isArray(event.groups)) throw new Error("fix groups must be an array");
      for (const group of event.groups) {
        validateText(group.groupId, "fix groupId");
        validateTextList(group.findingIds, "fix findingIds");
        if (typeof group.scopePreserved !== "boolean") throw new Error("scopePreserved must be boolean");
      }
      return;
    case "re_review_completed":
      if (!Array.isArray(event.groups)) throw new Error("review groups must be an array");
      for (const group of event.groups) {
        validateText(group.groupId, "review groupId");
        if (!Array.isArray(group.findings)) throw new Error("review findings must be an array");
        const ids = new Set<string>();
        for (const finding of group.findings) {
          validateText(finding.id, "review finding id");
          if (ids.has(finding.id)) throw new Error(`duplicate review finding id ${finding.id}`);
          if (typeof finding.accepted !== "boolean") throw new Error("review accepted must be boolean");
          ids.add(finding.id);
        }
      }
      return;
    case "validation_completed":
      for (const key of ["passed", "commitPresent", "cleanWorktree", "reviewAccepted"] as const) {
        if (typeof event[key] !== "boolean") throw new Error(`validation ${key} must be boolean`);
      }
      return;
    case "incident":
      if (!INCIDENTS.includes(event.incident)) throw new Error(`unknown incident ${String(event.incident)}`);
      return;
    case "decision":
      if (event.target !== "owner" && event.target !== "supervisor") throw new Error("unknown decision target");
      if (event.decision !== "resume" && event.decision !== "stop") throw new Error("unknown decision");
      return;
    default:
      return;
  }
}

function validateCheckpoint(checkpoint: PolicyCheckpoint): void {
  if (!isRecord(checkpoint)) throw new Error("checkpoint must be an object");
  for (const key of ["revision", "elapsedMs", "totalTokens", "totalCostMicros"] as const) {
    const value = checkpoint[key];
    if (!Number.isSafeInteger(value) || value < 0) throw new Error(`checkpoint ${key} must be a nonnegative safe integer`);
  }
  validateCursor(checkpoint.cursor, false);
}

function validateCursor(cursor: Cursor, nested: boolean): void {
  if (!isRecord(cursor) || typeof cursor.phase !== "string") throw new Error("checkpoint cursor is malformed");
  switch (cursor.phase) {
    case "worker":
      if (!Number.isSafeInteger(cursor.attempt) || cursor.attempt < 1) throw new Error("worker attempt must be a positive safe integer");
      return;
    case "initial_review":
    case "validation":
      if (cursor.phase === "validation" && typeof cursor.reviewAccepted !== "boolean") throw new Error("validation reviewAccepted must be boolean");
      return;
    case "fix_batch":
    case "re_review":
      validateFindings(cursor.findings);
      validateScopes(cursor.groups, cursor.findings);
      return;
    case "waiting":
      if (nested) throw new Error("waiting cursor cannot be nested");
      if (!INCIDENTS.includes(cursor.incident)) throw new Error("waiting incident is invalid");
      if (cursor.target !== "owner" && cursor.target !== "supervisor") throw new Error("waiting target is invalid");
      if (!Array.isArray(cursor.allowedDecisions) || cursor.allowedDecisions.length < 1 || cursor.allowedDecisions.some((item) => item !== "resume" && item !== "stop")) throw new Error("waiting decisions are invalid");
      validateCursor(cursor.resumeCursor, true);
      if (!isRecord(cursor.resumeAction) || typeof cursor.resumeAction.type !== "string" || typeof cursor.resumeAction.actionId !== "string") throw new Error("waiting resume action is invalid");
      return;
    case "terminal":
      if (cursor.outcome !== "ready_for_manual_integration" && cursor.outcome !== "stopped") throw new Error("terminal outcome is invalid");
      return;
    default:
      throw new Error(`unknown checkpoint phase ${String((cursor as { phase?: unknown }).phase)}`);
  }
}

function validatePolicy(policy: PolicyV1): void {
  if (!isRecord(policy) || policy.version !== 1) throw new Error("policy must be v1");
  const limits = policy.limits;
  if (!Number.isSafeInteger(limits.wall_clock_minutes) || limits.wall_clock_minutes < 1) throw new Error("policy wall clock limit is invalid");
  if (!Number.isSafeInteger(limits.total_tokens) || limits.total_tokens < 1) throw new Error("policy token limit is invalid");
  if (!Number.isFinite(limits.total_cost_usd) || limits.total_cost_usd <= 0 || Math.round(limits.total_cost_usd * 1_000_000) > Number.MAX_SAFE_INTEGER) throw new Error("policy cost limit is invalid");
  if (!Number.isSafeInteger(limits.max_worker_attempts) || limits.max_worker_attempts < 1) throw new Error("policy worker attempt bound is invalid");
  if (!Number.isSafeInteger(policy.review.max_fix_rounds_per_finding) || policy.review.max_fix_rounds_per_finding < 1) throw new Error("policy review round bound is invalid");
  if (!Number.isSafeInteger(policy.review.max_parallel_fixer_groups) || policy.review.max_parallel_fixer_groups < 1) throw new Error("policy parallel fixer bound is invalid");
  if (policy.validation.profile !== "repo-default" || policy.completion.integration !== "manual") throw new Error("unsupported policy execution mode");
}

function validateFindings(findings: Finding[]): void {
  if (!Array.isArray(findings) || findings.length > MAX_FINDINGS) throw new Error("checkpoint findings are invalid");
  const ids = new Set<string>();
  for (const finding of findings) {
    validateText(finding.id, "checkpoint finding id");
    validateText(finding.groupId, "checkpoint finding groupId");
    if (ids.has(finding.id) || !CATEGORIES.includes(finding.category)) throw new Error("checkpoint finding is invalid");
    if (!Number.isSafeInteger(finding.rounds) || finding.rounds < 0) throw new Error("checkpoint finding rounds are invalid");
    if (finding.state !== "queued" && finding.state !== "active" && finding.state !== "accepted") throw new Error("checkpoint finding state is invalid");
    ids.add(finding.id);
  }
}

function validateScopes(groups: GroupScope[], findings: Finding[]): void {
  if (!Array.isArray(groups) || groups.length > MAX_FINDINGS) throw new Error("checkpoint groups are invalid");
  const known = new Set(findings.map((finding) => finding.id));
  const groupIds = new Set<string>();
  for (const group of groups) {
    validateText(group.groupId, "checkpoint groupId");
    validateTextList(group.findingIds, "checkpoint findingIds");
    if (groupIds.has(group.groupId) || group.findingIds.some((id) => !known.has(id))) throw new Error("checkpoint group scope is invalid");
    groupIds.add(group.groupId);
  }
}

function assertFixScope(expected: GroupScope[], actual: FixGroupResult[]): void {
  const normalized = actual.map((group) => ({ groupId: group.groupId, findingIds: [...group.findingIds].sort(compareText) })).sort(compareScope);
  if (JSON.stringify(normalized) !== JSON.stringify(cloneScopes(expected))) throw new Error("fix result scope does not match assigned scope");
}

function assertReviewScope(expected: GroupScope[], actual: ReviewGroupResult[]): void {
  const normalized = actual.map((group) => ({ groupId: group.groupId, findingIds: group.findings.map((finding) => finding.id).sort(compareText) })).sort(compareScope);
  if (JSON.stringify(normalized) !== JSON.stringify(cloneScopes(expected))) throw new Error("re-review result scope does not match assigned scope");
}

function scopesFor(findings: Finding[]): GroupScope[] {
  const byGroup = new Map<string, string[]>();
  for (const finding of findings) {
    const ids = byGroup.get(finding.groupId) ?? [];
    ids.push(finding.id);
    byGroup.set(finding.groupId, ids);
  }
  return [...byGroup].map(([groupId, ids]) => ({ groupId, findingIds: ids.sort(compareText) })).sort(compareScope);
}

function role(policy: PolicyV1, name: RoleName): RoleSettings {
  const settings = policy.roles[name];
  return { model: settings.model, thinking: settings.thinking, fast: settings.fast };
}

function cloneFindings(findings: Finding[]): Finding[] {
  return findings.map((finding) => ({ ...finding })).sort(compareFinding);
}

function cloneScopes(groups: GroupScope[]): GroupScope[] {
  return groups.map((group) => ({ groupId: group.groupId, findingIds: [...group.findingIds].sort(compareText) })).sort(compareScope);
}

function cloneWorkCursor(cursor: WorkCursor): WorkCursor {
  switch (cursor.phase) {
    case "worker": return { phase: "worker", attempt: cursor.attempt };
    case "initial_review": return { phase: "initial_review" };
    case "validation": return { phase: "validation", reviewAccepted: cursor.reviewAccepted };
    case "fix_batch": return { phase: "fix_batch", findings: cloneFindings(cursor.findings), groups: cloneScopes(cursor.groups) };
    case "re_review": return { phase: "re_review", findings: cloneFindings(cursor.findings), groups: cloneScopes(cursor.groups) };
  }
}

function withActionId(action: PolicyAction, actionId: string): PolicyAction {
  switch (action.type) {
    case "run_worker": return { ...action, actionId, role: { ...action.role } };
    case "run_initial_review": return { ...action, actionId, role: { ...action.role } };
    case "run_fixers": return { ...action, actionId, role: { ...action.role }, groups: action.groups.map((group) => ({ groupId: group.groupId, findings: group.findings.map((finding) => ({ ...finding })) })) };
    case "run_re_review": return { ...action, actionId, role: { ...action.role }, groups: cloneScopes(action.groups) };
    case "run_validation": return { ...action, actionId, requirements: { ...action.requirements } };
    case "wait": return { ...action, actionId, allowedDecisions: [...action.allowedDecisions] };
    case "outcome": return { ...action, actionId };
  }
}

function validateTextList(values: string[], name: string): void {
  if (!Array.isArray(values) || values.length > MAX_FINDINGS) throw new Error(`${name} must be a bounded array`);
  const unique = new Set<string>();
  for (const value of values) {
    validateText(value, name);
    if (unique.has(value)) throw new Error(`${name} must be unique`);
    unique.add(value);
  }
}

function validateText(value: string, name: string): void {
  if (typeof value !== "string" || value.length < 1 || value.length > MAX_TEXT_LENGTH) throw new Error(`${name} must contain 1-${MAX_TEXT_LENGTH} characters`);
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function compareFinding(left: Finding, right: Finding): number {
  return compareText(left.id, right.id);
}

function compareScope(left: GroupScope, right: GroupScope): number {
  return compareText(left.groupId, right.groupId);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
