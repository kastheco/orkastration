import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, readdir, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createConnection } from "node:net";
import { test } from "node:test";
import { promisify } from "node:util";

import type { SubagentDelegationTerminalResponse } from "pi-subagents/delegation";

import type { DelegationSpec } from "../delegation-bridge.ts";
import { SessionDelegationBroker } from "../herdr-delegation-broker.ts";
import { delegateWithHerdrBroker } from "../herdr-delegation-client.ts";
import {
  herdrBrokerDescriptorPath,
  parseHerdrLaunchBinding,
} from "../herdr-launch.ts";

const execFileAsync = promisify(execFile);

const baseSpec: DelegationSpec = {
  ownerRunId: "run-1",
  nodeId: "initial-review",
  agent: "reviewer",
  task: "review the committed change",
  context: "fresh",
  cwd: "/tmp/repository",
  thinking: "medium",
  result: { kind: "text" },
};

function completed(spec: DelegationSpec): SubagentDelegationTerminalResponse {
  return {
    requestId: "interactive-result",
    ownerRunId: spec.ownerRunId,
    nodeId: spec.nodeId,
    status: "completed",
    agent: spec.agent,
    exitCode: 0,
    result: { kind: "text", text: "done" },
  };
}

test("Herdr launch bindings are strict and reject injected fields", () => {
  const launchId = "123e4567-e89b-42d3-a456-426614174000";
  assert.deepEqual(parseHerdrLaunchBinding({ version: 1, transport: "unix", launchId }), {
    version: 1,
    transport: "unix",
    launchId,
  });
  assert.throws(
    () => parseHerdrLaunchBinding({ version: 1, transport: "unix", launchId, capability: "injected" }),
    /unknown or missing fields/u,
  );
  assert.throws(
    () => parseHerdrLaunchBinding({ version: 1, transport: "unix", launchId: "not-a-uuid" }),
    /UUID v4/u,
  );
});

test("hosted delegation crosses the Unix broker and receives server-owned pane placement", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  const calls: DelegationSpec[] = [];
  const broker = new SessionDelegationBroker({
    sessionId: "session-one",
    env,
    run: async (spec) => {
      calls.push(spec);
      return completed(spec);
    },
  });
  try {
    await broker.start();
    const binding = await broker.registerLaunch("pane-root");
    broker.bindRun(binding.launchId, "run-1");

    const descriptorPath = herdrBrokerDescriptorPath(binding.launchId, env);
    assert.equal((await stat(descriptorPath)).mode & 0o777, 0o600);
    const descriptor = JSON.parse(await readFile(descriptorPath, "utf8")) as Record<string, unknown>;
    assert.equal(typeof descriptor.capability, "string");
    assert.equal(JSON.stringify(descriptor).includes("review the committed change"), false);

    const response = await delegateWithHerdrBroker(
      { ...baseSpec, herdrLaunch: binding },
      binding,
      new AbortController().signal,
      env,
    );
    assert.equal(response.status, "completed");
    assert.deepEqual(response.result, { kind: "text", text: "done" });
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0]?.panePlacement, {
      parentPaneId: "pane-root",
      stackId: binding.launchId,
      firstDirection: "right",
    });
    assert.equal(calls[0]?.herdrLaunch, undefined);
  } finally {
    await broker.close();
    await rm(runtime, { recursive: true, force: true });
  }
});

test("desktop delegation crosses the Unix broker without Herdr pane placement", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  const calls: DelegationSpec[] = [];
  const broker = new SessionDelegationBroker({
    sessionId: "desktop-session",
    env,
    run: async (spec) => {
      calls.push(spec);
      return completed(spec);
    },
  });
  try {
    await broker.start();
    const binding = await broker.registerLaunch();
    broker.bindRun(binding.launchId, "run-1");

    const response = await delegateWithHerdrBroker(
      { ...baseSpec, herdrLaunch: binding },
      binding,
      new AbortController().signal,
      env,
    );
    assert.equal(response.status, "completed");
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.panePlacement, undefined);
  } finally {
    await broker.close();
    await rm(runtime, { recursive: true, force: true });
  }
});

test("a separate hosted process can await the interactive broker result", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  const broker = new SessionDelegationBroker({
    sessionId: "session-process-boundary",
    env,
    run: async (spec) => completed(spec),
  });
  try {
    await broker.start();
    const binding = await broker.registerLaunch("pane-root");
    broker.bindRun(binding.launchId, "run-1");
    const clientUrl = new URL("../herdr-delegation-client.ts", import.meta.url).href;
    const script = [
      `import { delegateWithHerdrBroker } from ${JSON.stringify(clientUrl)};`,
      `const spec = ${JSON.stringify({ ...baseSpec, herdrLaunch: binding })};`,
      `const binding = ${JSON.stringify(binding)};`,
      "const response = await delegateWithHerdrBroker(spec, binding, new AbortController().signal);",
      "console.log(JSON.stringify(response));",
    ].join("\n");
    const { stdout } = await execFileAsync(
      process.execPath,
      ["--experimental-strip-types", "--input-type=module", "-e", script],
      { encoding: "utf8", env },
    );
    const response = JSON.parse(stdout) as SubagentDelegationTerminalResponse;
    assert.equal(response.status, "completed");
    assert.deepEqual(response.result, { kind: "text", text: "done" });
  } finally {
    await broker.close();
    await rm(runtime, { recursive: true, force: true });
  }
});

