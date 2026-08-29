import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { appendFileSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

import { LedgerCorruptionError, RunLedger } from "../ledger/file-ledger.ts";
import type { JsonValue } from "../ledger/types.ts";
import type { PolicyEvent } from "../reducer.ts";
import { POLICY_SNAPSHOT } from "./policy-fixture.ts";

function ids(start = 0): () => string {
  let value = start;
  return () => {
    value += 1;
    return `00000000-0000-4000-8000-${value.toString(16).padStart(12, "0")}`;
  };
}

function fixture(randomId = ids()) {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-policy-ledger-"));
  const repository = join(temporary, "repository");
  mkdirSync(repository);
  const ledger = new RunLedger({
    root: join(temporary, "state"),
    now: () => new Date("2026-08-28T22:00:00.000Z"),
    randomId,
  });
  const run = ledger.createRun({
    objective: "Prove append-first policy persistence.",
    supervisorSessionId: "session-1",
    repositoryRoot: repository,
    policySnapshot: POLICY_SNAPSHOT,
    hostPid: 4242,
  });
  return { temporary, repository, ledger, run, cleanup: () => rmSync(temporary, { recursive: true, force: true }) };
}

function handcraftedLegacyLedger() {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-legacy-policy-ledger-"));
  const repository = join(temporary, "repository");
  const root = join(temporary, "state");
  const runId = "00000000-0000-4000-8000-000000000001";
  const timestamp = "2026-08-28T22:00:00.000Z";
  const policySnapshot = "version: 0\nmode: historical-lifecycle\n";
  mkdirSync(repository);
  const ledger = new RunLedger({ root });
  const directory = join(root, runId);
  mkdirSync(directory);
  const record = {
    schemaVersion: 1,
    runId,
    objective: "Inspect a historical run.",
    supervisorSessionId: "legacy-session",
    repositoryRoot: repository,
    policyHash: createHash("sha256").update(policySnapshot).digest("hex"),
    policyFile: "policy.yaml",
    generation: 1,
    hostPid: 4242,
    sequence: 1,
    state: "submitted",
    reason: "run_created",
    createdAt: timestamp,
    updatedAt: timestamp,
    ownedProcesses: [],
    worktrees: [],
  } as const;
  const event = {
    schemaVersion: 1,
    eventId: "00000000-0000-4000-8000-000000000002",
    runId,
    sequence: 1,
    timestamp,
    type: "run_created",
    toState: "submitted",
    ruleId: "run.create",
    actor: "owner",
    evidence: { repositoryRoot: repository, policyHash: record.policyHash },
    projection: record,
  };
  writeFileSync(join(directory, "policy.yaml"), policySnapshot);
  writeFileSync(join(directory, "events.jsonl"), `${JSON.stringify(event)}\n`);
  writeFileSync(join(directory, "state.json"), `${JSON.stringify(record, null, 2)}\n`);
  return {
    temporary,
    ledger,
    runId,
    policySnapshot,
    cleanup: () => rmSync(temporary, { recursive: true, force: true }),
  };
}

function started(): Extract<PolicyEvent, { type: "started" }> {
  return { type: "started", sequence: 1, observation: { elapsedMs: 0, totalTokens: 0, totalCostMicros: 0 } };
}

function workerCompleted(): Extract<PolicyEvent, { type: "worker_completed" }> {
  return { type: "worker_completed", sequence: 2, observation: { elapsedMs: 1, totalTokens: 1, totalCostMicros: 1 } };
}

function delivery(actionId: string, receipt: JsonValue = { externalId: "receipt-1" }) {
  return { adapter: "fake-idempotent", idempotencyKey: actionId, receipt };
}

function rewriteEvents(
  ledger: RunLedger,
  runId: string,
  mutate: (events: Array<Record<string, any>>) => void,
): void {
  const path = join(ledger.runDirectory(runId), "events.jsonl");
  const events = readFileSync(path, "utf8").trimEnd().split("\n").map((line) => JSON.parse(line));
  mutate(events);
  writeFileSync(path, `${events.map((event) => JSON.stringify(event)).join("\n")}\n`);
}

test("new records explicitly start with a null policy checkpoint", () => {
  const value = fixture();
  try {
    assert.equal(Object.hasOwn(value.run, "policyCheckpoint"), true);
    assert.equal(value.run.policyCheckpoint, null);
  } finally {
    value.cleanup();
  }
});

test("a handcrafted pre-stage run hash-verifies but does not parse its non-v1 policy", () => {
  const value = handcraftedLegacyLedger();
  try {
    const legacy = value.ledger.loadRun(value.runId).record;
    assert.equal(Object.hasOwn(legacy, "policyCheckpoint"), false);
    assert.equal(value.ledger.policySnapshot(value.runId), value.policySnapshot);
    const cleaned = value.ledger.transition(value.runId, {
      state: "interrupted",
      reason: "legacy_cleanup",
      ruleId: "legacy.cleanup",
      actor: "extension",
    });
    assert.equal(cleaned.state, "interrupted");
    assert.equal(Object.hasOwn(cleaned, "policyCheckpoint"), false);
    assert.throws(
      () => value.ledger.applyPolicyEvent(value.runId, started()),
      /legacy run .* cannot apply policy events/u,
    );

    writeFileSync(
      join(value.ledger.runDirectory(value.runId), "policy.yaml"),
      `${value.policySnapshot}tampered: true\n`,
    );
    assert.throws(
      () => value.ledger.loadRun(value.runId),
      /policy snapshot hash does not match/u,
    );
  } finally {
    value.cleanup();
  }
});

test("an invalid exact policy creates no run, event, state, lock, or allocated ID", () => {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-invalid-policy-"));
  const repository = join(temporary, "repository");
  const root = join(temporary, "state");
  mkdirSync(repository);
  let allocations = 0;
  const ledger = new RunLedger({
    root,
    randomId: () => {
      allocations += 1;
      return "00000000-0000-4000-8000-000000000001";
    },
  });
  try {
    assert.throws(() => ledger.createRun({
      objective: "invalid",
      supervisorSessionId: "session",
      repositoryRoot: repository,
      policySnapshot: "version: 1\n",
      hostPid: 4242,
    }), /Policy /u);
    assert.equal(allocations, 0);
    assert.deepEqual(readdirSync(root), []);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("the first reduction is exact, append-first, pending, and durable across reload", () => {
  const value = fixture();
  try {
    const result = value.ledger.applyPolicyEvent(value.run.runId, started());
    assert.equal(result.appended, true);
    assert.equal(result.delivery, "pending");
    assert.deepEqual(result.reduction, {
      checkpoint: { revision: 1, elapsedMs: 0, totalTokens: 0, totalCostMicros: 0, cursor: { phase: "worker", attempt: 1 } },
      action: {
        actionId: "1:worker.started:action",
        type: "run_worker",
        attempt: 1,
        role: { model: "openai-codex/gpt-5.6-sol", thinking: "medium", fast: true },
      },
      ruleId: "worker.started",
      occurrenceId: "1:worker.started",
      trace: ["event.legal", "observation.valid", "worker.started"],
    });
    assert.deepEqual(result.record.pendingPolicyAction, result.reduction.action);
    assert.deepEqual(result.record.policyCheckpoint, result.reduction.checkpoint);
    assert.equal(result.record.state, value.run.state);
    const applied = value.ledger.events(value.run.runId).at(-1)!;
    assert.equal(applied.type, "policy_event_applied");
    assert.equal(applied.actor, "extension");
    assert.equal(applied.ruleId, "worker.started");
    assert.deepEqual(Object.keys(applied.evidence).sort(), ["action", "occurrenceId", "policyEvent", "trace"]);

    const reloaded = new RunLedger({ root: value.ledger.root }).loadRun(value.run.runId).record;
    assert.deepEqual(reloaded.pendingPolicyAction, result.reduction.action);
    assert.deepEqual(reloaded.policyCheckpoint, result.reduction.checkpoint);
  } finally {
    value.cleanup();
  }
});

test("delivery removes only the outbox slot and retains permanent exact ledger evidence", () => {
  const value = fixture();
  try {
    const applied = value.ledger.applyPolicyEvent(value.run.runId, started());
    const actionId = applied.reduction.action.actionId;
    const evidence = delivery(actionId);
    const acknowledged = value.ledger.recordPolicyActionDelivered(value.run.runId, actionId, evidence);
    assert.equal(acknowledged.pendingPolicyAction, undefined);
    assert.deepEqual(acknowledged.policyCheckpoint, applied.reduction.checkpoint);
    assert.equal(acknowledged.state, applied.record.state);

    const event = value.ledger.events(value.run.runId).at(-1)!;
    assert.equal(event.type, "policy_action_delivered");
    assert.deepEqual(event.evidence, evidence);
    assert.deepEqual(
      { actionId: event.actionId, policyRevision: event.policyRevision, actionType: event.actionType, delivery: event.delivery },
      { actionId, policyRevision: 1, actionType: "run_worker", delivery: "delivered" },
    );
  } finally {
    value.cleanup();
  }
});

test("duplicate apply and delivery are idempotent while conflicts, pending, stale, and skipped inputs append nothing", () => {
  const value = fixture();
  try {
    const first = value.ledger.applyPolicyEvent(value.run.runId, started());
    const duplicate = value.ledger.applyPolicyEvent(value.run.runId, structuredClone(started()));
    assert.equal(duplicate.appended, false);
    assert.equal(duplicate.delivery, "pending");
    assert.deepEqual(duplicate.reduction, first.reduction);
    assert.equal(duplicate.record.sequence, first.record.sequence);

    const beforeFailures = value.ledger.events(value.run.runId).length;
    assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, { ...started(), observation: { elapsedMs: 0, totalTokens: 0, totalCostMicros: 0 }, extra: true } as PolicyEvent), /conflicts/u);
    assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, workerCompleted()), /must be delivered/u);
    assert.equal(value.ledger.events(value.run.runId).length, beforeFailures);

    const actionId = first.reduction.action.actionId;
    const evidence = delivery(actionId);
    const ack = value.ledger.recordPolicyActionDelivered(value.run.runId, actionId, evidence);
    assert.equal(value.ledger.recordPolicyActionDelivered(value.run.runId, actionId, structuredClone(evidence)).sequence, ack.sequence);
    assert.throws(() => value.ledger.recordPolicyActionDelivered(value.run.runId, actionId, delivery(actionId, { externalId: "different" })), /conflicts/u);

    const deliveredDuplicate = value.ledger.applyPolicyEvent(value.run.runId, started());
    assert.equal(deliveredDuplicate.delivery, "delivered");
    assert.equal(deliveredDuplicate.appended, false);
    assert.deepEqual(deliveredDuplicate.reduction, first.reduction);
    assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, { ...workerCompleted(), sequence: 3 }), /sequence must equal 2/u);
    assert.throws(() => value.ledger.recordPolicyActionDelivered(value.run.runId, "unknown", delivery("unknown")), /unknown/u);
    assert.equal(value.ledger.events(value.run.runId).length, ack.sequence);

    const second = value.ledger.applyPolicyEvent(value.run.runId, workerCompleted());
    assert.equal(second.record.policyCheckpoint?.revision, 2);
    assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, started()), /stale/u);
    assert.throws(() => value.ledger.recordPolicyActionDelivered(value.run.runId, actionId, evidence), /stale/u);
    assert.equal(value.ledger.events(value.run.runId).length, second.record.sequence);
  } finally {
    value.cleanup();
  }
});

