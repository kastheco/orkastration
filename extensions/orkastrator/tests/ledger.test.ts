import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

import {
  ActiveRunError,
  LedgerCorruptionError,
  LedgerError,
  RunLedger,
} from "../ledger/file-ledger.ts";

function ids(): () => string {
  let value = 0;
  return () => {
    value += 1;
    return `00000000-0000-4000-8000-${value.toString(16).padStart(12, "0")}`;
  };
}

function fixture(): {
  temporary: string;
  repository: string;
  ledger: RunLedger;
  cleanup: () => void;
} {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-ledger-"));
  const repository = join(temporary, "repository");
  mkdirSync(repository);
  return {
    temporary,
    repository,
    ledger: new RunLedger({
      root: join(temporary, "state"),
      now: () => new Date("2026-08-28T22:00:00.000Z"),
      randomId: ids(),
    }),
    cleanup: () => rmSync(temporary, { recursive: true, force: true }),
  };
}

function create(ledger: RunLedger, repository: string, session = "session-1") {
  return ledger.createRun({
    objective: "Prove the lifecycle ledger.",
    supervisorSessionId: session,
    repositoryRoot: repository,
    policySnapshot: "version: 1\nmode: lifecycle-test\n",
    hostPid: 4242,
  });
}

test("run creation snapshots policy and commits the event before its state projection", () => {
  const value = fixture();
  try {
    const record = create(value.ledger, value.repository);
    const directory = value.ledger.runDirectory(record.runId);

    assert.equal(record.state, "submitted");
    assert.equal(record.sequence, 1);
    assert.equal(value.ledger.policySnapshot(record.runId), "version: 1\nmode: lifecycle-test\n");
    assert.deepEqual(JSON.parse(readFileSync(join(directory, "state.json"), "utf8")), record);
    const events = value.ledger.events(record.runId);
    assert.equal(events.length, 1);
    assert.equal(events[0]?.type, "run_created");
    assert.deepEqual(events[0]?.projection, record);

    assert.throws(
      () => create(value.ledger, value.repository),
      (error: unknown) => error instanceof ActiveRunError && /already owns active run/u.test(error.message),
    );
    assert.equal(create(value.ledger, value.repository, "session-2").state, "submitted");
  } finally {
    value.cleanup();
  }
});

