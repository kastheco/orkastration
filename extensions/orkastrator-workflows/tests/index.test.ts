import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { delegateSubagent } from "../delegation-bridge.ts";
import { installOrkastratorWorkflows } from "../index.ts";

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

test("the published package includes every command-addressed workflow", () => {
  const packageRoot = fileURLToPath(new URL("../../../", import.meta.url));
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

  await assert.rejects(delegateSubagent({
    ownerRunId: "run",
    nodeId: "node",
    agent: "worker",
    task: "work",
    context: "fresh",
    cwd: "/tmp",
    result: { kind: "text" },
  }, new AbortController().signal), /bridge is not installed/u);
  assert.equal(events.listenerCount("prompt-template:subagent:response"), 0);
});

test("all kas execution commands require project trust", async () => {
  const { commands, messages, notifications } = createHarness();
  const untrusted = {
    isProjectTrusted: () => false,
    ui: { notify: (message: string, level: string) => notifications.push([message, level]) },
  };

  for (const name of ["kas", "kas:cook", "kas:check"]) {
    const command = commands.get(name);
    assert.ok(command);
    await command.handler("ignored", untrusted);
  }

  assert.equal(messages.length, 0);
  assert.deepEqual(notifications, [
    ["Orkastrator requires project trust", "error"],
    ["Orkastrator requires project trust", "error"],
    ["Orkastrator requires project trust", "error"],
  ]);
});
