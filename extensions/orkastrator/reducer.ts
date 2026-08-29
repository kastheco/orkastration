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
  validateCheckpoint(policy, checkpoint);
  validateEventEnvelope(checkpoint, event);
  validateEventPayload(checkpoint, event);

  const base = observationCheckpoint(checkpoint, event.observation);
  const hardLimit = reachedLimit(policy, event.observation);
  const previousHardLimit = reachedLimit(policy, checkpoint);
  if (hardLimit !== undefined && previousHardLimit === undefined) {
    const trace = ["event.legal", "observation.valid", `limit.${hardLimit}.reached`];
    if (checkpoint.cursor.phase === "waiting") {
      return waitingLimitReduction(policy, base, checkpoint.cursor.resumeCursor, `limit.${hardLimit}`, trace);
    }
    return incidentReduction(policy, base, "policy_limit_reached", `limit.${hardLimit}`, trace, true);
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
      return reduceDecision(policy, base, event);
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
    ], true);
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
    .map((finding) => ({
      id: finding.id,
      category: finding.category,
      groupId: finding.groupId,
      rounds: 0,
      state: "queued" as const,
    }))
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
  policy: PolicyV1,
  checkpoint: PolicyCheckpoint,
  event: Extract<PolicyEvent, { type: "decision" }>,
): PolicyReduction {
  const cursor = checkpoint.cursor;
  if (cursor.phase !== "waiting") throw new Error("internal waiting cursor mismatch");
  if (event.decision === "stop") {
    const terminal: Cursor = { phase: "terminal", outcome: "stopped", incident: cursor.incident };
    return reduction(checkpoint, terminal, "decision.stop", ["event.legal", "observation.valid", "decision.target_verified", "decision.stop"], (actionId) => ({
      actionId,
      type: "outcome",
      outcome: "stopped",
      incident: cursor.incident,
    }));
  }
  if (cursor.resumeCursor.phase === "worker" && cursor.resumeCursor.attempt >= policy.limits.max_worker_attempts) {
    throw new Error("worker attempt bound cannot resume");
  }
  const resumeCursor = cloneWorkCursor(cursor.resumeCursor);
  return reduction(checkpoint, resumeCursor, "decision.resume", ["event.legal", "observation.valid", "decision.target_verified", "decision.resume_exact_work"], (actionId) => actionForCursor(policy, resumeCursor, actionId));
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
  if (groups.length === 0 || findings.some((finding) => finding.state === "active" && finding.rounds < 1)) {
    throw new Error("fixer action requires charged active finding groups");
  }
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
  stopOnly: boolean,
): PolicyReduction {
  const cursor = checkpoint.cursor;
  if (cursor.phase === "terminal" || cursor.phase === "waiting") throw new Error("incident requires active work");
  return incidentForWork(policy, checkpoint, cloneWorkCursor(cursor), incident, ruleId, trace, stopOnly);
}

function waitingLimitReduction(
  policy: PolicyV1,
  checkpoint: PolicyCheckpoint,
  resumeCursor: WorkCursor,
  ruleId: string,
  trace: string[],
): PolicyReduction {
  return incidentForWork(policy, checkpoint, cloneWorkCursor(resumeCursor), "policy_limit_reached", ruleId, trace, true);
}

function incidentForWork(
  policy: PolicyV1,
  checkpoint: PolicyCheckpoint,
  resumeCursor: WorkCursor,
  incident: Incident,
  ruleId: string,
  trace: string[],
  stopOnly: boolean,
): PolicyReduction {
  const supervision = supervisionFor(policy, incident, resumeCursor, stopOnly);
  if (supervision === undefined) {
    const terminal: Cursor = { phase: "terminal", outcome: "stopped", incident };
    return reduction(checkpoint, terminal, `${ruleId}.fail_closed`, [...trace, "supervision.unconfigured", "supervision.fail_closed"], (actionId) => ({
      actionId,
      type: "outcome",
      outcome: "stopped",
      incident,
    }));
  }
  const waiting: Cursor = {
    phase: "waiting",
    incident,
    target: supervision.target,
    allowedDecisions: [...supervision.allowedDecisions],
    resumeCursor,
  };
  return reduction(checkpoint, waiting, ruleId, [...trace, supervision.target === "owner" ? "supervision.require_human" : "supervision.wake"], (actionId) => ({
    actionId,
    type: "wait",
    target: supervision.target,
    incident,
    allowedDecisions: [...supervision.allowedDecisions],
  }));
}