test("a projection write failure preserves the already-fsynced authoritative event", () => {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-ledger-failure-"));
  const repository = join(temporary, "repository");
  const root = join(temporary, "state");
  const runId = "00000000-0000-4000-8000-000000000001";
  mkdirSync(repository);
  let value = 0;
  const ledger = new RunLedger({
    root,
    now: () => new Date("2026-08-28T22:00:00.000Z"),
    randomId: () => {
      value += 1;
      if (value === 4) mkdirSync(join(root, runId, "state.json"));
      return `00000000-0000-4000-8000-${value.toString(16).padStart(12, "0")}`;
    },
  });
  try {
    assert.throws(() => create(ledger, repository));
    const directory = ledger.runDirectory(runId);
    assert.equal(existsSync(join(directory, "policy.yaml")), true);
    const lines = readFileSync(join(directory, "events.jsonl"), "utf8").trimEnd().split("\n");
    assert.equal(lines.length, 1);
    assert.equal((JSON.parse(lines[0]!) as { type: string }).type, "run_created");
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("an unlocked persistent writer-lock file does not block the next mutation", () => {
  const value = fixture();
  try {
    writeFileSync(
      join(value.ledger.root, ".writer-lock"),
      `${JSON.stringify({ pid: 2_000_000_000, processStartTime: "dead", token: "dead" })}\n`,
    );
    assert.equal(create(value.ledger, value.repository).state, "submitted");
    assert.equal(existsSync(join(value.ledger.root, ".writer-lock")), true);
  } finally {
    value.cleanup();
  }
});

test("concurrent processes cannot create two active runs for one supervisor session", async () => {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-ledger-contention-"));
  const repository = join(temporary, "repository");
  const root = join(temporary, "state");
  const barrier = join(temporary, "start");
  mkdirSync(repository);
  const moduleUrl = pathToFileURL(
    resolve(import.meta.dirname, "../ledger/file-ledger.ts"),
  ).href;
  const source = `
    import { existsSync } from "node:fs";
    import { RunLedger } from ${JSON.stringify(moduleUrl)};
    const [root, repository, barrier] = process.argv.slice(1);
    while (!existsSync(barrier)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
    try {
      const run = new RunLedger({ root }).createRun({
        objective: "contention",
        supervisorSessionId: "shared-session",
        repositoryRoot: repository,
        policySnapshot: "version: 1\\n",
        hostPid: process.pid,
      });
      console.log(JSON.stringify({ ok: true, runId: run.runId }));
    } catch (error) {
      console.log(JSON.stringify({ ok: false, message: error instanceof Error ? error.message : String(error) }));
    }
  `;
  try {
    const children = Array.from({ length: 8 }, () =>
      spawn(
        process.execPath,
        ["--experimental-strip-types", "--input-type=module", "-e", source, root, repository, barrier],
        { stdio: ["ignore", "pipe", "pipe"] },
      ),
    );
    writeFileSync(barrier, "go\n");
    const results = await Promise.all(
      children.map(
        (child) =>
          new Promise<{ ok: boolean; runId?: string; message?: string }>((resolveResult, reject) => {
            let stdout = "";
            let stderr = "";
            child.stdout.on("data", (chunk: Buffer) => {
              stdout += chunk.toString("utf8");
            });
            child.stderr.on("data", (chunk: Buffer) => {
              stderr += chunk.toString("utf8");
            });
            child.once("error", reject);
            child.once("close", (code) => {
              if (code !== 0) {
                reject(new Error(`child exited ${code}: ${stderr}`));
                return;
              }
              resolveResult(JSON.parse(stdout.trim()));
            });
          }),
      ),
    );
    assert.equal(results.filter((result) => result.ok).length, 1);
    assert.equal(
      results
        .filter((result) => !result.ok)
        .every((result) => /already owns active run|another process is writing/u.test(result.message ?? "")),
      true,
    );
    assert.equal(new RunLedger({ root }).activeRunsForSession("shared-session").length, 1);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("events reconstruct state without Pi history or a valid state projection file", () => {
  const value = fixture();
  try {
    const created = create(value.ledger, value.repository);
    const validating = value.ledger.transition(created.runId, {
      state: "validating",
      reason: "checks_started",
      ruleId: "validation.start",
      actor: "extension",
      evidence: { commit: "a".repeat(40) },
    });
    writeFileSync(join(value.ledger.runDirectory(created.runId), "state.json"), "not-json\n");

    const loaded = value.ledger.loadRun(created.runId).record;
    assert.deepEqual(loaded, validating);
    assert.deepEqual(
      JSON.parse(readFileSync(join(value.ledger.runDirectory(created.runId), "state.json"), "utf8")),
      validating,
    );
  } finally {
    value.cleanup();
  }
});

test("symlinked run directories and ledger files are rejected", () => {
  const fileValue = fixture();
  try {
    const created = create(fileValue.ledger, fileValue.repository);
    const policyPath = join(fileValue.ledger.runDirectory(created.runId), "policy.yaml");
    const externalPolicy = join(fileValue.temporary, "external-policy.yaml");
    writeFileSync(externalPolicy, "version: 1\nmode: lifecycle-test\n");
    rmSync(policyPath);
    symlinkSync(externalPolicy, policyPath);
    assert.throws(
      () => fileValue.ledger.loadRun(created.runId),
      (error: unknown) =>
        error instanceof LedgerCorruptionError && /not a regular file/u.test(error.message),
    );
  } finally {
    fileValue.cleanup();
  }

  const directoryValue = fixture();
  try {
    const created = create(directoryValue.ledger, directoryValue.repository);
    const directory = directoryValue.ledger.runDirectory(created.runId);
    const externalDirectory = join(directoryValue.temporary, "external-run");
    mkdirSync(externalDirectory);
    rmSync(directory, { recursive: true });
    symlinkSync(externalDirectory, directory, "dir");
    assert.throws(
      () => directoryValue.ledger.loadRun(created.runId),
      (error: unknown) =>
        error instanceof LedgerCorruptionError && /not a real directory/u.test(error.message),
    );
    assert.throws(
      () => directoryValue.ledger.scanNonterminal(),
      (error: unknown) =>
        error instanceof LedgerCorruptionError && /not a real directory/u.test(error.message),
    );
    assert.throws(
      () => create(directoryValue.ledger, directoryValue.repository, "session-2"),
      (error: unknown) =>
        error instanceof LedgerCorruptionError && /not a real directory/u.test(error.message),
    );
  } finally {
    directoryValue.cleanup();
  }
});

test("policy tampering and complete-event sequence gaps fail loudly", () => {
  const value = fixture();
  try {
    const first = create(value.ledger, value.repository);
    writeFileSync(join(value.ledger.runDirectory(first.runId), "policy.yaml"), "version: changed\n");
    assert.throws(
      () => value.ledger.loadRun(first.runId),
      (error: unknown) =>
        error instanceof LedgerCorruptionError && /policy snapshot hash/u.test(error.message),
    );
  } finally {
    value.cleanup();
  }

  const nestedValue = fixture();
  try {
    const created = create(nestedValue.ledger, nestedValue.repository);
    const eventsPath = join(nestedValue.ledger.runDirectory(created.runId), "events.jsonl");
    const event = JSON.parse(readFileSync(eventsPath, "utf8")) as {
      projection: { ownedProcesses: unknown[] };
    };
    event.projection.ownedProcesses = [{ pid: "not-an-integer" }];
    writeFileSync(eventsPath, `${JSON.stringify(event)}\n`);
    assert.throws(
      () => nestedValue.ledger.loadRun(created.runId),
      (error: unknown) =>
        error instanceof LedgerCorruptionError && /nested identity evidence/u.test(error.message),
    );
  } finally {
    nestedValue.cleanup();
  }

  const sequenceValue = fixture();
  try {
    const created = create(sequenceValue.ledger, sequenceValue.repository);
    sequenceValue.ledger.transition(created.runId, {
      state: "preparing",
      reason: "prepare",
      ruleId: "run.prepare",
      actor: "extension",
    });
    const eventsPath = join(sequenceValue.ledger.runDirectory(created.runId), "events.jsonl");
    const events = readFileSync(eventsPath, "utf8").trimEnd().split("\n").map((line) => JSON.parse(line));
    events[1].sequence = 3;
    events[1].projection.sequence = 3;
    writeFileSync(eventsPath, `${events.map((event) => JSON.stringify(event)).join("\n")}\n`);
    assert.throws(
      () => sequenceValue.ledger.loadRun(created.runId),
      (error: unknown) =>
        error instanceof LedgerCorruptionError && /sequence continuity/u.test(error.message),
    );
  } finally {
    sequenceValue.cleanup();
  }
});

test("a truncated final event is dropped once and recorded as deterministic recovery", () => {
  const value = fixture();
  try {
    const created = create(value.ledger, value.repository);
    const eventsPath = join(value.ledger.runDirectory(created.runId), "events.jsonl");
    const partial = '{"schemaVersion":1,"eventId":"partial';
    appendFileSync(eventsPath, partial);

    const first = value.ledger.loadRun(created.runId);
    assert.deepEqual(first.recovery, {
      droppedBytes: Buffer.byteLength(partial),
      previousSequence: 1,
    });
    assert.equal(first.record.sequence, 2);
    assert.equal(value.ledger.events(created.runId).at(-1)?.type, "ledger_tail_recovered");
    assert.equal(readFileSync(eventsPath).subarray(-1).equals(Buffer.from("\n")), true);

    const second = value.ledger.loadRun(created.runId);
    assert.equal(second.recovery, undefined);
    assert.equal(second.record.sequence, 2);
    assert.equal(value.ledger.events(created.runId).length, 2);
  } finally {
    value.cleanup();
  }
});

test("awaiting_owner is nonterminal and resumes the same run after an allowed answer", () => {
  const value = fixture();
  try {
    const created = create(value.ledger, value.repository);
    const waiting = value.ledger.beginAwaitingOwner(created.runId, {
      ruleId: "integration.conflict",
      evidence: { paths: ["src/a.ts"] },
      allowedDecisions: ["retry", "stop", "retry"],
      resumeState: "submitted",
    });

    assert.equal(waiting.state, "awaiting_owner");
    assert.deepEqual(waiting.ownerWait?.allowedDecisions, ["retry", "stop"]);
    assert.equal(value.ledger.scanNonterminal().some((run) => run.runId === created.runId), true);
    assert.throws(
      () => value.ledger.answerOwner(created.runId, { decision: "merge", rationale: "unsafe" }),
      /not allowed/u,
    );

    const resumed = value.ledger.answerOwner(created.runId, {
      decision: "retry",
      rationale: "Retry with the recorded scope.",
    });
    assert.equal(resumed.runId, created.runId);
    assert.equal(resumed.state, "submitted");
    assert.equal(resumed.ownerWait?.response?.decision, "retry");
    assert.equal(value.ledger.events(created.runId).at(-1)?.type, "owner_answered");
  } finally {
    value.cleanup();
  }
});

test("terminal runs reject later state mutation", () => {
  const value = fixture();
  try {
    const created = create(value.ledger, value.repository);
    value.ledger.transition(created.runId, {
      state: "interrupted",
      reason: "session_shutdown:quit",
      ruleId: "lifecycle.session_shutdown",
      actor: "extension",
    });
    assert.throws(
      () =>
        value.ledger.transition(created.runId, {
          state: "preparing",
          reason: "restart",
          ruleId: "run.restart",
          actor: "owner",
        }),
      (error: unknown) => error instanceof LedgerError && /terminal run/u.test(error.message),
    );
  } finally {
    value.cleanup();
  }
});
