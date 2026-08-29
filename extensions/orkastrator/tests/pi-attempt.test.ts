import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  type PiAttemptEvent,
  runOwnedPiAttempt,
} from "../rpc/pi-attempt.ts";

interface Fixture {
  root: string;
  executable: string;
  sessionFile: string;
  cleanup(): void;
}

function fixture(body: string): Fixture {
  const root = mkdtempSync(join(tmpdir(), "orkastrator-pi-attempt-"));
  const executable = join(root, "fake-pi");
  writeFileSync(executable, `#!/usr/bin/env node\n${body}\n`, "utf8");
  chmodSync(executable, 0o700);
  return {
    root,
    executable,
    sessionFile: join(root, "worker.jsonl"),
    cleanup: () => rmSync(root, { recursive: true, force: true }),
  };
}

function settledBody(options: {
  split?: boolean;
  stderr?: number;
  grandchild?: boolean;
  ignoreTerm?: boolean;
  promptMarker?: string;
  readyMarker?: string;
  argvMarker?: string;
} = {}): string {
  return `
const fs = require("node:fs");
const cp = require("node:child_process");
${options.ignoreTerm ? 'process.on("SIGTERM", () => {});' : ""}
${options.readyMarker ? `fs.writeFileSync(${JSON.stringify(options.readyMarker)}, "ready\\n");` : ""}
const args = process.argv.slice(2);
${options.argvMarker ? `fs.writeFileSync(${JSON.stringify(options.argvMarker)}, JSON.stringify(args));` : ""}
const session = args[args.indexOf("--session") + 1];
fs.writeFileSync(session, "preserved\\n");
${options.stderr ? `process.stderr.write("x".repeat(${options.stderr}));` : ""}
${options.grandchild ? 'cp.spawn("sleep", ["60"], { stdio: "ignore" });' : ""}
let buffer = "";
let sendDelay = 0;
const send = (value) => {
  const line = JSON.stringify(value) + "\\n";
  ${options.split ? "const at = sendDelay; sendDelay += 4; setTimeout(() => process.stdout.write(line.slice(0, 3)), at); setTimeout(() => process.stdout.write(line.slice(3)), at + 2);" : "process.stdout.write(line);"}
};
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  while (buffer.includes("\\n")) {
    const at = buffer.indexOf("\\n");
    const record = JSON.parse(buffer.slice(0, at));
    buffer = buffer.slice(at + 1);
    if (record.type === "prompt") {
      ${options.promptMarker ? `fs.writeFileSync(${JSON.stringify(options.promptMarker)}, "prompted\\n");` : ""}
      send({ type: "response", id: record.id, success: true });
      send({ type: "agent_end" });
      send({ type: "tool_execution_start", toolName: "read" });
      send({ type: "tool_execution_end", toolName: "read" });
      send({ type: "agent_settled" });
    } else if (record.type === "get_session_stats") {
      send({ type: "response", id: record.id, success: true, data: { tokens: { input: 2, output: 3, total: 5 }, cost: 0.25 } });
    }
  }
});
setInterval(() => {}, 1000);
`;
}

async function run(
  value: Fixture,
  events: PiAttemptEvent[],
  signal = new AbortController().signal,
  journal?: (bound: boolean) => Promise<void> | void,
  options: {
    prompt?: string;
    model?: string;
    fast?: boolean;
    recordEvent?: (event: PiAttemptEvent) => Promise<void> | void;
  } = {},
) {
  return runOwnedPiAttempt({
    executable: value.executable,
    cwd: value.root,
    sessionFile: value.sessionFile,
    attemptToken: "attempt-1",
    prompt: options.prompt ?? "do one thing",
    model: options.model ?? "provider/model",
    thinking: "low",
    fast: options.fast ?? false,
    journalOwnership: async (identity) => journal?.(identity !== null),
    recordEvent: async (event) => {
      events.push(event);
      await options.recordEvent?.(event);
    },
  }, signal);
}

