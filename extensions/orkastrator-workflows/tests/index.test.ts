import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { delegateSubagent } from "../delegation-bridge.ts";
import { __indexTest__, installOrkastratorWorkflows } from "../index.ts";
import { currentHerdrPaneId } from "../herdr-session-pane.ts";
import {
  renderWorkflowDetailLines,
  renderWorkflowOutline,
  renderWorkflowWidgetLines,
  workflowReceiptLines,
} from "../workflow-widget.ts";

type Command = {
  description?: string;
  handler: (args: string, ctx: unknown) => Promise<void>;
};

type SentMessage = {
  message: string;
  options: { expandPromptTemplates?: boolean } | undefined;
};

function createHarness() {
  const commands = new Map<string, Command>();
  const messages: SentMessage[] = [];
  const notifications: Array<[string, string]> = [];
  const lifecycle = new Map<string, () => void>();
  const api = {
    events: new EventEmitter(),
    registerCommand(name: string, command: Command) {
      commands.set(name, command);
    },
    sendUserMessage(message: string, options?: SentMessage["options"]) {
      messages.push({ message, options });
    },
    on(event: string, handler: () => void) {
      lifecycle.set(event, handler);
    },
  };

  installOrkastratorWorkflows(api as never);
  return { commands, messages, notifications, lifecycle };
}

function trustedContext(notifications: Array<[string, string]>) {
  return {
    isProjectTrusted: () => true,
    ui: { notify: (message: string, level: string) => notifications.push([message, level]) },
  };
}

function workflowPath(message: string, name: string): string {
  const path = message.match(new RegExp(`workflow="([^"]+${name}\\.workflow\\.ts)"`))?.[1];
  assert.ok(path, `command must use the packaged ${name} workflow path`);
  assert.equal(existsSync(path), true);
  return path;
}

test("workflow event matching is exact and worker placement targets the originating session", () => {
  const workflow = [...__indexTest__.workflowPaths][0]!;
  const workflowInput = { task: "work", repository: "/tmp/repository" };
  assert.deepEqual(__indexTest__.workflowStartInput({
    toolName: "workflow",
    input: { action: "start", workflow, input: workflowInput },
  }), { workflowInput });
  assert.equal(__indexTest__.workflowStartInput({
    toolName: "workflow",
    input: { action: "start", workflow: "/tmp/unrelated.workflow.ts", input: workflowInput },
  }), undefined);
  assert.equal(__indexTest__.workflowResultRunId({ action: "start", runId: "run-1" }, "start"), "run-1");
  assert.equal(__indexTest__.workflowResultRunId({ action: "cancel", runId: "run-1" }, "cancel"), "run-1");
  assert.equal(__indexTest__.workflowResultRunId({ action: "status", runId: "run-1" }, "start"), undefined);
  assert.equal(currentHerdrPaneId({ HERDR_PANE_ID: "origin-pane" }), "origin-pane");
  assert.throws(() => currentHerdrPaneId({}), /cannot place workers/u);
});

test("completed workflow receipts replace raw output with a concise terminal state", () => {
  assert.deepEqual(workflowReceiptLines({
    workflowName: "orkastrator-review",
    startedAt: "2026-08-31T22:54:03.000Z",
    finishedAt: "2026-08-31T22:55:16.000Z",
    updatedAt: "2026-08-31T22:55:16.000Z",
    status: "completed",
    steps: [{}, {}, {}, {}],
    finalOutput: { reason: "no blocking findings", review: { findings: [] } },
  } as never), [
    "✓ orkastrator-review · complete",
    "1m 13s · 4 steps",
    "no blocking findings",
  ]);

});

test("the embedded workflow widget uses theme semantics and omits implied queued labels", () => {
  const theme = {
    bold: (text: string) => `<bold>${text}</bold>`,
    fg: (color: string, text: string) => `<${color}>${text}</${color}>`,
  };
  const lines = renderWorkflowWidgetLines({
    state: {
      workflowName: "orkastrator-review",
      startedAt: "2026-08-31T22:54:03.000Z",
      updatedAt: "2026-08-31T22:54:04.000Z",
      status: "running",
      currentNode: "review",
      currentNodeStartedAt: "2026-08-31T22:54:03.000Z",
      steps: [],
    },
    snapshot: {
      startAt: "review",
      nodes: {
        review: { nodeType: "action" },
        classify: { nodeType: "compute" },
      },
      edges: [{ from: "review", to: "classify" }],
    },
  } as never, 240, theme as never, new Date("2026-08-31T22:54:04.000Z"));

  assert.equal(lines.some((line) => line.includes("<warning>action</warning>")), true);
  assert.equal(lines.some((line) => line.includes("<syntaxFunction>compute</syntaxFunction>")), true);
  assert.equal(lines.some((line) => line.includes("queued")), false);
  assert.equal(lines.some((line) => line.includes("↓")), true);
});

