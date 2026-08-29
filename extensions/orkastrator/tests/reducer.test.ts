import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { parsePolicyV1, type PolicyV1 } from "../policy.ts";
import { reducePolicy, type PolicyCheckpoint, type PolicyEvent, type PolicyReduction } from "../reducer.ts";

const policy = parsePolicyV1(readFileSync(new URL("../../../orkastrator.v1.yaml", import.meta.url)));

function checkpoint(cursor: PolicyCheckpoint["cursor"] = { phase: "worker", attempt: 1 }): PolicyCheckpoint {
  return { revision: 0, elapsedMs: 0, totalTokens: 0, totalCostMicros: 0, cursor };
}

function observation(overrides: Partial<PolicyEvent["observation"]> = {}): PolicyEvent["observation"] {
  return { elapsedMs: 1, totalTokens: 1, totalCostMicros: 1, ...overrides };
}

type BareEvent = PolicyEvent extends infer Event
  ? Event extends PolicyEvent ? Omit<Event, "sequence" | "observation"> : never
  : never;

function reduce(cursor: PolicyCheckpoint["cursor"], event: BareEvent, selectedPolicy = policy): PolicyReduction {
  return reducePolicy({
    policy: selectedPolicy,
    checkpoint: checkpoint(cursor),
    event: { ...event, sequence: 1, observation: observation() } as PolicyEvent,
  });
}

function mutablePolicy(): PolicyV1 {
  return structuredClone(policy) as PolicyV1;
}

function initialReview(findings: Array<{ id: string; category: "correctness" | "security" | "data_loss" | "scope" | "acceptance"; groupId: string }>, selectedPolicy = policy): PolicyReduction {
  return reduce({ phase: "initial_review" }, { type: "initial_review_completed", findings }, selectedPolicy);
}

function completeFix(reduction: PolicyReduction, scopePreserved = true): PolicyReduction {
  assert.equal(reduction.checkpoint.cursor.phase, "fix_batch");
  const groups = reduction.checkpoint.cursor.groups.map((group) => ({ ...structuredClone(group), scopePreserved }));
  return reducePolicy({
    policy,
    checkpoint: reduction.checkpoint,
    event: { type: "fix_batch_completed", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), groups },
  });
}

function completeReReview(reduction: PolicyReduction, accepted: boolean): PolicyReduction {
  assert.equal(reduction.checkpoint.cursor.phase, "re_review");
  return reducePolicy({
    policy,
    checkpoint: reduction.checkpoint,
    event: {
      type: "re_review_completed",
      sequence: reduction.checkpoint.revision + 1,
      observation: observation({
        elapsedMs: reduction.checkpoint.elapsedMs + 1,
        totalTokens: reduction.checkpoint.totalTokens + 1,
        totalCostMicros: reduction.checkpoint.totalCostMicros + 1,
      }),
      groups: reduction.checkpoint.cursor.groups.map((group) => ({
        groupId: group.groupId,
        findings: group.findingIds.map((id) => ({ id, accepted })),
      })),
    },
  });
}

test("is deterministic, does not mutate inputs, and emits one identified action", () => {
  const inputPolicy = structuredClone(policy) as PolicyV1;
  const inputCheckpoint = checkpoint();
  const event: PolicyEvent = { type: "worker_completed", sequence: 1, observation: observation() };
  const before = structuredClone({ inputPolicy, inputCheckpoint, event });

  const left = reducePolicy({ policy: inputPolicy, checkpoint: inputCheckpoint, event });
  const right = reducePolicy({ policy: inputPolicy, checkpoint: inputCheckpoint, event });

  assert.deepEqual(left, right);
  assert.deepEqual({ inputPolicy, inputCheckpoint, event }, before);
  assert.equal(left.checkpoint.revision, 1);
  assert.equal(left.occurrenceId, "1:worker.completed");
  assert.equal(left.action.actionId, `${left.occurrenceId}:action`);
  assert.equal(Array.isArray((left as unknown as { actions?: unknown }).actions), false);
  assert.ok(left.trace.length > 0);
});