for (const [name, output, expected] of [
  ["malformed JSON", 'process.stdin.once("data", () => process.stdout.write("{bad}\\n")); setInterval(() => {}, 1000);', /malformed JSON/u],
  ["CRLF", 'process.stdin.once("data", () => process.stdout.write("{}\\r\\n")); setInterval(() => {}, 1000);', /CRLF/u],
  ["unterminated EOF", 'process.stdin.once("data", () => { process.stdout.write("{}"); process.exit(1); });', /unterminated|exited/u],
  ["oversized records", 'process.stdin.once("data", () => process.stdout.write("x".repeat(1_100_000))); setInterval(() => {}, 1000);', /bounded framing/u],
] as const) {
  test(`Pi-specific framing rejects ${name}`, async () => {
    const value = fixture(output);
    const events: PiAttemptEvent[] = [];
    try {
      const result = await run(value, events);
      assert.equal(result.status, "error");
      const message = events.find((event) => event.type === "error")?.message ?? result.error ?? "";
      assert.match(message, expected);
      if (name === "unterminated EOF") {
        assert.equal(result.error, "Pi RPC ended with an unterminated record");
      }
    } finally {
      value.cleanup();
    }
  });
}

test("durable ownership gates prompt, split frames settle, and agent_end is not terminal", async () => {
  const markerToken = `${process.pid}-${Date.now()}`;
  const promptMarker = join(tmpdir(), `orkastrator-prompt-${markerToken}`);
  const readyMarker = join(tmpdir(), `orkastrator-ready-${markerToken}`);
  const value = fixture(settledBody({ split: true, promptMarker, readyMarker }));
  const events: PiAttemptEvent[] = [];
  let journaled = false;
  try {
    const result = await run(value, events, undefined, async (bound) => {
      if (!bound) return;
      while (!existsSync(readyMarker)) {
        await new Promise<void>((resolve) => setTimeout(resolve, 5));
      }
      await new Promise((resolve) => setTimeout(resolve, 20));
      assert.equal(existsSync(promptMarker), false);
      journaled = true;
    });
    assert.equal(result.status, "settled");
    assert.deepEqual(result.usage, { input: 2, output: 3, total: 5, cost: 0.25 });
    assert.equal(journaled, true);
    assert.ok(events.findIndex((event) => event.type === "prompt_accepted") > 0);
    assert.equal(events.filter((event) => event.type === "settled").length, 1);
    assert.equal(readFileSync(value.sessionFile, "utf8"), "preserved\n");
  } finally {
    rmSync(promptMarker, { force: true });
    rmSync(readyMarker, { force: true });
    value.cleanup();
  }
});

test("Pi launch argv loads only the pinned fast extension when requested", async () => {
  const token = `${process.pid}-${Date.now()}`;
  const fastMarker = join(tmpdir(), `orkastrator-fast-argv-${token}`);
  const standardMarker = join(tmpdir(), `orkastrator-standard-argv-${token}`);
  const fast = fixture(settledBody({ argvMarker: fastMarker }));
  const standard = fixture(settledBody({ argvMarker: standardMarker }));
  try {
    assert.equal((await run(fast, [], undefined, undefined, {
      fast: true,
      model: "openai-codex/gpt-5.6-sol",
    })).status, "settled");
    assert.equal((await run(standard, [], undefined, undefined, { fast: false })).status, "settled");
    const fastArgv = JSON.parse(readFileSync(fastMarker, "utf8")) as string[];
    const standardArgv = JSON.parse(readFileSync(standardMarker, "utf8")) as string[];
    assert.equal(fastArgv.includes("--fast"), false);
    assert.equal(standardArgv.includes("--fast"), false);
    assert.equal(fastArgv.filter((argument) => argument === "--extension").length, 1);
    const extensionIndex = fastArgv.indexOf("--extension");
    assert.match(fastArgv[extensionIndex + 1] ?? "", /\/rpc\/openai-fast\.ts$/u);
    assert.equal(standardArgv.includes("--extension"), false);
  } finally {
    rmSync(fastMarker, { force: true });
    rmSync(standardMarker, { force: true });
    fast.cleanup();
    standard.cleanup();
  }
});

test("cancellation before launch gate and after settlement is idempotent", async () => {
  const before = fixture(settledBody());
  const after = fixture(settledBody());
  const preAborted = new AbortController();
  preAborted.abort();
  try {
    const cancelled = await run(before, [], preAborted.signal);
    assert.equal(cancelled.status, "cancelled");
    const completedController = new AbortController();
    const completed = await run(after, [], completedController.signal);
    assert.equal(completed.status, "settled");
    completedController.abort();
    completedController.abort();
  } finally {
    before.cleanup();
    after.cleanup();
  }
});

test("abort during a pending prompt write cancels and reaps the worker", async () => {
  const value = fixture("setInterval(() => {}, 1000);");
  const controller = new AbortController();
  try {
    setTimeout(() => controller.abort(), 50);
    const startedAt = Date.now();
    const result = await run(value, [], controller.signal, undefined, {
      prompt: "x".repeat(16 * 1024 * 1024),
    });
    assert.equal(result.status, "cancelled");
    assert.ok(Date.now() - startedAt < 1_500);
  } finally {
    value.cleanup();
  }
});