test("concurrent apply and acknowledgment calls serialize to one append each", async () => {
  const value = fixture();
  const barrier = join(value.temporary, "barrier");
  const moduleUrl = pathToFileURL(resolve(import.meta.dirname, "../ledger/file-ledger.ts")).href;
  const runChildren = async (operation: "apply" | "ack", actionId?: string) => {
    const source = `
      import { existsSync } from "node:fs";
      import { RunLedger } from ${JSON.stringify(moduleUrl)};
      const [root, runId, barrier, operation, actionId] = process.argv.slice(1);
      while (!existsSync(barrier)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
      const ledger = new RunLedger({ root });
      const result = operation === "apply"
        ? ledger.applyPolicyEvent(runId, ${JSON.stringify(started())})
        : ledger.recordPolicyActionDelivered(runId, actionId, {
            adapter: "fake-idempotent",
            idempotencyKey: actionId,
            receipt: { externalId: "one" },
          });
      console.log(JSON.stringify(operation === "apply"
        ? { appended: result.appended, sequence: result.record.sequence }
        : { sequence: result.sequence }));
    `;
    rmSync(barrier, { force: true });
    const children = Array.from({ length: 6 }, () => spawn(
      process.execPath,
      ["--experimental-strip-types", "--input-type=module", "-e", source, value.ledger.root, value.run.runId, barrier, operation, actionId ?? ""],
      { stdio: ["ignore", "pipe", "pipe"] },
    ));
    writeFileSync(barrier, "go\n");
    return Promise.all(children.map((child) => new Promise<Record<string, unknown>>((resolveResult, reject) => {
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf8"); });
      child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString("utf8"); });
      child.once("error", reject);
      child.once("close", (code) => code === 0
        ? resolveResult(JSON.parse(stdout.trim()))
        : reject(new Error(`child exited ${code}: ${stderr}`)));
    })));
  };
  try {
    const applies = await runChildren("apply");
    assert.equal(applies.filter((result) => result.appended === true).length, 1);
    assert.equal(new Set(applies.map((result) => result.sequence)).size, 1);
    const actionId = value.ledger.loadRun(value.run.runId).record.pendingPolicyAction!.actionId;
    const acknowledgments = await runChildren("ack", actionId);
    assert.equal(new Set(acknowledgments.map((result) => result.sequence)).size, 1);
    assert.equal(value.ledger.events(value.run.runId).filter((event) => event.type === "policy_event_applied").length, 1);
    assert.equal(value.ledger.events(value.run.runId).filter((event) => event.type === "policy_action_delivered").length, 1);
  } finally {
    value.cleanup();
  }
});

