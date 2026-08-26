import assert from "node:assert/strict";
import { test } from "node:test";

import type { MonitorTask } from "../core.ts";
import {
  installOrkastratorMonitors,
  POLL_INTERVAL_MS,
  STATUS_KEY,
} from "../index.ts";

const RUN_ID = "bb9a9b29-bd4e-4a34-91a0-d471eb4b0a28";

function task(): MonitorTask {
  return {
    id: "b1234abcd",
    name: "KAS-706 monitor",
    command: `orkas monitor ${RUN_ID} --watch`,
    status: "running",
    outputPath: ".pi/tasks/session-x/b1234abcd.output",
    startTime: 1_000,
    pid: 4242,
    runId: RUN_ID,
    stale: false,
    sourcePath: "/repo/.pi/tasks/session-x/b1234abcd.json",
  };
}

interface FakeContext {
  cwd: string;
  trusted: boolean;
  statuses: Array<[string, string | undefined]>;
  notifications: Array<[string, string]>;
  isProjectTrusted(): boolean;
  ui: {
    setStatus(key: string, value: string | undefined): void;
    notify(message: string, level: string): void;
  };
}

function context(trusted = true): FakeContext {
  const ctx: FakeContext = {
    cwd: "/repo",
    trusted,
    statuses: [],
    notifications: [],
    isProjectTrusted: () => ctx.trusted,
    ui: {
      setStatus: (key, value) => ctx.statuses.push([key, value]),
      notify: (message, level) => ctx.notifications.push([message, level]),
    },
  };
  return ctx;
}

test("session lifecycle polls once at a time and clears timer and footer on shutdown", async () => {
  const handlers = new Map<string, (...args: never[]) => unknown>();
  let commandHandler: ((args: string, ctx: FakeContext) => Promise<void>) | undefined;
  const callbacks = new Map<number, () => void>();
  const cleared: number[] = [];
  let nextTimer = 1;
  let activeDiscoveries = 0;
  let maxConcurrentDiscoveries = 0;
  let releaseDiscovery: (() => void) | undefined;
  let calls = 0;
  const api = {
    on: (event: string, handler: (...args: never[]) => unknown) => handlers.set(event, handler),
    registerCommand: (_name: string, options: { handler: typeof commandHandler }) => {
      commandHandler = options.handler;
    },
  };
  const timers = {
    setInterval: (callback: () => void, milliseconds: number) => {
      assert.equal(milliseconds, POLL_INTERVAL_MS);
      const id = nextTimer++;
      callbacks.set(id, callback);
      return id as unknown as ReturnType<typeof setInterval>;
    },
    clearInterval: (handle: ReturnType<typeof setInterval>) => {
      const id = handle as unknown as number;
      cleared.push(id);
      callbacks.delete(id);
    },
  };
  const discover = async (): Promise<MonitorTask[]> => {
    calls += 1;
    activeDiscoveries += 1;
    maxConcurrentDiscoveries = Math.max(maxConcurrentDiscoveries, activeDiscoveries);
    if (calls === 2) await new Promise<void>((resolve) => (releaseDiscovery = resolve));
    activeDiscoveries -= 1;
    return [task()];
  };
  installOrkastratorMonitors(api as never, { discover, timers, now: () => 66_000 });

  const ctx = context();
  await handlers.get("session_start")?.({ type: "session_start", reason: "startup" }, ctx as never);
  assert.deepEqual(ctx.statuses.at(-1), [STATUS_KEY, "● KAS-706 monitor bb9a9b29"]);
  assert.equal(callbacks.size, 1);

  const tick = [...callbacks.values()][0]!;
  tick();
  tick();
  await Promise.resolve();
  assert.equal(calls, 2);
  assert.equal(maxConcurrentDiscoveries, 1);
  releaseDiscovery?.();
  await new Promise((resolve) => setImmediate(resolve));

  assert.ok(commandHandler);
  const commandCtx = context();
  await commandHandler!("", commandCtx);
  assert.equal(callbacks.size, 1);
  assert.match(commandCtx.notifications.at(-1)?.[0] ?? "", /task id: b1234abcd/u);

  await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "reload" }, ctx as never);
  assert.deepEqual(cleared, [1]);
  assert.equal(callbacks.size, 0);
  assert.deepEqual(ctx.statuses.at(-1), [STATUS_KEY, undefined]);
});

test("reload starts one replacement timer and untrusted sessions never read task files", async () => {
  const handlers = new Map<string, (...args: never[]) => unknown>();
  const callbacks = new Map<number, () => void>();
  let nextTimer = 1;
  let discoveryCalls = 0;
  const api = {
    on: (event: string, handler: (...args: never[]) => unknown) => handlers.set(event, handler),
    registerCommand: () => undefined,
  };
  const timers = {
    setInterval: (callback: () => void) => {
      const id = nextTimer++;
      callbacks.set(id, callback);
      return id as unknown as ReturnType<typeof setInterval>;
    },
    clearInterval: (handle: ReturnType<typeof setInterval>) => callbacks.delete(handle as unknown as number),
  };
  installOrkastratorMonitors(api as never, {
    discover: async () => {
      discoveryCalls += 1;
      return [task()];
    },
    timers,
  });

  const first = context();
  await handlers.get("session_start")?.({ type: "session_start", reason: "startup" }, first as never);
  assert.equal(callbacks.size, 1);
  await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "reload" }, first as never);
  await handlers.get("session_start")?.({ type: "session_start", reason: "reload" }, first as never);
  assert.equal(callbacks.size, 1);

  const untrusted = context(false);
  await handlers.get("session_shutdown")?.({ type: "session_shutdown", reason: "new" }, first as never);
  await handlers.get("session_start")?.({ type: "session_start", reason: "new" }, untrusted as never);
  assert.equal(callbacks.size, 0);
  assert.equal(discoveryCalls, 2);
  assert.deepEqual(untrusted.statuses.at(-1), [STATUS_KEY, undefined]);
});

test("throwing host UI during a poll does not emit an unhandled rejection", async () => {
  const handlers = new Map<string, (...args: never[]) => unknown>();
  const callbacks = new Map<number, () => void>();
  let nextTimer = 1;
  let statusCalls = 0;
  const api = {
    on: (event: string, handler: (...args: never[]) => unknown) => handlers.set(event, handler),
    registerCommand: () => undefined,
  };
  const timers = {
    setInterval: (callback: () => void) => {
      const id = nextTimer++;
      callbacks.set(id, callback);
      return id as unknown as ReturnType<typeof setInterval>;
    },
    clearInterval: (handle: ReturnType<typeof setInterval>) => callbacks.delete(handle as unknown as number),
  };
  installOrkastratorMonitors(api as never, {
    discover: async () => [task()],
    timers,
  });

  const ctx = context();
  ctx.ui.setStatus = (key, value) => {
    statusCalls += 1;
    if (statusCalls === 2) throw new Error("ui gone");
    ctx.statuses.push([key, value]);
  };
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown): void => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    await handlers.get("session_start")?.({ type: "session_start", reason: "startup" }, ctx as never);
    assert.equal(callbacks.size, 1);
    [...callbacks.values()][0]!();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});