test("agent_end alone remains active until AbortSignal cancellation", async () => {
  const value = fixture(`
let buffer = "";
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  if (!buffer.includes("\\n")) return;
  const record = JSON.parse(buffer.slice(0, buffer.indexOf("\\n")));
  process.stdout.write(JSON.stringify({ type: "response", id: record.id, success: true }) + "\\n");
  process.stdout.write(JSON.stringify({ type: "agent_end" }) + "\\n");
});
setInterval(() => {}, 1000);
`);
  const controller = new AbortController();
  const events: PiAttemptEvent[] = [];
  try {
    setTimeout(() => controller.abort(), 80);
    const result = await run(value, events, controller.signal);
    assert.equal(result.status, "cancelled");
    assert.equal(events.some((event) => event.type === "settled"), false);
    assert.equal(events.filter((event) => event.type === "abort").length, 1);
  } finally {
    value.cleanup();
  }
});

test("stderr is bounded and worker crashes are typed", async () => {
  const flood = fixture(settledBody({ stderr: 100_000 }));
  const crash = fixture('process.stdin.once("data", () => process.exit(23));');
  try {
    const flooded = await run(flood, []);
    assert.equal(flooded.status, "settled");
    assert.equal(flooded.stderrTruncated, true);
    assert.ok(Buffer.byteLength(flooded.stderr) <= 32 * 1024);
    const crashed = await run(crash, []);
    assert.equal(crashed.status, "error");
    assert.equal(crashed.exitCode, 23);
  } finally {
    flood.cleanup();
    crash.cleanup();
  }
});

test("SIGTERM-resistant workers are escalated to SIGKILL", async () => {
  const value = fixture(settledBody({ ignoreTerm: true }));
  try {
    const result = await run(value, []);
    assert.equal(result.status, "settled");
    assert.equal(result.exitSignal, "SIGKILL");
  } finally {
    value.cleanup();
  }
});

test("an ambiguous durable bind is confirmed idempotently before prompting", async () => {
  const value = fixture(settledBody());
  let bound = false;
  let bindCalls = 0;
  let clearCalls = 0;
  try {
    const result = await run(value, [], undefined, (isBound) => {
      if (isBound) {
        bindCalls += 1;
        if (bound) return;
        bound = true;
        throw new Error("state projection failed after durable bind");
      }
      clearCalls += 1;
      bound = false;
    });
    assert.equal(result.status, "settled");
    assert.equal(bindCalls, 2);
    assert.equal(clearCalls, 1);
    assert.equal(bound, false);
  } finally {
    value.cleanup();
  }
});

test("post-reap cleanup survives telemetry and projection failures", async () => {
  const value = fixture(settledBody());
  let bound = false;
  let clearCalls = 0;
  try {
    await assert.rejects(
      run(value, [], undefined, (isBound) => {
        if (isBound) {
          bound = true;
          return;
        }
        clearCalls += 1;
        bound = false;
        if (clearCalls === 1) throw new Error("state projection failed after durable clear");
      }, {
        recordEvent: (event) => {
          if (event.type === "exit") throw new Error("telemetry projection failed");
        },
      }),
      /telemetry projection failed/u,
    );
    assert.equal(clearCalls, 2);
    assert.equal(bound, false);
  } finally {
    value.cleanup();
  }
});

test("the operation reaps the detached process group before clearing durable ownership", async () => {
  const value = fixture(settledBody({ grandchild: true }));
  const events: PiAttemptEvent[] = [];
  let processGroupId = 0;
  let absentWhenCleared = false;
  try {
    const result = await run(value, events, undefined, (bound) => {
      if (bound) {
        processGroupId = (events.find((event) => event.type === "started") as Extract<PiAttemptEvent, { type: "started" }> | undefined)?.identity.processGroupId ?? processGroupId;
        return;
      }
      const started = events.find((event) => event.type === "started") as Extract<PiAttemptEvent, { type: "started" }>;
      processGroupId = started.identity.processGroupId;
      try {
        process.kill(-processGroupId, 0);
      } catch (error) {
        absentWhenCleared = (error as NodeJS.ErrnoException).code === "ESRCH";
      }
    });
    assert.equal(result.status, "settled");
    assert.equal(absentWhenCleared, true);
  } finally {
    value.cleanup();
  }
});