test("malformed, illegal, nonmonotonic, and non-JSON policy inputs append nothing", () => {
  const value = fixture();
  try {
    const before = value.ledger.events(value.run.runId).length;
    const cases: PolicyEvent[] = [
      workerCompleted(),
      { ...started(), sequence: 0 },
      { ...started(), observation: { elapsedMs: Number.NaN, totalTokens: 0, totalCostMicros: 0 } },
      { ...started(), observation: { elapsedMs: 0, totalTokens: 0, totalCostMicros: 0, extra: true } } as PolicyEvent,
    ];
    for (const event of cases) assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, event));
    assert.equal(value.ledger.events(value.run.runId).length, before);
  } finally {
    value.cleanup();
  }
});

test("policy apply and delivery evidence reject size, depth, cycles, and extra keys without appending", () => {
  const value = fixture();
  try {
    const beforeApply = value.ledger.events(value.run.runId).length;
    const oversizedApply = {
      ...started(),
      padding: "x".repeat(4 * 1024 * 1024),
    } as unknown as PolicyEvent;
    assert.throws(
      () => value.ledger.applyPolicyEvent(value.run.runId, oversizedApply),
      /policy event exceeds/u,
    );

    let deepApply: Record<string, unknown> = { leaf: true };
    for (let index = 0; index < 102; index += 1) deepApply = { next: deepApply };
    assert.throws(
      () => value.ledger.applyPolicyEvent(
        value.run.runId,
        { ...started(), nested: deepApply } as unknown as PolicyEvent,
      ),
      /bounded JSON value/u,
    );
    const cyclicApply = started() as PolicyEvent & { cycle?: unknown };
    cyclicApply.cycle = cyclicApply;
    assert.throws(
      () => value.ledger.applyPolicyEvent(value.run.runId, cyclicApply),
      /contains a cycle/u,
    );
    assert.equal(value.ledger.events(value.run.runId).length, beforeApply);

    const applied = value.ledger.applyPolicyEvent(value.run.runId, started());
    const actionId = applied.reduction.action.actionId;
    const beforeDelivery = applied.record.sequence;
    assert.throws(
      () => value.ledger.recordPolicyActionDelivered(
        value.run.runId,
        actionId,
        delivery(actionId, "x".repeat(1024 * 1024)),
      ),
      /policy delivery evidence exceeds/u,
    );

    let deepReceipt: Record<string, JsonValue> = { leaf: true };
    for (let index = 0; index < 102; index += 1) deepReceipt = { next: deepReceipt };
    assert.throws(
      () => value.ledger.recordPolicyActionDelivered(
        value.run.runId,
        actionId,
        delivery(actionId, deepReceipt),
      ),
      /bounded JSON value/u,
    );
    const cyclicReceipt: Record<string, unknown> = {};
    cyclicReceipt.self = cyclicReceipt;
    assert.throws(
      () => value.ledger.recordPolicyActionDelivered(
        value.run.runId,
        actionId,
        {
          adapter: "fake-idempotent",
          idempotencyKey: actionId,
          receipt: cyclicReceipt,
        } as unknown as Parameters<RunLedger["recordPolicyActionDelivered"]>[2],
      ),
      /contains a cycle/u,
    );
    const extraEvidence = {
      ...delivery(actionId),
      unexpected: true,
    };
    assert.throws(
      () => value.ledger.recordPolicyActionDelivered(
        value.run.runId,
        actionId,
        extraEvidence as unknown as Parameters<RunLedger["recordPolicyActionDelivered"]>[2],
      ),
      /evidence or idempotency key is invalid/u,
    );
    assert.equal(value.ledger.events(value.run.runId).length, beforeDelivery);
  } finally {
    value.cleanup();
  }
});