test("workflow widget survives unknown persisted run statuses", () => {
  const theme = {
    bold: (text: string) => text,
    fg: (_color: string, text: string) => text,
  };
  const lines = renderWorkflowWidgetLines({
    state: {
      workflowName: "orkastrator-cook",
      startedAt: "2026-09-01T01:54:00.000Z",
      updatedAt: "2026-09-01T01:54:01.000Z",
      status: "starting",
      steps: [],
    },
    snapshot: {
      startAt: "start",
      nodes: { start: { nodeType: "compute" } },
      edges: [],
    },
  } as never, 120, theme as never, new Date("2026-09-01T01:54:01.000Z"));

  assert.match(lines[0]!, /starting/u);

  const fallback = renderWorkflowWidgetLines({ state: {} } as never, 120, theme as never);
  assert.match(fallback[0]!, /workflow display unavailable/u);
});

test("workflow widget bounds large composed graphs around the active node", () => {
  const theme = {
    bold: (text: string) => text,
    fg: (_color: string, text: string) => text,
  };
  const nodeIds = Array.from({ length: 20 }, (_, index) => `node-${index}`);
  const lines = renderWorkflowWidgetLines({
    state: {
      workflowName: "orkastrator-cook",
      startedAt: "2026-09-01T01:54:00.000Z",
      updatedAt: "2026-09-01T01:54:05.000Z",
      status: "running",
      currentNode: "node-10",
      currentNodeStartedAt: "2026-09-01T01:54:04.000Z",
      steps: nodeIds.slice(0, 10).map((nodeId) => ({
        nodeId,
        outcome: "ok",
        startedAt: "2026-09-01T01:54:00.000Z",
        finishedAt: "2026-09-01T01:54:01.000Z",
      })),
    },
    snapshot: {
      startAt: nodeIds[0],
      nodes: Object.fromEntries(nodeIds.map((nodeId) => [nodeId, { nodeType: "compute" }])),
      edges: nodeIds.slice(1).map((nodeId, index) => ({ from: nodeIds[index], to: nodeId })),
    },
  } as never, 160, theme as never, new Date("2026-09-01T01:54:05.000Z"));

  assert.equal(lines.length, 8);
  assert.equal(lines.some((line) => line.includes("node-10")), true);
  assert.equal(lines.some((line) => /↑ 8 earlier nodes/u.test(line)), true);
  assert.equal(lines.some((line) => /↓ 7 later nodes · \/kas:workflow/u.test(line)), true);
  assert.equal(renderWorkflowDetailLines({
    state: {
      workflowName: "orkastrator-cook",
      updatedAt: "2026-09-01T01:54:05.000Z",
      status: "running",
      steps: [],
    },
    snapshot: {
      startAt: nodeIds[0],
      nodes: Object.fromEntries(nodeIds.map((nodeId) => [nodeId, { nodeType: "compute" }])),
      edges: nodeIds.slice(1).map((nodeId, index) => ({ from: nodeIds[index], to: nodeId })),
    },
  } as never, 160, theme as never).length, 21);
});

test("workflow branches render as a vertical outline with join references", () => {
  const snapshot = {
    startAt: "initial",
    nodes: {
      initial: { nodeType: "action" },
      start: { nodeType: "compute" },
      accepted: { nodeType: "compute" },
      stopped: { nodeType: "compute" },
      resolved: { nodeType: "compute" },
    },
    edges: [
      { from: "initial", to: "start" },
      {
        from: "start",
        switch: { on: "$.route", cases: { accept: "accepted", stop: "stopped" } },
      },
      { from: "accepted", to: "resolved" },
      { from: "stopped", to: "resolved" },
    ],
  } as never;
  const state = {
    currentNode: "start",
    updatedAt: "2026-08-31T22:54:03.000Z",
    statusDetail: "waiting for route",
    steps: [],
  } as never;

  assert.deepEqual(renderWorkflowOutline(snapshot, state, new Date("2026-08-31T22:54:04.000Z")), [
    "· initial  action",
    "↓ ◐ start  compute · running · 1s · waiting for route",
    "├─ accept → · accepted  compute",
    "│  ↓ · resolved  compute",
    "└─ stop → · stopped  compute",
    "   ↓ ↳ resolved",
  ]);
});

