import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { installOrkastratorWorkflows } from "../index.ts";

type Command = {
  description?: string;
  handler: (args: string, ctx: unknown) => Promise<void>;
};

type SentMessage = {
  message: string;
  options: { expandPromptTemplates?: boolean } | undefined;
};

function createHarness(availableSkills = ["grill-with-docs", "implement"]) {
  const commands = new Map<string, Command>();
  const messages: SentMessage[] = [];
  const notifications: Array<[string, string]> = [];
  const lifecycle = new Map<string, () => void>();
  const implementSkillPath = fileURLToPath(
    new URL("./fixtures/implement-skill.md", import.meta.url),
  );
  const api = {
    events: new EventEmitter(),
    registerCommand(name: string, command: Command) {
      commands.set(name, command);
    },
    getCommands() {
      return [
        {
          name: "skill:grill-with-docs",
          source: "skill",
          sourceInfo: { path: "/skills/grill-with-docs/SKILL.md" },
        },
        {
          name: "skill:implement",
          source: "skill",
          sourceInfo: { path: implementSkillPath },
        },
      ].filter((command) => availableSkills.includes(command.name.slice("skill:".length)));
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

test("/kas invokes implement directly and automatically checks the committed result", async () => {
  const { commands, messages, notifications, lifecycle } = createHarness();
  const kas = commands.get("kas");
  assert.ok(kas);

  await kas.handler("add durable retries", trustedContext(notifications));

  assert.equal(messages.length, 1);
  assert.match(messages[0]!.message, /^\/skill:implement /);
  assert.match(messages[0]!.message, /add durable retries/);
  assert.match(messages[0]!.message, /do not invoke.*grill-with-docs/i);
  assert.match(messages[0]!.message, /automatically perform.*\/kas:check/i);
  assert.match(messages[0]!.message, /workflow=orkastrator-review/);
  assert.deepEqual(messages[0]!.options, { expandPromptTemplates: true });
  assert.equal(notifications.length, 0);

  lifecycle.get("session_shutdown")?.();
});

test("/kas:cook grills with docs, implements the accepted plan, then checks it", async () => {
  const { commands, messages, notifications } = createHarness();
  const cook = commands.get("kas:cook");
  assert.ok(cook);

  await cook.handler("replace the retry scheduler", trustedContext(notifications));

  assert.equal(messages.length, 1);
  assert.match(messages[0]!.message, /^\/skill:grill-with-docs /);
  assert.match(messages[0]!.message, /replace the retry scheduler/);
  assert.match(messages[0]!.message, /across as many user turns as needed/i);
  assert.match(messages[0]!.message, /Implement from the accepted plan/);
  assert.doesNotMatch(messages[0]!.message, /name: implement/);
  assert.match(messages[0]!.message, /automatically perform.*\/kas:check/i);
  assert.match(messages[0]!.message, /workflow=orkastrator-review/);
  assert.deepEqual(messages[0]!.options, { expandPromptTemplates: true });
  assert.equal(notifications.length, 0);
});

test("/kas:check runs only the Orkastrator review workflow", async () => {
  const { commands, messages, notifications } = createHarness();
  const check = commands.get("kas:check");
  assert.ok(check);

  await check.handler("preserve the parser contract", trustedContext(notifications));

  assert.equal(messages.length, 1);
  assert.match(messages[0]!.message, /workflow=orkastrator-review/);
  assert.match(messages[0]!.message, /preserve the parser contract/);
  assert.match(messages[0]!.message, /clean worktree/);
  assert.doesNotMatch(messages[0]!.message, /skill:implement|grill-with-docs/);
  assert.equal(messages[0]!.options, undefined);
  assert.equal(notifications.length, 0);
});

test("implementation commands fail closed when their required skills are missing", async () => {
  const withoutImplement = createHarness(["grill-with-docs"]);
  await withoutImplement.commands.get("kas")!.handler(
    "ignored",
    trustedContext(withoutImplement.notifications),
  );
  await withoutImplement.commands.get("kas:cook")!.handler(
    "ignored",
    trustedContext(withoutImplement.notifications),
  );

  assert.equal(withoutImplement.messages.length, 0);
  assert.deepEqual(withoutImplement.notifications, [
    ["Required skill /skill:implement is not installed", "error"],
    ["Required skill /skill:implement is not installed", "error"],
  ]);

  const withoutGrill = createHarness(["implement"]);
  await withoutGrill.commands.get("kas:cook")!.handler(
    "ignored",
    trustedContext(withoutGrill.notifications),
  );
  assert.equal(withoutGrill.messages.length, 0);
  assert.deepEqual(withoutGrill.notifications, [
    ["Required skill /skill:grill-with-docs is not installed", "error"],
  ]);
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
