import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  delegateSubagent,
  detectDelegationBackend,
  installDelegationBridge,
  probePiSubagents,
  type DelegationEvents,
  type DelegationSpec,
  type ToolSourceMetadata,
} from "../delegation-bridge.ts";

const HERDR_API = Symbol.for("pi-herdr-subagents/delegation.v1");
const herdrGlobal = globalThis as typeof globalThis & { [HERDR_API]?: unknown };

const spec: DelegationSpec = {
  ownerRunId: "run",
  nodeId: "node",
  agent: "worker",
  task: "work",
  context: "fresh",
  cwd: "/tmp",
  result: { kind: "text" },
};

function tool(source: string): ToolSourceMetadata {
  return { name: "subagent", sourceInfo: { source, path: `/packages/${source}/index.ts` } };
}

class CapabilityEvents implements DelegationEvents {
  private readonly listeners = new Map<string, Set<(payload: unknown) => void>>();
  private readonly hasPiSubagents: boolean;

  constructor(hasPiSubagents: boolean) {
    this.hasPiSubagents = hasPiSubagents;
  }

  on(event: string, handler: (payload: unknown) => void): () => void {
    const handlers = this.listeners.get(event) ?? new Set();
    handlers.add(handler);
    this.listeners.set(event, handlers);
    return () => handlers.delete(handler);
  }

  emit(event: string, payload: unknown): void {
    if (event === "prompt-template:subagent:request" && this.hasPiSubagents) {
      const request = payload as { requestId: string; ownerRunId: string; nodeId: string };
      this.emit("prompt-template:subagent:response", {
        requestId: request.requestId,
        ownerRunId: request.ownerRunId,
        nodeId: request.nodeId,
        status: "invalid_request",
        error: "Unsupported delegation field: orkastratorCapabilityProbe.",
      });
    }
    for (const handler of this.listeners.get(event) ?? []) handler(payload);
  }

  listenerCount(event: string): number {
    return this.listeners.get(event)?.size ?? 0;
  }
}

function context() {
  return {
    cwd: "/tmp",
    thinkingLevel: "medium",
    sessionManager: {},
    modelRegistry: {},
  } as never;
}

function installHerdr(result: Record<string, unknown>, controller?: AbortController) {
  const calls: Array<{ params: Record<string, unknown>; options: Record<string, unknown> }> = [];
  herdrGlobal[HERDR_API] = {
    version: 1,
    runSubagent: async (
      params: Record<string, unknown>,
      _ctx: unknown,
      options: Record<string, unknown>,
    ) => {
      calls.push({ params, options });
      controller?.abort();
      return result;
    },
  };
  const uninstallBridge = installDelegationBridge(new EventEmitter() as never, {}, {
    backend: "pi-herdr-subagents",
    getContext: context,
  });
  return {
    calls,
    uninstall: () => {
      uninstallBridge();
      delete herdrGlobal[HERDR_API];
    },
  };
}

test("capability detection routes either backend and rejects both or neither", async () => {
  delete herdrGlobal[HERDR_API];
  assert.equal(await detectDelegationBackend([], new CapabilityEvents(true), 5), "pi-subagents");

  herdrGlobal[HERDR_API] = { version: 1, runSubagent() {} };
  assert.equal(await detectDelegationBackend([], new CapabilityEvents(false), 5), "pi-herdr-subagents");
  await assert.rejects(
    detectDelegationBackend([], new CapabilityEvents(true), 5),
    /both pi-subagents and pi-herdr-subagents.*exactly one/u,
  );
  delete herdrGlobal[HERDR_API];

  await assert.rejects(
    detectDelegationBackend([], new CapabilityEvents(false), 5),
    /requires either/u,
  );
});

test("Herdr metadata only refines incompatible-version errors", async () => {
  delete herdrGlobal[HERDR_API];
  await assert.rejects(
    detectDelegationBackend([tool("pi-herdr-subagents")], new CapabilityEvents(false), 5),
    /tool metadata without the required delegation API.*0\.2\.1-orkastrator\.0/u,
  );

  herdrGlobal[HERDR_API] = { version: 0, runSubagent() {} };
  await assert.rejects(
    detectDelegationBackend([], new CapabilityEvents(false), 5),
    /delegation API version 1/u,
  );
  delete herdrGlobal[HERDR_API];
});