test("workflow cancellation applies only after an accepted result and uses the resolved run ID", () => {
  const failed = new Set(["failed-call"]);
  assert.equal(__indexTest__.acceptedCancellationRunId(failed, {
    toolCallId: "failed-call",
    isError: true,
    details: { action: "cancel", runId: "run-1" },
  }), undefined);
  assert.equal(failed.size, 0);

  const accepted = new Set(["accepted-call"]);
  assert.equal(__indexTest__.acceptedCancellationRunId(accepted, {
    toolCallId: "accepted-call",
    isError: false,
    details: { action: "cancel", runId: "resolved-active-run" },
  }), "resolved-active-run");
  assert.equal(accepted.size, 0);
});

test("/kas launches one workflow that owns planning, implementation, and review", async () => {
  const { commands, messages, notifications, lifecycle } = createHarness();
  const kas = commands.get("kas");
  assert.ok(kas);

  await kas.handler("add durable retries", trustedContext(notifications));

  assert.equal(messages.length, 1);
  workflowPath(messages[0]!.message, "orkastrator-implement");
  assert.match(messages[0]!.message, /add durable retries/);
  assert.match(messages[0]!.message, /workflow owns every stage after launch/i);
  assert.doesNotMatch(messages[0]!.message, /skill:implement|skill:grill-with-docs|automatically perform.*kas:check/i);
  assert.equal(messages[0]!.options, undefined);
  assert.equal(notifications.length, 0);

  lifecycle.get("session_shutdown")?.();
});

test("/kas:cook launches one workflow that owns planning through review", async () => {
  const { commands, messages, notifications } = createHarness();
  const cook = commands.get("kas:cook");
  assert.ok(cook);

  await cook.handler("replace the retry scheduler", trustedContext(notifications));

  assert.equal(messages.length, 1);
  workflowPath(messages[0]!.message, "orkastrator-cook");
  assert.match(messages[0]!.message, /replace the retry scheduler/);
  assert.match(messages[0]!.message, /workflow owns every stage after launch/i);
  assert.doesNotMatch(messages[0]!.message, /skill:implement|skill:grill-with-docs|automatically perform.*kas:check/i);
  assert.equal(messages[0]!.options, undefined);
  assert.equal(notifications.length, 0);
});

test("/kas:check launches only the Orkastrator review workflow", async () => {
  const { commands, messages, notifications } = createHarness();
  const check = commands.get("kas:check");
  assert.ok(check);

  await check.handler("preserve the parser contract", trustedContext(notifications));

  assert.equal(messages.length, 1);
  workflowPath(messages[0]!.message, "orkastrator-review");
  assert.match(messages[0]!.message, /preserve the parser contract/);
  assert.match(messages[0]!.message, /clean worktree/);
  assert.doesNotMatch(messages[0]!.message, /skill:implement|grill-with-docs/);
  assert.equal(messages[0]!.options, undefined);
  assert.equal(notifications.length, 0);
});

test("empty implementation requests stop before workflow launch", async () => {
  const { commands, messages, notifications } = createHarness();

  await commands.get("kas")!.handler("   ", trustedContext(notifications));
  await commands.get("kas:cook")!.handler("", trustedContext(notifications));

  assert.equal(messages.length, 2);
  assert.equal(messages.every((item) => /do not start a workflow yet/i.test(item.message)), true);
  assert.equal(messages.every((item) => !/action=start/.test(item.message)), true);
});

test("the published package includes every command-addressed workflow and runtime loader", () => {
  const packageRoot = fileURLToPath(new URL("../../../", import.meta.url));
  const manifest = JSON.parse(readFileSync(`${packageRoot}/package.json`, "utf8")) as {
    dependencies?: Record<string, string>;
  };
  assert.match(manifest.dependencies?.tsx ?? "", /^\^?4\./u);
  const pack = JSON.parse(
    execFileSync("npm", ["pack", "--dry-run", "--json"], {
      cwd: packageRoot,
      encoding: "utf8",
    }),
  ) as Array<{ files: Array<{ path: string }> }>;

  for (const name of [
    "orkastrator-implement.workflow.ts",
    "orkastrator-cook.workflow.ts",
    "orkastrator-review.workflow.ts",
  ]) {
    assert.equal(
      pack[0]?.files.some((file) => file.path === `.pi/workflows/${name}`),
      true,
      `package must contain ${name}`,
    );
  }
});