function supervisionFor(
  policy: PolicyV1,
  incident: Incident,
  resumeCursor: WorkCursor,
  stopOnly: boolean,
): { target: "supervisor" | "owner"; allowedDecisions: Array<"resume" | "stop"> } | undefined {
  const human = policy.supervision.require_human_on.includes(incident as never);
  const wake = policy.supervision.wake_on.includes(incident as never);
  if (!human && !wake) return undefined;
  const workerBound = resumeCursor.phase === "worker" && resumeCursor.attempt >= policy.limits.max_worker_attempts;
  return {
    target: human ? "owner" : "supervisor",
    allowedDecisions: stopOnly || incident === "policy_limit_reached" || workerBound ? ["stop"] : ["resume", "stop"],
  };
}

function actionForCursor(policy: PolicyV1, cursor: WorkCursor, actionId: string): PolicyAction {
  switch (cursor.phase) {
    case "worker": {
      if (cursor.attempt >= policy.limits.max_worker_attempts) throw new Error("worker attempt bound cannot emit run_worker");
      return { actionId, type: "run_worker", attempt: cursor.attempt, role: role(policy, "worker") };
    }
    case "initial_review": return { actionId, type: "run_initial_review", role: role(policy, "initial_reviewer") };
    case "fix_batch": {
      if (cursor.groups.length === 0) throw new Error("fixer action requires at least one group");
      return {
        actionId,
        type: "run_fixers",
        role: role(policy, "fixer"),
        groups: cursor.groups.map((group) => ({
          groupId: group.groupId,
          findings: group.findingIds.map((id) => {
            const finding = cursor.findings.find((candidate) => candidate.id === id);
            if (finding === undefined) throw new Error("fix scope references an unknown finding");
            if (finding.rounds < 1) throw new Error("fixer action cannot emit round zero");
            return { id, round: finding.rounds };
          }),
        })),
      };
    }
    case "re_review": {
      if (cursor.groups.length === 0) throw new Error("re-review action requires at least one group");
      return { actionId, type: "run_re_review", role: role(policy, "re_reviewer"), groups: cloneScopes(cursor.groups) };
    }
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
  const phases = EVENT_PHASES[event.type as PolicyEvent["type"]];
  if (!phases.includes(checkpoint.cursor.phase)) throw new Error(`event ${event.type} is illegal in phase ${checkpoint.cursor.phase}`);
  validateObservation(checkpoint, event.observation);
}

function validateObservation(checkpoint: PolicyCheckpoint, observation: Observation): void {
  assertExactKeys(observation, ["elapsedMs", "totalCostMicros", "totalTokens"], "observation");
  for (const key of ["elapsedMs", "totalTokens", "totalCostMicros"] as const) {
    const value = observation[key];
    if (!Number.isFinite(value) || !Number.isSafeInteger(value) || value < 0) throw new Error(`observation ${key} must be a finite nonnegative safe integer`);
    if (value < checkpoint[key]) throw new Error(`observation ${key} must be monotonic`);
  }
}

function validateEventPayload(checkpoint: PolicyCheckpoint, event: PolicyEvent): void {
  const baseKeys = ["observation", "sequence", "type"];
  switch (event.type) {
    case "worker_completed":
    case "worker_retryable_failure":
    case "worker_blocked":
      assertExactKeys(event, baseKeys, event.type);
      return;
    case "initial_review_completed": {
      assertExactKeys(event, [...baseKeys, "findings"], event.type);
      if (!Array.isArray(event.findings) || event.findings.length > MAX_FINDINGS) throw new Error(`findings must contain at most ${MAX_FINDINGS} items`);
      const ids = new Set<string>();
      for (const finding of event.findings) {
        assertExactKeys(finding, ["category", "groupId", "id"], "finding");
        validateText(finding.id, "finding id");
        validateText(finding.groupId, "finding groupId");
        if (!CATEGORIES.includes(finding.category)) throw new Error(`unknown finding category ${String(finding.category)}`);
        if (ids.has(finding.id)) throw new Error(`duplicate finding id ${finding.id}`);
        ids.add(finding.id);
      }
      return;
    }
    case "fix_batch_completed": {
      assertExactKeys(event, [...baseKeys, "groups"], event.type);
      const cursor = checkpoint.cursor;
      if (cursor.phase !== "fix_batch") throw new Error("fix result requires fix_batch cursor");
      validateOuterGroups(event.groups, cursor.groups.length, "fix groups");
      const assigned = new Map(cursor.groups.map((group) => [group.groupId, group.findingIds.length]));
      for (const group of event.groups) {
        assertExactKeys(group, ["findingIds", "groupId", "scopePreserved"], "fix group");
        validateText(group.groupId, "fix groupId");
        const assignedCount = assigned.get(group.groupId) ?? 0;
        validateTextList(group.findingIds, "fix findingIds", assignedCount);
        if (typeof group.scopePreserved !== "boolean") throw new Error("scopePreserved must be boolean");
      }
      return;
    }
    case "re_review_completed": {
      assertExactKeys(event, [...baseKeys, "groups"], event.type);
      const cursor = checkpoint.cursor;
      if (cursor.phase !== "re_review") throw new Error("review result requires re_review cursor");
      validateOuterGroups(event.groups, cursor.groups.length, "review groups");
      const assigned = new Map(cursor.groups.map((group) => [group.groupId, group.findingIds.length]));
      for (const group of event.groups) {
        assertExactKeys(group, ["findings", "groupId"], "review group");
        validateText(group.groupId, "review groupId");
        if (!Array.isArray(group.findings) || group.findings.length > (assigned.get(group.groupId) ?? 0)) throw new Error("review findings exceed assigned scope");
        const ids = new Set<string>();
        for (const finding of group.findings) {
          assertExactKeys(finding, ["accepted", "id"], "review verdict");
          validateText(finding.id, "review finding id");
          if (ids.has(finding.id)) throw new Error(`duplicate review finding id ${finding.id}`);
          if (typeof finding.accepted !== "boolean") throw new Error("review accepted must be boolean");
          ids.add(finding.id);
        }
      }
      return;
    }
    case "validation_completed":
      assertExactKeys(event, [...baseKeys, "cleanWorktree", "commitPresent", "passed", "reviewAccepted"], event.type);
      for (const key of ["passed", "commitPresent", "cleanWorktree", "reviewAccepted"] as const) {
        if (typeof event[key] !== "boolean") throw new Error(`validation ${key} must be boolean`);
      }
      return;
    case "incident":
      assertExactKeys(event, [...baseKeys, "incident"], event.type);
      if (!INCIDENTS.includes(event.incident)) throw new Error(`unknown incident ${String(event.incident)}`);
      return;
    case "decision": {
      assertExactKeys(event, [...baseKeys, "decision", "target"], event.type);
      if (event.target !== "owner" && event.target !== "supervisor") throw new Error("unknown decision target");
      if (event.decision !== "resume" && event.decision !== "stop") throw new Error("unknown decision");
      const cursor = checkpoint.cursor;
      if (cursor.phase !== "waiting") throw new Error("decision requires waiting cursor");
      if (event.target !== cursor.target) throw new Error(`decision target ${event.target} does not match recorded target ${cursor.target}`);
      if (!cursor.allowedDecisions.includes(event.decision)) throw new Error(`decision ${event.decision} is not allowed`);
      return;
    }
  }
}

function validateOuterGroups(value: unknown, assignedCount: number, name: string): asserts value is unknown[] {
  if (!Array.isArray(value) || value.length > MAX_FINDINGS || value.length > assignedCount) {
    throw new Error(`${name} exceed assigned group count`);
  }
}

function validateCheckpoint(policy: PolicyV1, checkpoint: PolicyCheckpoint): void {
  assertExactKeys(checkpoint, ["cursor", "elapsedMs", "revision", "totalCostMicros", "totalTokens"], "checkpoint");
  for (const key of ["revision", "elapsedMs", "totalTokens", "totalCostMicros"] as const) {
    const value = checkpoint[key];
    if (!Number.isSafeInteger(value) || value < 0) throw new Error(`checkpoint ${key} must be a nonnegative safe integer`);
  }
  validateCursor(policy, checkpoint.cursor, false);
  const existingLimit = reachedLimit(policy, checkpoint);
  if (
    existingLimit !== undefined
    && checkpoint.cursor.phase !== "terminal"
    && !(checkpoint.cursor.phase === "waiting" && checkpoint.cursor.incident === "policy_limit_reached")
  ) {
    throw new Error("nonterminal checkpoint at a hard limit must be a policy-limit wait");
  }
}

function validateCursor(policy: PolicyV1, cursor: Cursor, nested: boolean): void {
  if (!isRecord(cursor) || typeof cursor.phase !== "string") throw new Error("checkpoint cursor is malformed");
  switch (cursor.phase) {
    case "worker":
      assertExactKeys(cursor, ["attempt", "phase"], "worker cursor");
      if (!Number.isSafeInteger(cursor.attempt) || cursor.attempt < 1 || cursor.attempt > policy.limits.max_worker_attempts) throw new Error("worker attempt is outside the policy bound");
      return;
    case "initial_review":
      assertExactKeys(cursor, ["phase"], "initial review cursor");
      return;
    case "validation":
      assertExactKeys(cursor, ["phase", "reviewAccepted"], "validation cursor");
      if (typeof cursor.reviewAccepted !== "boolean") throw new Error("validation reviewAccepted must be boolean");
      return;
    case "fix_batch":
    case "re_review":
      assertExactKeys(cursor, ["findings", "groups", "phase"], `${cursor.phase} cursor`);
      validateFindings(policy, cursor.findings);
      validateScopes(policy, cursor.groups, cursor.findings);
      return;
    case "waiting": {
      if (nested) throw new Error("waiting cursor cannot be nested");
      assertExactKeys(cursor, ["allowedDecisions", "incident", "phase", "resumeCursor", "target"], "waiting cursor");
      if (!INCIDENTS.includes(cursor.incident)) throw new Error("waiting incident is invalid");
      validateCursor(policy, cursor.resumeCursor, true);
      const expected = supervisionFor(policy, cursor.incident, cursor.resumeCursor, false);
      if (expected === undefined) throw new Error("unconfigured incident cannot have a waiting cursor");
      if (cursor.target !== expected.target) throw new Error("waiting target does not match policy");
      if (!sameDecisionList(cursor.allowedDecisions, expected.allowedDecisions)) throw new Error("waiting decisions do not match policy");
      return;
    }
    case "terminal":
      assertExactKeys(cursor, cursor.incident === undefined ? ["outcome", "phase"] : ["incident", "outcome", "phase"], "terminal cursor");
      if (cursor.outcome !== "ready_for_manual_integration" && cursor.outcome !== "stopped") throw new Error("terminal outcome is invalid");
      if (cursor.incident !== undefined && !INCIDENTS.includes(cursor.incident)) throw new Error("terminal incident is invalid");
      if (cursor.outcome === "ready_for_manual_integration" && cursor.incident !== undefined) throw new Error("successful terminal cursor cannot contain an incident");
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

function validateFindings(policy: PolicyV1, findings: Finding[]): void {
  if (!Array.isArray(findings) || findings.length > MAX_FINDINGS) throw new Error("checkpoint findings are invalid");
  const ids = new Set<string>();
  let priorId: string | undefined;
  for (const finding of findings) {
    assertExactKeys(finding, ["category", "groupId", "id", "rounds", "state"], "checkpoint finding");
    validateText(finding.id, "checkpoint finding id");
    validateText(finding.groupId, "checkpoint finding groupId");
    if (ids.has(finding.id) || !CATEGORIES.includes(finding.category)) throw new Error("checkpoint finding is invalid");
    if (priorId !== undefined && compareText(priorId, finding.id) >= 0) throw new Error("checkpoint findings are not canonical");
    if (!Number.isSafeInteger(finding.rounds) || finding.rounds < 0 || finding.rounds > policy.review.max_fix_rounds_per_finding) throw new Error("checkpoint finding rounds are invalid");
    if (finding.state !== "queued" && finding.state !== "active" && finding.state !== "accepted") throw new Error("checkpoint finding state is invalid");
    if ((finding.state === "active" || finding.state === "accepted") && finding.rounds < 1) {
      throw new Error("active and accepted findings require a charged round");
    }
    ids.add(finding.id);
    priorId = finding.id;
  }
}

function validateScopes(policy: PolicyV1, groups: GroupScope[], findings: Finding[]): void {
  if (!Array.isArray(groups) || groups.length > MAX_FINDINGS || groups.length > policy.review.max_parallel_fixer_groups) throw new Error("checkpoint groups are invalid");
  const activeIds = findings.filter((finding) => finding.state === "active").map((finding) => finding.id);
  if (activeIds.length === 0) throw new Error("fix and re-review cursors require at least one active finding");
  if (groups.length === 0) throw new Error("fix and re-review cursors require at least one group");
  const byId = new Map(findings.map((finding) => [finding.id, finding]));
  const scopedIds = new Set<string>();
  const groupIds = new Set<string>();
  let priorGroupId: string | undefined;
  for (const group of groups) {
    assertExactKeys(group, ["findingIds", "groupId"], "checkpoint group scope");
    validateText(group.groupId, "checkpoint groupId");
    validateTextList(group.findingIds, "checkpoint findingIds", MAX_FINDINGS);
    if (group.findingIds.length === 0 || groupIds.has(group.groupId)) throw new Error("checkpoint group scope is invalid");
    if (priorGroupId !== undefined && compareText(priorGroupId, group.groupId) >= 0) throw new Error("checkpoint groups are not canonical");
    let priorFindingId: string | undefined;
    for (const id of group.findingIds) {
      if (priorFindingId !== undefined && compareText(priorFindingId, id) >= 0) throw new Error("checkpoint scope finding IDs are not canonical");
      const finding = byId.get(id);
      if (finding === undefined || finding.groupId !== group.groupId) throw new Error("checkpoint scope group ownership is invalid");
      if (finding.state !== "active") throw new Error("checkpoint scope contains a non-active finding");
      if (scopedIds.has(id)) throw new Error("checkpoint scope contains a duplicate finding");
      scopedIds.add(id);
      priorFindingId = id;
    }
    groupIds.add(group.groupId);
    priorGroupId = group.groupId;
  }
  if (activeIds.length !== scopedIds.size || activeIds.some((id) => !scopedIds.has(id))) {
    throw new Error("checkpoint active finding scope is incomplete");
  }
}

function sameDecisionList(actual: Array<"resume" | "stop">, expected: Array<"resume" | "stop">): boolean {
  return Array.isArray(actual)
    && actual.length === expected.length
    && actual.every((decision, index) => decision === expected[index]);
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

function validateTextList(values: string[], name: string, maximum = MAX_FINDINGS): void {
  if (!Array.isArray(values) || values.length > maximum) throw new Error(`${name} must be a bounded array`);
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

function assertExactKeys(value: unknown, expected: readonly string[], name: string): asserts value is Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`${name} must be an object`);
  const actual = Object.keys(value).sort(compareText);
  const canonicalExpected = [...expected].sort(compareText);
  if (actual.length !== canonicalExpected.length || actual.some((key, index) => key !== canonicalExpected[index])) {
    throw new Error(`${name} has unknown or missing keys`);
  }
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