test("every role action consumes the exact configured role settings", () => {
  const worker = reduce({ phase: "worker", attempt: 1 }, { type: "worker_retryable_failure" });
  assert.equal(worker.action.type, "run_worker");
  if (worker.action.type === "run_worker") assert.deepEqual(worker.action.role, policy.roles.worker);

  const reviewer = reduce({ phase: "worker", attempt: 1 }, { type: "worker_completed" });
  assert.equal(reviewer.action.type, "run_initial_review");
  if (reviewer.action.type === "run_initial_review") assert.deepEqual(reviewer.action.role, policy.roles.initial_reviewer);

  const fixer = initialReview([{ id: "F-1", category: "correctness", groupId: "g" }]);
  assert.equal(fixer.action.type, "run_fixers");
  if (fixer.action.type === "run_fixers") assert.deepEqual(fixer.action.role, policy.roles.fixer);

  const reReviewer = completeFix(fixer);
  assert.equal(reReviewer.action.type, "run_re_review");
  if (reReviewer.action.type === "run_re_review") assert.deepEqual(reReviewer.action.role, policy.roles.re_reviewer);
});

test("worker retries only below max_worker_attempts and limit precedence wins over the attempt bound", () => {
  const retry = reduce({ phase: "worker", attempt: 1 }, { type: "worker_retryable_failure" });
  assert.equal(retry.action.type, "run_worker");
  if (retry.action.type === "run_worker") assert.equal(retry.action.attempt, 2);

  const bounded = reduce({ phase: "worker", attempt: policy.limits.max_worker_attempts }, { type: "worker_retryable_failure" });
  assert.equal(bounded.ruleId, "worker.attempt_bound");
  assert.equal(bounded.action.type, "wait");

  const atWallLimit = reducePolicy({
    policy,
    checkpoint: checkpoint({ phase: "worker", attempt: policy.limits.max_worker_attempts }),
    event: {
      type: "worker_retryable_failure",
      sequence: 1,
      observation: observation({ elapsedMs: policy.limits.wall_clock_minutes * 60_000 }),
    },
  });
  assert.equal(atWallLimit.ruleId, "limit.wall_clock");
  assert.equal(atWallLimit.action.type, "wait");
  if (atWallLimit.action.type === "wait") assert.deepEqual(atWallLimit.action.allowedDecisions, ["stop"]);
});

test("wall-clock, token, then cost limits trigger at equality in fixed precedence", () => {
  const cases: Array<[Partial<PolicyEvent["observation"]>, string]> = [
    [{ elapsedMs: policy.limits.wall_clock_minutes * 60_000, totalTokens: policy.limits.total_tokens, totalCostMicros: policy.limits.total_cost_usd * 1_000_000 }, "limit.wall_clock"],
    [{ totalTokens: policy.limits.total_tokens, totalCostMicros: policy.limits.total_cost_usd * 1_000_000 }, "limit.tokens"],
    [{ totalCostMicros: policy.limits.total_cost_usd * 1_000_000 }, "limit.cost"],
  ];
  for (const [totals, ruleId] of cases) {
    const result = reducePolicy({
      policy,
      checkpoint: checkpoint(),
      event: { type: "worker_completed", sequence: 1, observation: observation(totals) },
    });
    assert.equal(result.ruleId, ruleId);
    assert.equal(result.action.type, "wait");
    if (result.action.type === "wait") assert.deepEqual(result.action.allowedDecisions, ["stop"]);
  }
});