test("a stale async session_start cannot install after shutdown", async () => {
  const lifecycle = new Map<string, (...args: unknown[]) => unknown>();
  const events = new EventEmitter();
  const eventBus = {
    on(event: string, handler: (payload: unknown) => void) {
      events.on(event, handler);
      return () => events.off(event, handler);
    },
    emit(event: string, payload: unknown) {
      if (event === "prompt-template:subagent:request") {
        const request = payload as { requestId: string; ownerRunId: string; nodeId: string };
        setTimeout(() => eventBus.emit("prompt-template:subagent:response", {
          requestId: request.requestId,
          ownerRunId: request.ownerRunId,
          nodeId: request.nodeId,
          status: "invalid_request",
        }), 5);
      }
      events.emit(event, payload);
    },
  };
  const api = {
    events: eventBus,
    getAllTools: () => [],
    registerCommand() {},
    sendUserMessage() {},
    on(event: string, handler: (...args: unknown[]) => unknown) { lifecycle.set(event, handler); },
  };
  installOrkastratorWorkflows(api as never);
  const starting = lifecycle.get("session_start")?.({}, trustedContext([]));
  lifecycle.get("session_shutdown")?.({});
  await starting;

  let hostedFallback = false;
  const response = await delegateSubagent({
    ownerRunId: "run",
    nodeId: "node",
    agent: "worker",
    task: "work",
    context: "fresh",
    cwd: "/tmp",
    result: { kind: "text" },
  }, new AbortController().signal, async (spec) => {
    hostedFallback = true;
    return {
      requestId: "hosted",
      ownerRunId: spec.ownerRunId,
      nodeId: spec.nodeId,
      status: "completed",
      agent: spec.agent,
      exitCode: 0,
      result: { kind: "text", text: "done" },
    };
  });
  assert.equal(hostedFallback, true);
  assert.equal(response.status, "completed");
  assert.equal(events.listenerCount("prompt-template:subagent:response"), 0);
});

test("workflow management commands route exact control requests", async () => {
  const { commands, messages, notifications } = createHarness();
  const ctx = trustedContext(notifications);
  const cases = [
    ["kas:status", "", { action: "status" }],
    ["kas:status", "run-1", { action: "status", runId: "run-1" }],
    ["kas:pause", "", { action: "pause" }],
    ["kas:resume", "", { action: "resume" }],
    ["kas:cancel", "run-1", { action: "cancel", runId: "run-1" }],
    ["kas-runs", "", { action: "status" }],
  ] as const;

  for (const [name, args, input] of cases) {
    const command = commands.get(name);
    assert.ok(command);
    await command.handler(args, ctx);
    assert.match(messages.at(-1)!.message, new RegExp(JSON.stringify(input).replace(/[{}]/gu, "\\$&"), "u"));
  }

  assert.match(messages[3]!.message, /Do not call workflow status first/u);
  assert.deepEqual(notifications, []);
});

test("workflow management commands reject unsupported or malformed run IDs", async () => {
  const { commands, messages, notifications } = createHarness();
  const ctx = trustedContext(notifications);

  await commands.get("kas:resume")!.handler("run-1", ctx);
  await commands.get("kas:cancel")!.handler("run 1; restart", ctx);

  assert.equal(messages.length, 0);
  assert.deepEqual(notifications, [
    ["/kas:resume operates on the active workflow and does not accept a run ID", "error"],
    ["Workflow run IDs may contain only letters, numbers, dots, underscores, colons, and hyphens", "error"],
  ]);
});

test("all kas execution and management commands require project trust", async () => {
  const { commands, messages, notifications } = createHarness();
  const untrusted = {
    isProjectTrusted: () => false,
    ui: { notify: (message: string, level: string) => notifications.push([message, level]) },
  };
  const names = [
    "kas",
    "kas:cook",
    "kas:check",
    "kas:status",
    "kas:pause",
    "kas:resume",
    "kas:cancel",
    "kas-runs",
  ];

  for (const name of names) {
    const command = commands.get(name);
    assert.ok(command);
    await command.handler("ignored", untrusted);
  }

  assert.equal(messages.length, 0);
  assert.deepEqual(
    notifications,
    names.map(() => ["Orkastrator requires project trust", "error"]),
  );
});