test("broker socket paths are process-unique and shutdown destroys idle clients", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  const first = new SessionDelegationBroker({ sessionId: "shared-session", env });
  const second = new SessionDelegationBroker({ sessionId: "shared-session", env });
  try {
    assert.notEqual(first.socketPath, second.socketPath);
    await first.start();
    await second.start();
    const idle = createConnection(first.socketPath);
    await new Promise<void>((resolve, reject) => {
      idle.once("connect", resolve);
      idle.once("error", reject);
    });
    const idleClosed = new Promise<void>((resolve) => idle.once("close", resolve));
    await Promise.race([
      first.close(),
      new Promise<never>((_resolve, reject) => {
        setTimeout(() => reject(new Error("broker close hung on an idle socket")), 500);
      }),
    ]);
    await Promise.race([
      idleClosed,
      new Promise<never>((_resolve, reject) => {
        setTimeout(() => reject(new Error("idle client did not observe broker shutdown")), 500);
      }),
    ]);
  } finally {
    await first.close();
    await second.close();
    await rm(runtime, { recursive: true, force: true });
  }
});

test("broker registration racing shutdown leaves no launch descriptor", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  try {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const broker = new SessionDelegationBroker({ sessionId: `race-${attempt}`, env });
      await broker.start();
      const registering = broker.registerLaunch("pane-root");
      const closing = broker.close();
      await assert.rejects(registering, /closed during launch registration|unavailable/u);
      await closing;
    }
    const uid = typeof process.getuid === "function" ? process.getuid() : "user";
    const brokerFiles = await readdir(join(runtime, `orkastrator-${uid}`));
    assert.equal(brokerFiles.some((file) => file.startsWith("launch-")), false);
  } finally {
    await rm(runtime, { recursive: true, force: true });
  }
});

test("broker rejects cross-run reuse and fails closed after release", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  const broker = new SessionDelegationBroker({
    sessionId: "session-two",
    env,
    run: async (spec) => completed(spec),
  });
  try {
    await broker.start();
    const binding = await broker.registerLaunch("pane-root");
    broker.bindRun(binding.launchId, "run-1");
    await assert.rejects(
      delegateWithHerdrBroker(
        { ...baseSpec, ownerRunId: "run-2", herdrLaunch: binding },
        binding,
        new AbortController().signal,
        env,
      ),
      /belongs to another workflow lineage/u,
    );

    await broker.releaseLaunch(binding.launchId);
    await assert.rejects(
      delegateWithHerdrBroker(
        { ...baseSpec, herdrLaunch: binding },
        binding,
        new AbortController().signal,
        env,
      ),
      /originating session may have closed/u,
    );
  } finally {
    await broker.close();
    await rm(runtime, { recursive: true, force: true });
  }
});

test("accepted continuation preserves the Herdr launch lineage", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  const ownerRunIds: string[] = [];
  const broker = new SessionDelegationBroker({
    sessionId: "session-continuation",
    env,
    continuationRunIds: (parentRunId) => parentRunId === "parent-run" ? ["continuation-run"] : [],
    run: async (spec) => {
      ownerRunIds.push(spec.ownerRunId);
      return completed(spec);
    },
  });
  try {
    await broker.start();
    const binding = await broker.registerLaunch("pane-root");
    broker.bindRun(binding.launchId, "parent-run");

    const response = await delegateWithHerdrBroker(
      { ...baseSpec, ownerRunId: "continuation-run", herdrLaunch: binding },
      binding,
      new AbortController().signal,
      env,
    );

    assert.equal(response.status, "completed");
    assert.deepEqual(ownerRunIds, ["continuation-run"]);
  } finally {
    await broker.close();
    await rm(runtime, { recursive: true, force: true });
  }
});

test("client abort closes the request and aborts the exact interactive child", async () => {
  const runtime = await mkdtemp(join(tmpdir(), "orkastrator-broker-test-"));
  const env = { ...process.env, XDG_RUNTIME_DIR: runtime };
  let started!: () => void;
  const didStart = new Promise<void>((resolve) => { started = resolve; });
  let aborted!: () => void;
  const didAbort = new Promise<void>((resolve) => { aborted = resolve; });
  const broker = new SessionDelegationBroker({
    sessionId: "session-three",
    env,
    run: async (_spec, signal) => await new Promise<SubagentDelegationTerminalResponse>((_resolve, reject) => {
      started();
      signal.addEventListener("abort", () => {
        aborted();
        reject(new DOMException("cancelled", "AbortError"));
      }, { once: true });
    }),
  });
  try {
    await broker.start();
    const binding = await broker.registerLaunch("pane-root");
    const controller = new AbortController();
    const delegated = delegateWithHerdrBroker(
      { ...baseSpec, herdrLaunch: binding },
      binding,
      controller.signal,
      env,
    );
    await didStart;
    controller.abort();
    await assert.rejects(delegated, (error: unknown) =>
      error instanceof DOMException && error.name === "AbortError");
    await didAbort;
  } finally {
    await broker.close();
    await rm(runtime, { recursive: true, force: true });
  }
});