test("findings are unique, filtered, sorted into deterministic capped groups, and charged on dispatch", () => {
  const selected = mutablePolicy() as unknown as { review: { blocking: string[]; max_fix_rounds_per_finding: number; max_parallel_fixer_groups: number } };
  selected.review.blocking = ["correctness"];
  selected.review.max_parallel_fixer_groups = 2;
  const result = initialReview([
    { id: "F-4", category: "correctness", groupId: "z" },
    { id: "F-3", category: "security", groupId: "a" },
    { id: "F-2", category: "correctness", groupId: "b" },
    { id: "F-1", category: "correctness", groupId: "a" },
  ], selected as unknown as PolicyV1);
  assert.equal(result.action.type, "run_fixers");
  if (result.action.type !== "run_fixers" || result.checkpoint.cursor.phase !== "fix_batch") return;
  assert.deepEqual(result.action.groups, [
    { groupId: "a", findings: [{ id: "F-1", round: 1 }] },
    { groupId: "b", findings: [{ id: "F-2", round: 1 }] },
  ]);
  assert.deepEqual(result.checkpoint.cursor.findings.map((finding) => [finding.id, finding.rounds, finding.state]), [
    ["F-1", 1, "active"],
    ["F-2", 1, "active"],
    ["F-4", 0, "queued"],
  ]);
  assert.throws(() => initialReview([
    { id: "same", category: "correctness", groupId: "a" },
    { id: "same", category: "security", groupId: "b" },
  ]), /duplicate finding id/u);
});

test("fix completion validates exact scope and requires scoped re-review", () => {
  const fixer = initialReview([{ id: "F-1", category: "scope", groupId: "g" }]);
  assert.equal(fixer.checkpoint.cursor.phase, "fix_batch");
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: fixer.checkpoint,
    event: {
      type: "fix_batch_completed",
      sequence: 2,
      observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }),
      groups: [{ groupId: "g", findingIds: ["wrong"], scopePreserved: true }],
    },
  }), /scope does not match/u);

  const escaped = completeFix(fixer, false);
  assert.equal(escaped.ruleId, "fix.scope_escape");
  assert.equal(escaped.action.type, "wait");

  const reviewed = completeFix(fixer, true);
  assert.equal(reviewed.action.type, "run_re_review");
  if (reviewed.action.type === "run_re_review") assert.deepEqual(reviewed.action.groups, [{ groupId: "g", findingIds: ["F-1"] }]);
});

test("later groups wait for an accepted batch and rejected findings consume bounded rounds", () => {
  const selected = mutablePolicy() as unknown as { review: { max_parallel_fixer_groups: number; max_fix_rounds_per_finding: number } };
  selected.review.max_parallel_fixer_groups = 1;
  selected.review.max_fix_rounds_per_finding = 2;
  const first = initialReview([
    { id: "F-2", category: "correctness", groupId: "b" },
    { id: "F-1", category: "correctness", groupId: "a" },
  ], selected as unknown as PolicyV1);
  assert.equal(first.action.type, "run_fixers");
  if (first.action.type === "run_fixers") assert.equal(first.action.groups[0]?.groupId, "a");

  assert.equal(first.checkpoint.cursor.phase, "fix_batch");
  const firstReview = reducePolicy({
    policy: selected as unknown as PolicyV1,
    checkpoint: first.checkpoint,
    event: {
      type: "fix_batch_completed",
      sequence: 2,
      observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }),
      groups: [{ groupId: "a", findingIds: ["F-1"], scopePreserved: true }],
    },
  });
  const retry = reducePolicy({
    policy: selected as unknown as PolicyV1,
    checkpoint: firstReview.checkpoint,
    event: {
      type: "re_review_completed",
      sequence: 3,
      observation: observation({ elapsedMs: 3, totalTokens: 3, totalCostMicros: 3 }),
      groups: [{ groupId: "a", findings: [{ id: "F-1", accepted: false }] }],
    },
  });
  assert.equal(retry.action.type, "run_fixers");
  if (retry.action.type === "run_fixers") assert.deepEqual(retry.action.groups, [{ groupId: "a", findings: [{ id: "F-1", round: 2 }] }]);

  assert.equal(retry.checkpoint.cursor.phase, "fix_batch");
  const retryReview = reducePolicy({
    policy: selected as unknown as PolicyV1,
    checkpoint: retry.checkpoint,
    event: {
      type: "fix_batch_completed",
      sequence: 4,
      observation: observation({ elapsedMs: 4, totalTokens: 4, totalCostMicros: 4 }),
      groups: [{ groupId: "a", findingIds: ["F-1"], scopePreserved: true }],
    },
  });
  const exhausted = reducePolicy({
    policy: selected as unknown as PolicyV1,
    checkpoint: retryReview.checkpoint,
    event: {
      type: "re_review_completed",
      sequence: 5,
      observation: observation({ elapsedMs: 5, totalTokens: 5, totalCostMicros: 5 }),
      groups: [{ groupId: "a", findings: [{ id: "F-1", accepted: false }] }],
    },
  });
  assert.equal(exhausted.ruleId, "review.round_bound");

  const accepted = reducePolicy({
    policy: selected as unknown as PolicyV1,
    checkpoint: firstReview.checkpoint,
    event: {
      type: "re_review_completed",
      sequence: 3,
      observation: observation({ elapsedMs: 3, totalTokens: 3, totalCostMicros: 3 }),
      groups: [{ groupId: "a", findings: [{ id: "F-1", accepted: true }] }],
    },
  });
  assert.equal(accepted.action.type, "run_fixers");
  if (accepted.action.type === "run_fixers") assert.equal(accepted.action.groups[0]?.groupId, "b");
});