test("pi-subagents capability probes always clean up their correlated listener", async () => {
  for (const available of [true, false]) {
    const events = new CapabilityEvents(available);
    assert.equal(await probePiSubagents(events, 5), available);
    assert.equal(events.listenerCount("prompt-template:subagent:response"), 0);
  }
});

test("Herdr routing resolves the real global API and normalizes successful text", async () => {
  const { calls, uninstall } = installHerdr({ summary: "done", exitCode: 0, elapsed: 2 });
  const response = await delegateSubagent(spec, new AbortController().signal);
  assert.equal(response.status, "completed");
  assert.deepEqual(response.result, { kind: "text", text: "done" });
  assert.equal(response.usage?.durationMs, 2000);
  assert.equal(calls[0]?.params.agent, "worker");
  assert.equal(calls[0]?.params.fork, false);
  assert.equal(calls[0]?.options.structuredOutput, undefined);
  uninstall();
});

test("Herdr rejects missing and incompatible global delegation APIs", async () => {
  const uninstall = installDelegationBridge(new EventEmitter() as never, {}, {
    backend: "pi-herdr-subagents",
    getContext: context,
  });
  delete herdrGlobal[HERDR_API];
  await assert.rejects(delegateSubagent(spec, new AbortController().signal), /delegation API is unavailable/u);

  herdrGlobal[HERDR_API] = { version: 2, runSubagent() {} };
  await assert.rejects(delegateSubagent(spec, new AbortController().signal), /API version 1/u);

  herdrGlobal[HERDR_API] = { version: 1 };
  await assert.rejects(delegateSubagent(spec, new AbortController().signal), /runSubagent capability/u);
  delete herdrGlobal[HERDR_API];
  uninstall();
});

test("Herdr failure and cancellation retain terminal and abort semantics", async () => {
  let installed = installHerdr({ summary: "bad", exitCode: 3, elapsed: 1, error: "failed hard" });
  const failed = await delegateSubagent(spec, new AbortController().signal);
  assert.equal(failed.status, "failed");
  assert.equal(failed.error, "failed hard");
  installed.uninstall();

  const controller = new AbortController();
  installed = installHerdr({ summary: "Subagent cancelled.", exitCode: 1, elapsed: 0, error: "cancelled" }, controller);
  await assert.rejects(delegateSubagent(spec, controller.signal), (error: unknown) =>
    error instanceof DOMException && error.name === "AbortError");
  installed.uninstall();
});

test("Herdr structured success uses runner output and forwards the requested schema", async () => {
  const structured = { ...spec, result: { kind: "structured" as const, schema: {
    type: "object", required: ["ok"], properties: { ok: { type: "boolean" } }, additionalProperties: false,
  } } };
  const installed = installHerdr({
    summary: '{"ok":true}',
    structuredOutput: { ok: true },
    exitCode: 0,
    elapsed: 1,
  });
  const response = await delegateSubagent(structured, new AbortController().signal);
  assert.deepEqual(response.result, { kind: "structured", value: { ok: true } });
  assert.deepEqual(installed.calls[0]?.options.structuredOutput, { schema: structured.result.schema });
  installed.uninstall();
});

test("Herdr StructuredOutputFailure maps to structured_output_failed", async () => {
  const structured = { ...spec, result: { kind: "structured" as const, schema: { type: "boolean" } } };
  const installed = installHerdr({
    summary: "Subagent failed to return valid structured output after 2 attempt(s).",
    exitCode: 0,
    elapsed: 2,
    error: "structured_output",
    attempts: 2,
    rawResponse: "not-json",
    validationErrors: ["Invalid JSON"],
  });
  const response = await delegateSubagent(structured, new AbortController().signal);
  assert.equal(response.status, "structured_output_failed");
  assert.equal(response.error, "Invalid JSON");
  installed.uninstall();
});

test("Herdr fails clearly without captured context and uninstall invalidates delegation", async () => {
  const uninstall = installDelegationBridge(new EventEmitter() as never, {}, {
    backend: "pi-herdr-subagents",
    getContext: () => undefined,
  });
  await assert.rejects(delegateSubagent(spec, new AbortController().signal), /before Pi session context is available/u);
  uninstall();
  await assert.rejects(delegateSubagent(spec, new AbortController().signal), /bridge is not installed/u);
});

test("delegation bridge contains no Herdr TypeScript runtime import", () => {
  const source = readFileSync(new URL("../delegation-bridge.ts", import.meta.url), "utf8");
  assert.doesNotMatch(source, /import\([^)]*pi-herdr-subagents[^)]*\.ts/u);
});
