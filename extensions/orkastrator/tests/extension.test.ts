import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { installOrkastrator, ENTRY_TYPE, STATUS_KEY } from "../index.ts";
import { RunLedger } from "../ledger/file-ledger.ts";
import { LifecycleCoordinator } from "../lifecycle.ts";
import type { PiAttemptResult, PiAttemptSpec } from "../rpc/pi-attempt.ts";
import { POLICY_SNAPSHOT } from "./policy-fixture.ts";

interface FakeContext {
  cwd: string;
  trusted: boolean;
  sessionManager: { getSessionId(): string };
  model: { provider: string; id: string };
  thinkingLevel: "low";
  isProjectTrusted(): boolean;
  statuses: Array<[string, string | undefined]>;
  notifications: Array<[string, string | undefined]>;
  ui: {
    setStatus(key: string, value: string | undefined): void;
    notify(message: string, level?: string): void;
  };
}

type FakeHandler = (
  event: { reason: "startup" | "reload" | "new" | "resume" | "fork" | "quit" },
  context: FakeContext,
) => Promise<void> | void;

interface FakeTool {
  name: string;
  execute(
    toolCallId: string,
    params: Record<string, string>,
    signal: AbortSignal | undefined,
    onUpdate: undefined,
    context: FakeContext,
  ): Promise<{ content: Array<{ type: string; text: string }>; details: Record<string, unknown> }>;
}

interface FakeCommand {
  handler(args: string, context: FakeContext): Promise<void>;
}

class FakeApi {
  readonly handlers = new Map<string, FakeHandler>();
  readonly tools = new Map<string, FakeTool>();
  readonly commands = new Map<string, FakeCommand>();
  readonly entries: Array<{ customType: string; data: unknown }> = [];
  readonly userMessages: Array<{
    content: string;
    options: { expandPromptTemplates?: boolean } | undefined;
  }> = [];

  on(event: string, handler: FakeHandler): void {
    this.handlers.set(event, handler);
  }

  registerTool(tool: unknown): void {
    const typed = tool as FakeTool;
    this.tools.set(typed.name, typed);
  }

  registerCommand(name: string, command: unknown): void {
    this.commands.set(name, command as FakeCommand);
  }

  appendEntry(customType: string, data: unknown): void {
    this.entries.push({ customType, data });
  }

  sendUserMessage(
    content: string,
    options?: { expandPromptTemplates?: boolean },
  ): void {
    this.userMessages.push({ content, options });
  }
}

function context(repository: string, session = "session-1", trusted = true): FakeContext {
  const value: FakeContext = {
    cwd: repository,
    trusted,
    sessionManager: { getSessionId: () => session },
    model: { provider: "test", id: "worker" },
    thinkingLevel: "low",
    isProjectTrusted: () => value.trusted,
    statuses: [],
    notifications: [],
    ui: {
      setStatus: (key, status) => value.statuses.push([key, status]),
      notify: (message, level) => value.notifications.push([message, level]),
    },
  };
  return value;
}

function fixture(options: {
  canonicalizeRepository?: (path: string) => string;
  runAttempt?: (spec: PiAttemptSpec, signal: AbortSignal) => Promise<PiAttemptResult>;
} = {}): {
  temporary: string;
  repository: string;
  ledger: RunLedger;
  coordinator: LifecycleCoordinator;
  api: FakeApi;
  cleanup: () => void;
} {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-extension-"));
  const repository = join(temporary, "repository");
  mkdirSync(repository);
  const ledger = new RunLedger({ root: join(temporary, "state") });
  const coordinator = new LifecycleCoordinator(ledger);
  const api = new FakeApi();
  installOrkastrator(api as unknown as ExtensionAPI, {
    ledger,
    coordinator,
    hostPid: 4242,
    canonicalizeRepository: options.canonicalizeRepository ?? (() => repository),
    randomAttemptToken: () => "attempt-token",
    runAttempt: options.runAttempt ?? (async () => ({
      status: "settled",
      usage: { input: 1, output: 1, total: 2, cost: 0 },
      stderr: "",
      stderrTruncated: false,
      exitCode: 143,
      exitSignal: null,
    })),
  });
  return {
    temporary,
    repository,
    ledger,
    coordinator,
    api,
    cleanup: () => rmSync(temporary, { recursive: true, force: true }),
  };
}