test("validation action carries profile and completion requirements", () => {
  const action = initialReview([]);
  assert.equal(action.action.type, "run_validation");
  if (action.action.type === "run_validation") {
    assert.equal(action.action.profile, policy.validation.profile);
    assert.deepEqual(action.action.requirements, { commit: true, cleanWorktree: true, reviewAcceptance: true });
  }

  const failure = reduce({ phase: "validation", reviewAccepted: true }, {
    type: "validation_completed", passed: false, commitPresent: true, cleanWorktree: true, reviewAccepted: true,
  });
  assert.equal(failure.ruleId, "validation.failed");

  for (const missing of ["commitPresent", "cleanWorktree", "reviewAccepted"] as const) {
    const evidence = { passed: true, commitPresent: true, cleanWorktree: true, reviewAccepted: true };
    evidence[missing] = false;
    const result = reduce({ phase: "validation", reviewAccepted: true }, { type: "validation_completed", ...evidence });
    assert.equal(result.ruleId, "validation.cleanup_unverified");
  }

  const success = reduce({ phase: "validation", reviewAccepted: true }, {
    type: "validation_completed", passed: true, commitPresent: true, cleanWorktree: true, reviewAccepted: true,
  });
  assert.equal(success.action.type, "outcome");
  if (success.action.type === "outcome") assert.equal(success.action.outcome, "ready_for_manual_integration");
  assert.deepEqual(success.checkpoint.cursor, { phase: "terminal", outcome: "ready_for_manual_integration" });
});

test("every wake_on literal waits for supervisor and every require_human_on literal waits for owner", () => {
  for (const incident of policy.supervision.wake_on) {
    const result = reduce({ phase: "worker", attempt: 1 }, { type: "incident", incident });
    assert.equal(result.action.type, "wait");
    if (result.action.type === "wait") {
      assert.equal(result.action.target, "supervisor");
      assert.deepEqual(result.action.allowedDecisions, incident === "policy_limit_reached" ? ["stop"] : ["resume", "stop"]);
    }
  }
  for (const incident of policy.supervision.require_human_on) {
    const result = reduce({ phase: "worker", attempt: 1 }, { type: "incident", incident });
    assert.equal(result.action.type, "wait");
    if (result.action.type === "wait") assert.equal(result.action.target, "owner");
  }
});