test("an apply event append write failure leaves no policy event or projection", async () => {
  const value = fixture();
  try {
    const eventsPath = join(value.ledger.runDirectory(value.run.runId), "events.jsonl");
    const initialBytes = statSync(eventsPath).size;
    const blockCount = Math.ceil((initialBytes + 1) / 1024);
    const limitBytes = blockCount * 1024;
    appendFileSync(eventsPath, "\n".repeat(limitBytes - initialBytes));

    const moduleUrl = pathToFileURL(resolve(import.meta.dirname, "../ledger/file-ledger.ts")).href;
    const source = `
      import { RunLedger } from ${JSON.stringify(moduleUrl)};
      try {
        new RunLedger({ root: process.env.ROOT }).applyPolicyEvent(
          process.env.RUN_ID,
          ${JSON.stringify(started())},
        );
        console.log(JSON.stringify({ appended: true }));
      } catch (error) {
        console.log(JSON.stringify({ appended: false, message: error instanceof Error ? error.message : String(error) }));
      }
    `;
    const child = spawn(
      "bash",
      ["-c", `trap '' XFSZ; ulimit -f ${blockCount}; exec "$NODE" --experimental-strip-types --input-type=module -e "$SOURCE"`],
      {
        env: {
          ...process.env,
          NODE: process.execPath,
          SOURCE: source,
          ROOT: value.ledger.root,
          RUN_ID: value.run.runId,
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    const result = await new Promise<{ appended: boolean; message?: string }>((resolveResult, reject) => {
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf8"); });
      child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString("utf8"); });
      child.once("error", reject);
      child.once("close", (code) => code === 0
        ? resolveResult(JSON.parse(stdout.trim()))
        : reject(new Error(`append-fault child exited ${code}: ${stderr}`)));
    });
    assert.equal(result.appended, false);
    assert.match(result.message ?? "", /EFBIG|file too large/iu);
    assert.equal(statSync(eventsPath).size, limitBytes);
    assert.equal(value.ledger.events(value.run.runId).length, 1);
    assert.equal(value.ledger.loadRun(value.run.runId).record.sequence, 1);
    assert.equal(value.ledger.loadRun(value.run.runId).record.policyCheckpoint, null);
  } finally {
    value.cleanup();
  }
});

test("apply fsync failure injection has no existing narrow seam", {
  skip: "node:fs fsyncSync is directly imported and no production fault-injection abstraction exists",
}, () => {});

test("event-first policy persistence repairs state after a projection write failure", () => {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-policy-repair-"));
  const repository = join(temporary, "repository");
  const root = join(temporary, "state");
  const runId = "00000000-0000-4000-8000-000000000001";
  mkdirSync(repository);
  let value = 0;
  let collision = "";
  const ledger = new RunLedger({
    root,
    now: () => new Date("2026-08-28T22:00:00.000Z"),
    randomId: () => {
      value += 1;
      const id = `00000000-0000-4000-8000-${value.toString(16).padStart(12, "0")}`;
      if (value === 6) {
        collision = join(root, runId, `state.json.${id}.tmp`);
        mkdirSync(collision);
      }
      return id;
    },
  });
  try {
    const run = ledger.createRun({ objective: "repair", supervisorSessionId: "session", repositoryRoot: repository, policySnapshot: POLICY_SNAPSHOT, hostPid: 4242 });
    assert.throws(() => ledger.applyPolicyEvent(run.runId, started()));
    rmSync(collision, { recursive: true, force: true });
    const repaired = ledger.loadRun(run.runId).record;
    assert.equal(repaired.sequence, 2);
    assert.equal(repaired.pendingPolicyAction?.actionId, "1:worker.started:action");
    assert.equal(ledger.events(run.runId).at(-1)?.type, "policy_event_applied");
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("an acknowledged action is repaired from its event after the state write crashes", () => {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-policy-ack-repair-"));
  const repository = join(temporary, "repository");
  const root = join(temporary, "state");
  const runId = "00000000-0000-4000-8000-000000000001";
  mkdirSync(repository);
  let value = 0;
  let collision = "";
  const ledger = new RunLedger({
    root,
    now: () => new Date("2026-08-28T22:00:00.000Z"),
    randomId: () => {
      value += 1;
      const id = `00000000-0000-4000-8000-${value.toString(16).padStart(12, "0")}`;
      if (value === 8) {
        collision = join(root, runId, `state.json.${id}.tmp`);
        mkdirSync(collision);
      }
      return id;
    },
  });
  try {
    const run = ledger.createRun({ objective: "ack repair", supervisorSessionId: "session", repositoryRoot: repository, policySnapshot: POLICY_SNAPSHOT, hostPid: 4242 });
    const applied = ledger.applyPolicyEvent(run.runId, started());
    assert.throws(() => ledger.recordPolicyActionDelivered(
      run.runId,
      applied.reduction.action.actionId,
      delivery(applied.reduction.action.actionId),
    ));
    rmSync(collision, { recursive: true, force: true });

    const repaired = ledger.loadRun(run.runId).record;
    assert.equal(repaired.sequence, 3);
    assert.equal(repaired.pendingPolicyAction, undefined);
    assert.deepEqual(repaired.policyCheckpoint, applied.reduction.checkpoint);
    assert.equal(ledger.events(run.runId).at(-1)?.type, "policy_action_delivered");
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("truncated-tail recovery preserves a pending action and dispatch-crash redelivery uses one idempotency key", () => {
  const value = fixture();
  try {
    const applied = value.ledger.applyPolicyEvent(value.run.runId, started());
    const actionId = applied.reduction.action.actionId;
    const receipts = new Map<string, { externalId: string }>();
    const adapter = (key: string) => {
      const existing = receipts.get(key);
      if (existing !== undefined) return existing;
      const receipt = { externalId: `external-${receipts.size + 1}` };
      receipts.set(key, receipt);
      return receipt;
    };
    const firstReceipt = adapter(actionId); // Simulated crash before acknowledgment.
    appendFileSync(join(value.ledger.runDirectory(value.run.runId), "events.jsonl"), "{\"partial\":");

    const reloadedLedger = new RunLedger({ root: value.ledger.root });
    const recovered = reloadedLedger.loadRun(value.run.runId).record;
    assert.equal(recovered.pendingPolicyAction?.actionId, actionId);
    const redeliveryReceipt = adapter(actionId);
    assert.deepEqual(redeliveryReceipt, firstReceipt);
    assert.equal(receipts.size, 1);
    const acknowledged = reloadedLedger.recordPolicyActionDelivered(value.run.runId, actionId, delivery(actionId, redeliveryReceipt));
    assert.equal(acknowledged.pendingPolicyAction, undefined);
  } finally {
    value.cleanup();
  }
});

const applyTampering: Array<[string, (events: Array<Record<string, any>>) => void]> = [
  ["policy input", (events) => { events[1]!.evidence.policyEvent.sequence = 2; }],
  ["prior checkpoint", (events) => { events[0]!.projection.policyCheckpoint = { revision: 1 }; }],
  ["checkpoint", (events) => { events[1]!.projection.policyCheckpoint.revision = 9; }],
  ["action evidence", (events) => { events[1]!.evidence.action.actionId = "forged"; }],
  ["occurrence", (events) => { events[1]!.evidence.occurrenceId = "forged"; }],
  ["rule", (events) => { events[1]!.ruleId = "forged"; }],
  ["trace", (events) => { events[1]!.evidence.trace = ["forged"]; }],
  ["pending action", (events) => { events[1]!.projection.pendingPolicyAction.actionId = "forged"; }],
];

for (const [name, tamper] of applyTampering) {
  test(`semantic reload rejects tampered ${name}`, () => {
    const value = fixture();
    try {
      value.ledger.applyPolicyEvent(value.run.runId, started());
      rewriteEvents(value.ledger, value.run.runId, tamper);
      assert.throws(() => new RunLedger({ root: value.ledger.root }).loadRun(value.run.runId), LedgerCorruptionError);
    } finally {
      value.cleanup();
    }
  });
}

test("semantic reload rejects snapshot, acknowledgment, receipt, and extra-key tampering", () => {
  for (const kind of ["snapshot", "ack", "receipt", "ack-evidence-extra", "ack-envelope-extra"] as const) {
    const value = fixture();
    try {
      const applied = value.ledger.applyPolicyEvent(value.run.runId, started());
      value.ledger.recordPolicyActionDelivered(value.run.runId, applied.reduction.action.actionId, delivery(applied.reduction.action.actionId));
      if (kind === "snapshot") {
        writeFileSync(join(value.ledger.runDirectory(value.run.runId), "policy.yaml"), `${POLICY_SNAPSHOT}\n`);
      } else {
        rewriteEvents(value.ledger, value.run.runId, (events) => {
          if (kind === "ack") events[2]!.actionId = "forged";
          else if (kind === "receipt") delete events[2]!.evidence.receipt;
          else if (kind === "ack-evidence-extra") events[2]!.evidence.unexpected = true;
          else events[2]!.unexpected = true;
        });
      }
      assert.throws(() => new RunLedger({ root: value.ledger.root }).loadRun(value.run.runId), LedgerCorruptionError);
    } finally {
      value.cleanup();
    }
  }
});

test("unrelated lifecycle and worker events preserve policy state exactly", () => {
  const value = fixture();
  try {
    const applied = value.ledger.applyPolicyEvent(value.run.runId, started());
    const transitioned = value.ledger.transition(value.run.runId, { state: "worker_running", reason: "worker", ruleId: "worker.start", actor: "extension" });
    const observed = value.ledger.appendWorkerEvent(value.run.runId, { type: "settled" });
    assert.deepEqual(transitioned.policyCheckpoint, applied.record.policyCheckpoint);
    assert.deepEqual(transitioned.pendingPolicyAction, applied.record.pendingPolicyAction);
    assert.deepEqual(observed.policyCheckpoint, applied.record.policyCheckpoint);
    assert.deepEqual(observed.pendingPolicyAction, applied.record.pendingPolicyAction);
    assert.doesNotThrow(() => new RunLedger({ root: value.ledger.root }).loadRun(value.run.runId));
  } finally {
    value.cleanup();
  }
});

test("a terminal policy action can be acknowledged and its checkpoint rejects later reductions", () => {
  const value = fixture();
  try {
    const applyAndAcknowledge = (event: PolicyEvent) => {
      const result = value.ledger.applyPolicyEvent(value.run.runId, event);
      value.ledger.recordPolicyActionDelivered(
        value.run.runId,
        result.reduction.action.actionId,
        delivery(result.reduction.action.actionId),
      );
      return result;
    };
    applyAndAcknowledge(started());
    applyAndAcknowledge(workerCompleted());
    applyAndAcknowledge({
      type: "initial_review_completed",
      sequence: 3,
      observation: { elapsedMs: 2, totalTokens: 2, totalCostMicros: 2 },
      findings: [],
    });
    const terminal = value.ledger.applyPolicyEvent(value.run.runId, {
      type: "validation_completed",
      sequence: 4,
      observation: { elapsedMs: 3, totalTokens: 3, totalCostMicros: 3 },
      passed: true,
      commitPresent: true,
      cleanWorktree: true,
      reviewAccepted: true,
    });
    assert.deepEqual(terminal.reduction.action, {
      actionId: "4:completion.ready_for_manual_integration:action",
      type: "outcome",
      outcome: "ready_for_manual_integration",
    });
    const acknowledged = value.ledger.recordPolicyActionDelivered(
      value.run.runId,
      terminal.reduction.action.actionId,
      delivery(terminal.reduction.action.actionId),
    );
    assert.equal(acknowledged.pendingPolicyAction, undefined);
    assert.equal(acknowledged.policyCheckpoint?.cursor.phase, "terminal");
    assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, {
      type: "incident",
      sequence: 5,
      observation: { elapsedMs: 4, totalTokens: 4, totalCostMicros: 4 },
      incident: "worker_blocked",
    }), /terminal checkpoint rejects all events/u);
  } finally {
    value.cleanup();
  }
});

test("a pending terminal lifecycle run can acknowledge, but no later policy reduction is legal", () => {
  const value = fixture();
  try {
    const applied = value.ledger.applyPolicyEvent(value.run.runId, started());
    value.ledger.transition(value.run.runId, { state: "stopped", reason: "stopped", ruleId: "run.stop", actor: "extension" });
    const actionId = applied.reduction.action.actionId;
    const acknowledged = value.ledger.recordPolicyActionDelivered(value.run.runId, actionId, delivery(actionId));
    assert.equal(acknowledged.state, "stopped");
    assert.equal(acknowledged.pendingPolicyAction, undefined);
    assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, started()), /terminal run .* cannot apply/u);
    assert.throws(() => value.ledger.applyPolicyEvent(value.run.runId, workerCompleted()), /terminal run .* cannot apply/u);
  } finally {
    value.cleanup();
  }
});