test("kas command starts the Pi-native worker flow without legacy Orca", async () => {
  const value = fixture();
  try {
    const ctx = context(value.repository);
    await value.api.commands.get("kas")?.handler("Ship the lifecycle slice", ctx);
    assert.deepEqual(value.api.userMessages, [
      {
        content:
          "Start a Pi-native Orkastrator v1 worker attempt for this objective: Ship the lifecycle slice\n\nUse orkastrator_run_create directly. Do not use the legacy orkas CLI or Orca.",
        options: undefined,
      },
    ]);
  } finally {
    value.cleanup();
  }
});

test("trusted run tool creates one durable run and rejects a second active run", async () => {
  const value = fixture();
  try {
    const ctx = context(value.repository);
    await value.api.handlers.get("session_start")?.({ reason: "startup" }, ctx);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    const result = await tool.execute(
      "tool-1",
      { objective: "Build the ledger", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    const record = value.ledger.activeRunsForSession("session-1")[0];
    assert.ok(record);
    assert.match(result.content[0]?.text ?? "", new RegExp(record.runId, "u"));
    assert.deepEqual(value.api.entries.at(-1), {
      customType: ENTRY_TYPE,
      data: { action: "run_created", runId: record.runId, policyHash: record.policyHash },
    });
    assert.deepEqual(ctx.statuses.at(-1), [STATUS_KEY, `● ${record.runId.slice(0, 8)} submitted`]);

    await assert.rejects(
      tool.execute(
        "tool-2",
        { objective: "Start twice", policySnapshot: POLICY_SNAPSHOT },
        undefined,
        undefined,
        ctx,
      ),
      /already owns active run/u,
    );
  } finally {
    value.cleanup();
  }
});

test("worker events are ledgered before Pi history and clear ownership with reap evidence", async () => {
  const value = fixture({
    runAttempt: async (spec) => {
      const identity = {
        pid: 6001,
        processGroupId: 6001,
        sessionFile: spec.sessionFile,
        attemptToken: spec.attemptToken,
      };
      await spec.journalOwnership(identity);
      await spec.recordEvent({ type: "started", identity });
      await spec.recordEvent({ type: "prompt_accepted" });
      await spec.recordEvent({ type: "exit", code: 143, signal: null });
      await spec.journalOwnership(null);
      return {
        status: "settled",
        stderr: "",
        stderrTruncated: false,
        exitCode: 143,
        exitSignal: null,
      };
    },
  });
  const durableBeforeMirror: boolean[] = [];
  const appendEntry = value.api.appendEntry.bind(value.api);
  value.api.appendEntry = (customType, data) => {
    const entry = data as { action?: string; runId?: string; sequence?: number };
    if (entry.action === "worker_event" && entry.runId !== undefined) {
      durableBeforeMirror.push(
        value.ledger.events(entry.runId).some((event) => event.sequence === entry.sequence),
      );
    }
    appendEntry(customType, data);
  };
  try {
    const ctx = context(value.repository);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    await tool.execute(
      "tool-1",
      { objective: "Persist worker events", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    const run = value.ledger.activeRunsForSession("session-1")[0]!;
    const events = value.ledger.events(run.runId);
    const durableWorkerEvents = events.filter((event) => event.type === "worker_event");
    const historyWorkerEntries = value.api.entries.filter(
      (entry) => (entry.data as { action?: string }).action === "worker_event",
    );

    assert.deepEqual(durableWorkerEvents.map((event) => event.sequence), [3, 4, 5]);
    assert.deepEqual(durableBeforeMirror, [true, true, true]);
    assert.deepEqual(
      historyWorkerEntries.map((entry) => (entry.data as { sequence: number }).sequence),
      [3, 4, 5],
    );
    assert.deepEqual(
      historyWorkerEntries.map((entry) => (entry.data as { event: unknown }).event),
      durableWorkerEvents.map((event) => event.evidence.event),
    );
    assert.deepEqual(events.at(-1)?.evidence, {
      identity: {
        pid: 6001,
        processGroupId: 6001,
        sessionFile: join(value.ledger.runDirectory(run.runId), "worker-attempt-token.jsonl"),
        attemptToken: "attempt-token",
      },
      processGroupAbsent: true,
      exitCode: 143,
      exitSignal: null,
    });
  } finally {
    value.cleanup();
  }
});

test("a ledger worker-event append failure cannot produce a successful tool result", async () => {
  let ownershipCleared = false;
  const value = fixture({
    runAttempt: async (spec) => {
      const identity = {
        pid: 6003,
        processGroupId: 6003,
        sessionFile: spec.sessionFile,
        attemptToken: spec.attemptToken,
      };
      await spec.journalOwnership(identity);
      let telemetryError: unknown;
      try {
        await spec.recordEvent({ type: "settled" });
      } catch (error) {
        telemetryError = error;
      }
      await spec.journalOwnership(null);
      ownershipCleared = true;
      if (telemetryError !== undefined) throw telemetryError;
      return {
        status: "settled",
        stderr: "",
        stderrTruncated: false,
        exitCode: 0,
        exitSignal: null,
      };
    },
  });
  value.ledger.appendWorkerEvent = () => {
    throw new Error("simulated ledger append failure");
  };
  try {
    const ctx = context(value.repository);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    await assert.rejects(
      tool.execute(
        "tool-1",
        { objective: "Fail closed", policySnapshot: POLICY_SNAPSHOT },
        undefined,
        undefined,
        ctx,
      ),
      /simulated ledger append failure/u,
    );
    assert.equal(ownershipCleared, true);
    assert.equal(
      value.api.entries.some(
        (entry) => (entry.data as { action?: string }).action === "worker_event",
      ),
      false,
    );
  } finally {
    value.cleanup();
  }
});

test("untrusted sessions cannot create or inspect project-local run state", async () => {
  const value = fixture();
  try {
    const ctx = context(value.repository, "session-1", false);
    await value.api.handlers.get("session_start")?.({ reason: "startup" }, ctx);
    assert.deepEqual(ctx.statuses.at(-1), [STATUS_KEY, undefined]);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    await assert.rejects(
      tool.execute(
        "tool-1",
        { objective: "Denied", policySnapshot: POLICY_SNAPSHOT },
        undefined,
        undefined,
        ctx,
      ),
      /requires project trust/u,
    );
    await value.api.commands.get("kas-runs")?.handler("", ctx);
    assert.match(ctx.notifications.at(-1)?.[0] ?? "", /requires project trust/u);
    assert.equal(value.ledger.scanNonterminal().length, 0);
  } finally {
    value.cleanup();
  }
});

test("the default Pi path is repository-local and a pre-aborted tool signal reaches the adapter", async () => {
  let executable = "";
  let adapterAborted = false;
  const value = fixture({
    runAttempt: async (spec, signal) => {
      executable = spec.executable;
      adapterAborted = signal.aborted;
      return {
        status: "cancelled",
        stderr: "",
        stderrTruncated: false,
        exitCode: null,
        exitSignal: "SIGTERM",
      };
    },
  });
  const controller = new AbortController();
  controller.abort();
  try {
    const ctx = context(value.repository);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    await tool.execute(
      "tool-1",
      { objective: "Respect cancellation", policySnapshot: POLICY_SNAPSHOT },
      controller.signal,
      undefined,
      ctx,
    );
    assert.equal(
      executable,
      resolve(import.meta.dirname, "../../../node_modules/.bin/pi"),
    );
    assert.equal(adapterAborted, true);
  } finally {
    value.cleanup();
  }
});

test("shutdown awaits worker reap and ownership clear before the terminal transition", async () => {
  const order: string[] = [];
  const value = fixture({
    runAttempt: async (spec, signal) => {
      await spec.journalOwnership({
        pid: 6001,
        processGroupId: 6001,
        sessionFile: join(value.repository, "worker.jsonl"),
        attemptToken: spec.attemptToken,
      });
      order.push("ownership_bound");
      await new Promise<void>((resolveAbort) => {
        if (signal.aborted) resolveAbort();
        else signal.addEventListener("abort", () => resolveAbort(), { once: true });
      });
      order.push("abort_received");
      await new Promise<void>((resolveTick) => setImmediate(resolveTick));
      await spec.journalOwnership(null);
      order.push("ownership_cleared");
      return {
        status: "cancelled",
        stderr: "",
        stderrTruncated: false,
        exitCode: 143,
        exitSignal: null,
      };
    },
  });
  try {
    const ctx = context(value.repository);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    const execution = tool.execute(
      "tool-1",
      { objective: "Wait for shutdown", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    while (!order.includes("ownership_bound")) {
      await new Promise<void>((resolveTick) => setImmediate(resolveTick));
    }
    const runId = value.ledger.activeRunsForSession("session-1")[0]!.runId;
    await value.api.handlers.get("session_shutdown")?.({ reason: "quit" }, ctx);
    order.push("shutdown_returned");
    await execution;
    assert.deepEqual(order, [
      "ownership_bound",
      "abort_received",
      "ownership_cleared",
      "shutdown_returned",
    ]);
    assert.equal(value.ledger.activeRunsForSession("session-1").length, 0);
    assert.deepEqual(
      value.ledger.events(runId).slice(-2).map((event) => event.type),
      ["owned_process_cleared", "run_state_transitioned"],
    );
  } finally {
    value.cleanup();
  }
});

test("shutdown durably interrupts after post-cleanup telemetry failure", async () => {
  let ownershipBound = false;
  const value = fixture({
    runAttempt: async (spec, signal) => {
      await spec.journalOwnership({
        pid: 6002,
        processGroupId: 6002,
        sessionFile: join(value.repository, "worker-telemetry.jsonl"),
        attemptToken: spec.attemptToken,
      });
      ownershipBound = true;
      await new Promise<void>((resolveAbort) => {
        if (signal.aborted) resolveAbort();
        else signal.addEventListener("abort", () => resolveAbort(), { once: true });
      });
      await spec.journalOwnership(null);
      ownershipBound = false;
      throw new Error("exit telemetry failed after cleanup");
    },
  });
  try {
    const ctx = context(value.repository);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    const execution = tool.execute(
      "tool-1",
      { objective: "Interrupt after telemetry failure", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    const executionFailure = assert.rejects(execution, /exit telemetry failed after cleanup/u);
    while (!ownershipBound) {
      await new Promise<void>((resolveTick) => setImmediate(resolveTick));
    }
    const runId = value.ledger.activeRunsForSession("session-1")[0]!.runId;
    const shutdown = value.api.handlers.get("session_shutdown");
    assert.ok(shutdown);

    await assert.rejects(
      async () => shutdown({ reason: "quit" }, ctx),
      /exit telemetry failed after cleanup/u,
    );
    await executionFailure;

    const interrupted = value.ledger.loadRun(runId).record;
    assert.equal(ownershipBound, false);
    assert.equal(interrupted.state, "interrupted");
    assert.equal(interrupted.reason, "session_shutdown:quit");
    assert.deepEqual(
      value.ledger.events(runId).slice(-2).map((event) => event.type),
      ["owned_process_cleared", "run_state_transitioned"],
    );
  } finally {
    value.cleanup();
  }
});

test("reload rebinds the same session while quit interrupts and preserves the run directory", async () => {
  const value = fixture();
  try {
    const ctx = context(value.repository);
    const tool = value.api.tools.get("orkastrator_run_create");
    assert.ok(tool);
    await tool.execute(
      "tool-1",
      { objective: "Reload safely", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    const runId = value.ledger.activeRunsForSession("session-1")[0]!.runId;

    await value.api.handlers.get("session_shutdown")?.({ reason: "reload" }, ctx);
    await value.api.handlers.get("session_start")?.({ reason: "reload" }, ctx);
    assert.equal(value.ledger.loadRun(runId).record.generation, 2);
    assert.equal(value.ledger.loadRun(runId).record.state, "submitted");
    assert.equal(
      value.api.entries.some(
        (entry) =>
          entry.customType === ENTRY_TYPE &&
          (entry.data as { action?: string }).action === "reload_rebound",
      ),
      true,
    );

    await value.api.handlers.get("session_shutdown")?.({ reason: "quit" }, ctx);
    assert.equal(value.ledger.loadRun(runId).record.state, "interrupted");
    assert.equal(value.ledger.policySnapshot(runId), POLICY_SNAPSHOT);
    assert.deepEqual(ctx.statuses.at(-1), [STATUS_KEY, undefined]);
  } finally {
    value.cleanup();
  }
});

test("shutdown records interruption when repository canonicalization becomes unavailable", async () => {
  let canonicalizations = 0;
  let repository = "";
  const value = fixture({
    canonicalizeRepository: () => {
      canonicalizations += 1;
      if (canonicalizations > 1) throw new Error("cwd removed");
      return repository;
    },
  });
  repository = value.repository;
  try {
    const ctx = context(value.repository);
    const createTool = value.api.tools.get("orkastrator_run_create");
    assert.ok(createTool);
    await createTool.execute(
      "tool-create",
      { objective: "Survive missing cwd", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    const runId = value.ledger.activeRunsForSession("session-1")[0]!.runId;

    await value.api.handlers.get("session_shutdown")?.({ reason: "quit" }, ctx);
    assert.equal(value.ledger.loadRun(runId).record.state, "interrupted");
    assert.equal(canonicalizations, 1);
  } finally {
    value.cleanup();
  }
});

test("reload fails closed when repository canonicalization becomes unavailable", async () => {
  let canonicalizations = 0;
  let repository = "";
  const value = fixture({
    canonicalizeRepository: () => {
      canonicalizations += 1;
      if (canonicalizations > 1) throw new Error("cwd removed");
      return repository;
    },
  });
  repository = value.repository;
  try {
    const ctx = context(value.repository);
    const createTool = value.api.tools.get("orkastrator_run_create");
    assert.ok(createTool);
    await createTool.execute(
      "tool-create",
      { objective: "Fail reload closed", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    const runId = value.ledger.activeRunsForSession("session-1")[0]!.runId;

    await value.api.handlers.get("session_shutdown")?.({ reason: "reload" }, ctx);
    const interrupted = value.ledger.loadRun(runId).record;
    assert.equal(interrupted.state, "interrupted");
    assert.equal(interrupted.reason, "reload_repository_identity_unproven");
  } finally {
    value.cleanup();
  }
});

test("same-session startup reports a stale run without claiming or mutating it", async () => {
  const value = fixture();
  try {
    const old = value.coordinator.startRun({
      objective: "Wait for owner",
      supervisorSessionId: "current-session",
      repositoryRoot: value.repository,
      policySnapshot: POLICY_SNAPSHOT,
      hostPid: 4242,
    });
    value.ledger.beginAwaitingOwner(old.runId, {
      ruleId: "policy.owner_required",
      evidence: { question: "Retry?" },
      allowedDecisions: ["retry", "stop"],
      resumeState: "submitted",
    });

    const current = context(value.repository, "current-session");
    await value.api.handlers.get("session_start")?.({ reason: "startup" }, current);
    assert.match(current.notifications.at(-1)?.[0] ?? "", /preserved without being claimed/u);
    assert.deepEqual(current.statuses.at(-1), [STATUS_KEY, undefined]);

    const answer = value.api.tools.get("orkastrator_owner_answer");
    assert.ok(answer);
    await assert.rejects(
      answer.execute(
        "tool-answer",
        { runId: old.runId, decision: "retry", rationale: "Do not claim after a crash." },
        undefined,
        undefined,
        current,
      ),
      /preserved but not claimed/u,
    );
    await value.api.handlers.get("session_shutdown")?.({ reason: "quit" }, current);
    const preserved = value.ledger.loadRun(old.runId).record;
    assert.equal(preserved.state, "awaiting_owner");
    assert.equal(preserved.ownerWait?.response, undefined);
  } finally {
    value.cleanup();
  }
});

test("owner answers resume an awaiting run claimed by this extension instance", async () => {
  const value = fixture();
  try {
    const ctx = context(value.repository, "current-session");
    const createTool = value.api.tools.get("orkastrator_run_create");
    const answerTool = value.api.tools.get("orkastrator_owner_answer");
    assert.ok(createTool);
    assert.ok(answerTool);
    await createTool.execute(
      "tool-create",
      { objective: "Wait for owner", policySnapshot: POLICY_SNAPSHOT },
      undefined,
      undefined,
      ctx,
    );
    const run = value.ledger.activeRunsForSession("current-session")[0]!;
    value.ledger.beginAwaitingOwner(run.runId, {
      ruleId: "policy.owner_required",
      evidence: { question: "Retry?" },
      allowedDecisions: ["retry", "stop"],
      resumeState: "submitted",
    });

    await answerTool.execute(
      "tool-answer",
      { runId: run.runId, decision: "retry", rationale: "Use the same bounded run." },
      undefined,
      undefined,
      ctx,
    );
    const resumed = value.ledger.loadRun(run.runId).record;
    assert.equal(resumed.runId, run.runId);
    assert.equal(resumed.state, "submitted");
    assert.equal(resumed.ownerWait?.response?.decision, "retry");
  } finally {
    value.cleanup();
  }
});