test("require_human_on wins over wake_on and unconfigured incidents fail closed", () => {
  const overlap = mutablePolicy() as unknown as { supervision: { wake_on: string[]; require_human_on: string[] } };
  overlap.supervision.wake_on = ["worker_blocked"];
  overlap.supervision.require_human_on = ["worker_blocked"];
  const owner = reduce({ phase: "worker", attempt: 1 }, { type: "incident", incident: "worker_blocked" }, overlap as unknown as PolicyV1);
  assert.equal(owner.action.type, "wait");
  if (owner.action.type === "wait") assert.equal(owner.action.target, "owner");

  const absent = mutablePolicy() as unknown as { supervision: { wake_on: string[]; require_human_on: string[] } };
  absent.supervision.wake_on = ["identity_mismatch"];
  absent.supervision.require_human_on = ["policy_override"];
  const stopped = reduce({ phase: "worker", attempt: 1 }, { type: "incident", incident: "worker_blocked" }, absent as unknown as PolicyV1);
  assert.equal(stopped.action.type, "outcome");
  if (stopped.action.type === "outcome") assert.equal(stopped.action.outcome, "stopped");
  assert.match(stopped.ruleId, /fail_closed$/u);
});

test("recorded wait target gates resume exact work and stop", () => {
  const waiting = reduce({ phase: "worker", attempt: 1 }, { type: "worker_blocked" });
  assert.equal(waiting.checkpoint.cursor.phase, "waiting");
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: waiting.checkpoint,
    event: { type: "decision", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), target: "owner", decision: "resume" },
  }), /does not match recorded target/u);

  const resumed = reducePolicy({
    policy,
    checkpoint: waiting.checkpoint,
    event: { type: "decision", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), target: "supervisor", decision: "resume" },
  });
  assert.deepEqual(resumed.checkpoint.cursor, { phase: "worker", attempt: 1 });
  assert.equal(resumed.action.type, "run_worker");
  if (resumed.action.type === "run_worker") assert.equal(resumed.action.attempt, 1);

  const hard = reducePolicy({
    policy,
    checkpoint: checkpoint(),
    event: { type: "worker_completed", sequence: 1, observation: observation({ totalTokens: policy.limits.total_tokens }) },
  });
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: hard.checkpoint,
    event: { type: "decision", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: policy.limits.total_tokens, totalCostMicros: 2 }), target: "supervisor", decision: "resume" },
  }), /not allowed/u);
  const stopped = reducePolicy({
    policy,
    checkpoint: waiting.checkpoint,
    event: { type: "decision", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), target: "supervisor", decision: "stop" },
  });
  assert.deepEqual(stopped.checkpoint.cursor, { phase: "terminal", outcome: "stopped", incident: "worker_blocked" });
});

test("sequence, legality, monotonic totals, non-finite totals, and terminal inputs are rejected", () => {
  const valid: PolicyEvent = { type: "worker_completed", sequence: 1, observation: observation() };
  assert.throws(() => reducePolicy({ policy, checkpoint: checkpoint(), event: { ...valid, sequence: 2 } }), /sequence must equal 1/u);
  assert.throws(() => reducePolicy({ policy, checkpoint: checkpoint({ phase: "validation", reviewAccepted: true }), event: valid }), /illegal in phase validation/u);

  const prior: PolicyCheckpoint = { revision: 3, elapsedMs: 10, totalTokens: 10, totalCostMicros: 10, cursor: { phase: "worker", attempt: 1 } };
  for (const [field, value] of [
    ["elapsedMs", 9],
    ["totalTokens", Number.NaN],
    ["totalCostMicros", Number.POSITIVE_INFINITY],
  ] as const) {
    assert.throws(() => reducePolicy({
      policy,
      checkpoint: prior,
      event: { type: "worker_completed", sequence: 4, observation: { elapsedMs: 10, totalTokens: 10, totalCostMicros: 10, [field]: value } },
    }), /observation/u);
  }
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: checkpoint({ phase: "terminal", outcome: "stopped" }),
    event: valid,
  }), /terminal checkpoint rejects all events/u);
});

