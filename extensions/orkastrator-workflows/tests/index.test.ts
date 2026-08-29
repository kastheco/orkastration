import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { installOrkastratorWorkflows } from "../index.ts";

test("/kas is a trusted shorthand for the orkastrator-review workflow policy", async () => {
  const commands = new Map<string, { handler: (args: string, ctx: unknown) => Promise<void> }>();
  const messages: string[] = [];
  const notifications: Array<[string, string]> = [];
  const lifecycle = new Map<string, () => void>();
  const api = {
    events: new EventEmitter(),
    registerCommand(name: string, command: { handler: (args: string, ctx: unknown) => Promise<void> }) {
      commands.set(name, command);
    },
    sendUserMessage(message: string) {
      messages.push(message);
    },
    on(event: string, handler: () => void) {
      lifecycle.set(event, handler);
    },
  };

  installOrkastratorWorkflows(api as never);
  const kas = commands.get("kas");
  assert.ok(kas);
  await kas.handler("preserve the parser contract", {
    isProjectTrusted: () => true,
    ui: { notify: (message: string, level: string) => notifications.push([message, level]) },
  });

  assert.equal(messages.length, 1);
  assert.match(messages[0]!, /workflow=orkastrator-review/);
  assert.match(messages[0]!, /preserve the parser contract/);
  assert.match(messages[0]!, /clean worktree/);
  assert.doesNotMatch(messages[0]!, /orkastrator_run_create directly/);
  assert.equal(notifications.length, 0);

  await kas.handler("ignored", {
    isProjectTrusted: () => false,
    ui: { notify: (message: string, level: string) => notifications.push([message, level]) },
  });
  assert.equal(messages.length, 1);
  assert.deepEqual(notifications, [["Orkastrator requires project trust", "error"]]);

  lifecycle.get("session_shutdown")?.();
});