test("decisions newly reaching each hard limit replace supervisor and owner waits with one stop-only limit action", () => {
  const waits = [
    reduce({ phase: "worker", attempt: 1 }, { type: "worker_blocked" }),
    reduce({ phase: "worker", attempt: 1 }, { type: "incident", incident: "policy_override" }),
  ];
  const limits: Array<[PolicyEvent["observation"], string]> = [
    [observation({ elapsedMs: policy.limits.wall_clock_minutes * 60_000 }), "limit.wall_clock"],
    [observation({ totalTokens: policy.limits.total_tokens }), "limit.tokens"],
    [observation({ totalCostMicros: Math.round(policy.limits.total_cost_usd * 1_000_000) }), "limit.cost"],
  ];

  for (const waiting of waits) {
    assert.equal(waiting.checkpoint.cursor.phase, "waiting");
    const sourceTarget = waiting.checkpoint.cursor.target;
    for (const [totals, ruleId] of limits) {
      const result = reducePolicy({
        policy,
        checkpoint: waiting.checkpoint,
        event: {
          type: "decision",
          sequence: waiting.checkpoint.revision + 1,
          observation: totals,
          target: sourceTarget,
          decision: "resume",
        },
      });
      assert.equal(result.ruleId, ruleId);
      assert.equal(result.action.type, "wait");
      if (result.action.type !== "wait" || result.checkpoint.cursor.phase !== "waiting") continue;
      assert.equal(result.action.incident, "policy_limit_reached");
      assert.deepEqual(result.action.allowedDecisions, ["stop"]);
      assert.deepEqual(result.checkpoint.cursor.allowedDecisions, ["stop"]);
      assert.deepEqual(result.checkpoint.cursor.resumeCursor, { phase: "worker", attempt: 1 });
      assert.equal("resumeAction" in result.checkpoint.cursor, false);
    }
  }
});

test("waiting checkpoints derive target and exact decisions from policy and contain only a validated resume cursor", () => {
  const waiting = reduce({ phase: "worker", attempt: 1 }, { type: "worker_blocked" });
  assert.equal(waiting.checkpoint.cursor.phase, "waiting");
  const variants: Array<[string, (value: Record<string, unknown>) => void]> = [
    ["target", (cursor) => { cursor.target = "owner"; }],
    ["decisions", (cursor) => { cursor.allowedDecisions = ["stop", "resume"]; }],
    ["duplicate decisions", (cursor) => { cursor.allowedDecisions = ["resume", "stop", "stop"]; }],
    ["legacy resume action", (cursor) => { cursor.resumeAction = { type: "run_validation" }; }],
    ["resume work", (cursor) => { cursor.resumeCursor = { phase: "worker", attempt: policy.limits.max_worker_attempts + 1 }; }],
  ];
  for (const [name, mutate] of variants) {
    const forged = structuredClone(waiting.checkpoint) as unknown as { cursor: Record<string, unknown> };
    mutate(forged.cursor);
    assert.throws(() => reducePolicy({
      policy,
      checkpoint: forged as unknown as PolicyCheckpoint,
      event: {
        type: "decision",
        sequence: 2,
        observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }),
        target: "supervisor",
        decision: "stop",
      },
    }), (error: unknown) => error instanceof Error, name);
  }
});

test("attempt-bound waits are stop-only and cannot resume into run_worker", () => {
  const bounded = reduce({ phase: "worker", attempt: policy.limits.max_worker_attempts }, { type: "worker_retryable_failure" });
  assert.equal(bounded.checkpoint.cursor.phase, "waiting");
  if (bounded.checkpoint.cursor.phase !== "waiting") return;
  assert.deepEqual(bounded.checkpoint.cursor.allowedDecisions, ["stop"]);
  assert.equal("resumeAction" in bounded.checkpoint.cursor, false);
  const boundedTarget = bounded.checkpoint.cursor.target;
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: bounded.checkpoint,
    event: {
      type: "decision",
      sequence: 2,
      observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }),
      target: boundedTarget,
      decision: "resume",
    },
  }), /not allowed/u);
});

test("event, observation, completion, group, verdict, and finding inputs reject unknown nested keys", () => {
  const top = { type: "worker_completed", sequence: 1, observation: observation(), extra: true } as unknown as PolicyEvent;
  assert.throws(() => reducePolicy({ policy, checkpoint: checkpoint(), event: top }), /unknown or missing keys/u);

  const nestedObservation = {
    type: "worker_completed",
    sequence: 1,
    observation: { ...observation(), extra: true },
  } as unknown as PolicyEvent;
  assert.throws(() => reducePolicy({ policy, checkpoint: checkpoint(), event: nestedObservation }), /observation has unknown/u);

  const findingAlias = { evidence: { injected: true } };
  const findingEvent = {
    type: "initial_review_completed",
    sequence: 1,
    observation: observation(),
    findings: [{ id: "F-1", category: "correctness", groupId: "g", alias: findingAlias }],
  } as unknown as PolicyEvent;
  assert.throws(() => reducePolicy({ policy, checkpoint: checkpoint({ phase: "initial_review" }), event: findingEvent }), /finding has unknown/u);

  const validationEvent = {
    type: "validation_completed",
    sequence: 1,
    observation: observation(),
    passed: true,
    commitPresent: true,
    cleanWorktree: true,
    reviewAccepted: true,
    completion: { override: true },
  } as unknown as PolicyEvent;
  assert.throws(() => reducePolicy({ policy, checkpoint: checkpoint({ phase: "validation", reviewAccepted: true }), event: validationEvent }), /unknown or missing keys/u);

  const fixer = initialReview([{ id: "F-1", category: "correctness", groupId: "g" }]);
  const fixGroup = {
    type: "fix_batch_completed",
    sequence: 2,
    observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }),
    groups: [{ groupId: "g", findingIds: ["F-1"], scopePreserved: true, command: "inject" }],
  } as unknown as PolicyEvent;
  assert.throws(() => reducePolicy({ policy, checkpoint: fixer.checkpoint, event: fixGroup }), /fix group has unknown/u);

  const review = completeFix(fixer);
  const verdict = {
    type: "re_review_completed",
    sequence: 3,
    observation: observation({ elapsedMs: 3, totalTokens: 3, totalCostMicros: 3 }),
    groups: [{ groupId: "g", findings: [{ id: "F-1", accepted: true, rationale: { injected: true } }] }],
  } as unknown as PolicyEvent;
  assert.throws(() => reducePolicy({ policy, checkpoint: review.checkpoint, event: verdict }), /review verdict has unknown/u);
});

test("checkpoint, cursor, work finding, scope, and wait structures reject unknown keys without retaining aliases", () => {
  const cases: PolicyCheckpoint[] = [
    { ...checkpoint(), extra: true } as unknown as PolicyCheckpoint,
    checkpoint({ phase: "worker", attempt: 1, extra: true } as unknown as PolicyCheckpoint["cursor"]),
    checkpoint({ phase: "validation", reviewAccepted: true, proof: {} } as unknown as PolicyCheckpoint["cursor"]),
  ];
  for (const malformed of cases) {
    assert.throws(() => reducePolicy({
      policy,
      checkpoint: malformed,
      event: { type: "worker_completed", sequence: 1, observation: observation() },
    }), /unknown or missing keys/u);
  }

  const fixer = initialReview([{ id: "F-1", category: "correctness", groupId: "g" }]);
  assert.equal(fixer.checkpoint.cursor.phase, "fix_batch");
  if (fixer.checkpoint.cursor.phase !== "fix_batch") return;
  const findingExtra = structuredClone(fixer.checkpoint) as unknown as { cursor: { findings: Array<Record<string, unknown>> } };
  findingExtra.cursor.findings[0]!.nested = { injected: true };
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: findingExtra as unknown as PolicyCheckpoint,
    event: { type: "fix_batch_completed", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), groups: [] },
  }), /checkpoint finding has unknown/u);

  const scopeExtra = structuredClone(fixer.checkpoint) as unknown as { cursor: { groups: Array<Record<string, unknown>> } };
  scopeExtra.cursor.groups[0]!.nested = { injected: true };
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: scopeExtra as unknown as PolicyCheckpoint,
    event: { type: "fix_batch_completed", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), groups: [] },
  }), /group scope has unknown/u);

  const waiting = reduce({ phase: "worker", attempt: 1 }, { type: "worker_blocked" });
  const waitExtra = structuredClone(waiting.checkpoint) as unknown as { cursor: Record<string, unknown> };
  waitExtra.cursor.nested = { injected: true };
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: waitExtra as unknown as PolicyCheckpoint,
    event: { type: "decision", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), target: "supervisor", decision: "stop" },
  }), /waiting cursor has unknown/u);

  const sourceIds = fixer.checkpoint.cursor.groups[0]!.findingIds;
  const fixed = completeFix(fixer);
  assert.equal(fixed.checkpoint.cursor.phase, "re_review");
  if (fixed.checkpoint.cursor.phase === "re_review") {
    assert.notEqual(fixed.checkpoint.cursor.groups[0]!.findingIds, sourceIds);
  }
});

test("phase scope invariants reject wrong ownership, cross-group duplicates, state mismatch, and omitted active findings", () => {
  const base = initialReview([
    { id: "F-1", category: "correctness", groupId: "a" },
    { id: "F-2", category: "correctness", groupId: "b" },
  ]).checkpoint;
  assert.equal(base.cursor.phase, "fix_batch");
  if (base.cursor.phase !== "fix_batch") return;

  const mutations: Array<[string, (value: Extract<PolicyCheckpoint["cursor"], { phase: "fix_batch" }>) => void]> = [
    ["wrong group", (cursor) => { cursor.groups[0]!.findingIds = ["F-2"]; }],
    ["cross group duplicate", (cursor) => { cursor.groups[1]!.findingIds = ["F-1"]; }],
    ["accepted in active scope", (cursor) => { cursor.findings[0]!.state = "accepted"; }],
    ["queued in active scope", (cursor) => { cursor.findings[0]!.state = "queued"; }],
    ["omitted active", (cursor) => { cursor.groups[0]!.findingIds = []; }],
  ];
  for (const [name, mutate] of mutations) {
    const forged = structuredClone(base);
    assert.equal(forged.cursor.phase, "fix_batch");
    if (forged.cursor.phase !== "fix_batch") continue;
    mutate(forged.cursor);
    assert.throws(() => reducePolicy({
      policy,
      checkpoint: forged,
      event: { type: "fix_batch_completed", sequence: 2, observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }), groups: [] },
    }), (error: unknown) => error instanceof Error, name);
  }
});

test("fix and re-review outer group arrays are bounded before nested traversal", () => {
  const fixer = initialReview([{ id: "F-1", category: "correctness", groupId: "g" }]);
  const oversizedFixGroups = Array.from({ length: 1_001 }, () => null) as unknown as Extract<PolicyEvent, { type: "fix_batch_completed" }>["groups"];
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: fixer.checkpoint,
    event: {
      type: "fix_batch_completed",
      sequence: 2,
      observation: observation({ elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 }),
      groups: oversizedFixGroups,
    },
  }), /exceed assigned group count/u);

  const review = completeFix(fixer);
  const oversizedReviewGroups = Array.from({ length: 1_001 }, () => null) as unknown as Extract<PolicyEvent, { type: "re_review_completed" }>["groups"];
  assert.throws(() => reducePolicy({
    policy,
    checkpoint: review.checkpoint,
    event: {
      type: "re_review_completed",
      sequence: 3,
      observation: observation({ elapsedMs: 3, totalTokens: 3, totalCostMicros: 3 }),
      groups: oversizedReviewGroups,
    },
  }), /exceed assigned group count/u);
});
