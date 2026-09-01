import { randomUUID } from "node:crypto";

import {
  createAgentSession,
  DefaultResourceLoader,
  getAgentDir,
  ModelRuntime,
  resolveCliModel,
  SessionManager,
  SettingsManager,
  type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import type {
  SubagentDelegationRequest,
  SubagentDelegationResponse,
  SubagentDelegationTerminalResponse,
} from "pi-subagents/delegation";

import { delegateWithHerdrBroker } from "./herdr-delegation-client.ts";
import type { HerdrLaunchBinding } from "./herdr-launch.ts";

type ThinkingLevel = "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
type HerdrParams = Record<string, unknown>;
type HerdrContext = Pick<ExtensionContext, "cwd" | "sessionManager" | "model" | "modelRegistry">;
type HerdrResult = {
  summary: string;
  exitCode: number;
  elapsed: number;
  error?: string;
  errorMessage?: string;
  structuredOutput?: unknown;
  attempts?: number;
  rawResponse?: string;
  validationErrors?: string[];
};

export interface HerdrPanePlacement {
  parentPaneId: string;
  stackId: string;
  firstDirection: "right" | "down";
}

interface HerdrDelegationApiV1 {
  readonly version: 1;
  readonly capabilities: {
    readonly panePlacement: true;
    readonly resourceIsolation: true;
    readonly writableRootSandbox: true;
  };
  runSubagent(
    params: HerdrParams,
    ctx: HerdrContext,
    options: {
      signal: AbortSignal;
      parentThinking: ThinkingLevel;
      structuredOutput?: { schema: Record<string, unknown> };
      placement?: HerdrPanePlacement;
      isolateResources?: boolean;
      sandboxRoot?: string;
    },
  ): Promise<HerdrResult>;
}

export const SUBAGENT_DELEGATION_REQUEST_EVENT = "prompt-template:subagent:request";
export const SUBAGENT_DELEGATION_RESPONSE_EVENT = "prompt-template:subagent:response";
export const SUBAGENT_DELEGATION_CANCEL_EVENT = "prompt-template:subagent:cancel";

const BRIDGE = Symbol.for("orkastrator.pi-subagents-delegation.v1");

export interface DelegationEvents {
  on(event: string, handler: (payload: unknown) => void): () => void;
  emit(event: string, payload: unknown): void;
}

export interface DelegationSpec extends Omit<SubagentDelegationRequest, "requestId" | "result"> {
  result: { kind: "text" } | { kind: "structured"; schema: Record<string, unknown> };
  herdrLaunch?: HerdrLaunchBinding;
  panePlacement?: HerdrPanePlacement;
}

export type DelegationBackend = "pi-subagents" | "pi-herdr-subagents";

export interface ToolSourceMetadata {
  name: string;
  sourceInfo: { path: string; source: string };
}

const HERDR_DELEGATION_API = Symbol.for("pi-herdr-subagents/delegation.v1");

type HerdrGlobal = typeof globalThis & { [HERDR_DELEGATION_API]?: unknown };

interface InstalledBridge {
  events: DelegationEvents;
  owner: object;
  backend: DelegationBackend;
  getContext?: () => ExtensionContext | undefined;
}

type BridgeGlobal = typeof globalThis & { [BRIDGE]?: InstalledBridge };

function sourceNames(tool: ToolSourceMetadata): string {
  return `${tool.sourceInfo.source}\n${tool.sourceInfo.path}`.replaceAll("\\", "/");
}

function herdrMetadataPresent(tools: ToolSourceMetadata[]): boolean {
  return tools.some((tool) => /(^|[/@:])pi-herdr-subagents([/@:]|$)/u.test(sourceNames(tool)));
}

function inspectHerdrApi(): { available: boolean; error?: Error } {
  const api = (globalThis as HerdrGlobal)[HERDR_DELEGATION_API];
  if (api === undefined) return { available: false };
  if (typeof api !== "object" || api === null || (api as { version?: unknown }).version !== 1) {
    return { available: false, error: new Error("Orkastrator requires pi-herdr-subagents delegation API version 1") };
  }
  if (typeof (api as { runSubagent?: unknown }).runSubagent !== "function") {
    return { available: false, error: new Error("Orkastrator requires the pi-herdr-subagents awaitable runSubagent capability") };
  }
  const capabilities = (api as { capabilities?: Record<string, unknown> }).capabilities;
  if (
    capabilities?.panePlacement !== true
    || capabilities.resourceIsolation !== true
    || capabilities.writableRootSandbox !== true
  ) {
    return {
      available: false,
      error: new Error(
        "Orkastrator requires pi-herdr-subagents pane placement, resource isolation, and writable-root sandbox capabilities",
      ),
    };
  }
  return { available: true };
}

/** Probe pi-subagents with an invalid correlated request that cannot launch a child. */
export async function probePiSubagents(
  events: DelegationEvents,
  timeoutMs = 25,
): Promise<boolean> {
  const requestId = `orkastrator-probe-${randomUUID()}`;
  const ownerRunId = `orkastrator-probe-owner-${randomUUID()}`;
  const nodeId = `orkastrator-probe-node-${randomUUID()}`;
  return await new Promise<boolean>((resolve) => {
    let settled = false;
    let unsubscribe: () => void;
    const finish = (available: boolean): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      unsubscribe?.();
      resolve(available);
    };
    unsubscribe = events.on(SUBAGENT_DELEGATION_RESPONSE_EVENT, (payload) => {
      if (!payload || typeof payload !== "object") return;
      const response = payload as Partial<SubagentDelegationResponse>;
      if (response.requestId !== requestId || response.ownerRunId !== ownerRunId || response.nodeId !== nodeId) return;
      if (response.status === "invalid_request") finish(true);
    });
    const timer = setTimeout(() => finish(false), timeoutMs);
    events.emit(SUBAGENT_DELEGATION_REQUEST_EVENT, {
      requestId,
      ownerRunId,
      nodeId,
      result: { kind: "structured", schema: {} },
      orkastratorCapabilityProbe: true,
    });
  });
}

/** Detect non-collapsing delegation capabilities, not duplicate tool names. */
export async function detectDelegationBackend(
  tools: ToolSourceMetadata[],
  events: DelegationEvents,
  probeTimeoutMs = 25,
): Promise<DelegationBackend> {
  const [hasPiSubagents, herdr] = await Promise.all([
    probePiSubagents(events, probeTimeoutMs),
    Promise.resolve(inspectHerdrApi()),
  ]);
  if (hasPiSubagents && herdr.available) {
    throw new Error(
      "Orkastrator found both pi-subagents and pi-herdr-subagents. Install exactly one delegation backend.",
    );
  }
  if (herdr.error) throw herdr.error;
  if (herdr.available) return "pi-herdr-subagents";
  if (hasPiSubagents) return "pi-subagents";
  if (herdrMetadataPresent(tools)) {
    throw new Error(
      "Orkastrator found pi-herdr-subagents tool metadata without the required delegation API; install version 0.2.1-orkastrator.1",
    );
  }
  throw new Error("Orkastrator requires either pi-subagents or pi-herdr-subagents");
}

/** Install the extension-to-extension transport used by workflow action nodes. */
export function installDelegationBridge(
  events: DelegationEvents,
  owner: object,
  options: {
    backend?: DelegationBackend;
    getContext?: () => ExtensionContext | undefined;
  } = {},
): () => void {
  const target = globalThis as BridgeGlobal;
  const installed: InstalledBridge = {
    events,
    owner,
    backend: options.backend ?? "pi-subagents",
    ...(options.getContext ? { getContext: options.getContext } : {}),
  };
  target[BRIDGE] = installed;
  return () => {
    if (target[BRIDGE] === installed) delete target[BRIDGE];
  };
}

function aborted(): DOMException {
  return new DOMException("Subagent delegation aborted", "AbortError");
}

async function delegateWithEvents(
  bridge: InstalledBridge,
  spec: DelegationSpec,
  signal: AbortSignal,
): Promise<SubagentDelegationTerminalResponse> {
  const { herdrLaunch: _binding, panePlacement: _placement, ...eventSpec } = spec;
  const request: SubagentDelegationRequest = { ...eventSpec, requestId: randomUUID() };
  return await new Promise<SubagentDelegationTerminalResponse>((resolve, reject) => {
    let settled = false;
    const finish = (outcome: { response: SubagentDelegationTerminalResponse } | { error: Error }): void => {
      if (settled) return;
      settled = true;
      unsubscribe?.();
      signal.removeEventListener("abort", abort);
      if ("response" in outcome) resolve(outcome.response);
      else reject(outcome.error);
    };
    const abort = (): void => {
      bridge.events.emit(SUBAGENT_DELEGATION_CANCEL_EVENT, {
        requestId: request.requestId,
        ownerRunId: request.ownerRunId,
        nodeId: request.nodeId,
      });
      finish({ error: aborted() });
    };
    const unsubscribe = bridge.events.on(SUBAGENT_DELEGATION_RESPONSE_EVENT, (payload) => {
      const response = payload as SubagentDelegationResponse;
      if (response.requestId !== request.requestId) return;
      if (response.ownerRunId !== request.ownerRunId || response.nodeId !== request.nodeId) return;
      if (response.status === "invalid_request") {
        finish({ error: new Error(response.error ?? "pi-subagents rejected delegation") });
        return;
      }
      finish({ response });
    });
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) return abort();
    bridge.events.emit(SUBAGENT_DELEGATION_REQUEST_EVENT, request);
  });
}

function herdrThinking(value: DelegationSpec["thinking"], fallback: string | undefined): ThinkingLevel {
  const selected = value ?? fallback ?? "medium";
  return selected === "off" ? "minimal" : selected as ThinkingLevel;
}

function requireHerdrApi(): HerdrDelegationApiV1 {
  const inspected = inspectHerdrApi();
  if (inspected.error) throw inspected.error;
  if (!inspected.available) {
    throw new Error("Orkastrator found pi-herdr-subagents, but its delegation API is unavailable");
  }
  return (globalThis as HerdrGlobal)[HERDR_DELEGATION_API] as HerdrDelegationApiV1;
}

async function delegateWithHerdr(
  bridge: InstalledBridge,
  spec: DelegationSpec,
  signal: AbortSignal,
): Promise<SubagentDelegationTerminalResponse> {
  const ctx = bridge.getContext?.();
  if (ctx === undefined) throw new Error("Orkastrator cannot delegate through Herdr before Pi session context is available");
  const api = requireHerdrApi();
  const params: HerdrParams = {
    name: `${spec.agent}:${spec.nodeId}`,
    agent: spec.agent,
    task: spec.task,
    cwd: spec.cwd,
    fork: spec.context === "fork",
    ...(spec.model ? { model: spec.model } : {}),
    ...(spec.thinking ? { thinking: spec.thinking === "off" ? "minimal" : spec.thinking } : {}),
    tools: spec.agent === "reviewer"
      ? "read,grep,find,ls"
      : "read,bash,edit,write,grep,find,ls",
    interactive: false,
  };
  const result = await api.runSubagent(
    params,
    ctx,
    {
      signal,
      parentThinking: herdrThinking(spec.thinking, ctx.thinkingLevel),
      ...(spec.result.kind === "structured"
        ? { structuredOutput: { schema: spec.result.schema } }
        : {}),
      ...(spec.panePlacement === undefined ? {} : { placement: spec.panePlacement }),
      isolateResources: true,
      ...(spec.agent === "worker" ? { sandboxRoot: spec.cwd } : {}),
    },
  );

  if (signal.aborted || result.error === "cancelled") throw aborted();
  const response: SubagentDelegationTerminalResponse = {
    requestId: randomUUID(),
    ownerRunId: spec.ownerRunId,
    nodeId: spec.nodeId,
    status: result.exitCode === 0 && result.error === undefined ? "completed" : "failed",
    agent: spec.agent,
    exitCode: result.exitCode,
    usage: {
      input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, turns: 0, toolCalls: 0,
      durationMs: result.elapsed * 1000,
    },
  };
  if (result.error === "structured_output") {
    return {
      ...response,
      status: "structured_output_failed",
      error: result.validationErrors?.join("; ")
        ?? `Herdr failed to return structured output after ${result.attempts ?? 0} attempt(s)`,
    };
  }
  if (response.status === "failed") {
    response.error = result.errorMessage ?? result.error ?? result.summary;
    return response;
  }
  if (spec.result.kind === "structured") {
    if (!("structuredOutput" in result)) {
      return { ...response, status: "structured_output_failed", error: "Herdr returned no structured output" };
    }
    response.result = { kind: "structured", value: result.structuredOutput };
    return response;
  }
  response.result = { kind: "text", text: result.summary };
  return response;
}

function assistantText(messages: unknown[]): string | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message === null || typeof message !== "object" || Array.isArray(message)) continue;
    const record = message as Record<string, unknown>;
    if (record.role !== "assistant" || !Array.isArray(record.content)) continue;
    const text = record.content.flatMap((part) => {
      if (part === null || typeof part !== "object" || Array.isArray(part)) return [];
      const value = part as Record<string, unknown>;
      return value.type === "text" && typeof value.text === "string" ? [value.text] : [];
    }).join("").trim();
    if (text.length > 0) return text;
  }
  return undefined;
}

class StructuredOutputError extends Error {}

function parseStructuredText(text: string): unknown {
  const trimmed = text.trim();
  const fenced = /^```(?:json)?\s*([\s\S]*?)\s*```$/iu.exec(trimmed)?.[1] ?? trimmed;
  try {
    return JSON.parse(fenced);
  } catch {
    throw new StructuredOutputError("Pi child returned invalid structured JSON");
  }
}

async function delegateWithPiSdk(
  spec: DelegationSpec,
  signal: AbortSignal,
): Promise<SubagentDelegationTerminalResponse> {
  const startedAt = Date.now();
  const agentDir = getAgentDir();
  const settingsManager = SettingsManager.create(spec.cwd, agentDir);
  const resourceLoader = new DefaultResourceLoader({
    cwd: spec.cwd,
    agentDir,
    settingsManager,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
  });
  await resourceLoader.reload();
  const modelRuntime = await ModelRuntime.create({ signal });
  const resolvedModel = spec.model === undefined
    ? undefined
    : resolveCliModel({ cliModel: spec.model, modelRuntime });
  if (resolvedModel?.error) throw new Error(resolvedModel.error);
  const model = resolvedModel?.model;
  const { session } = await createAgentSession({
    cwd: spec.cwd,
    agentDir,
    resourceLoader,
    settingsManager,
    sessionManager: SessionManager.inMemory(spec.cwd),
    modelRuntime,
    ...(model === undefined ? {} : { model }),
    ...(spec.thinking === undefined ? {} : { thinkingLevel: spec.thinking }),
    tools: spec.agent === "reviewer"
      ? ["read", "grep", "find", "ls"]
      : ["read", "bash", "edit", "write", "grep", "find", "ls"],
  });
  const abort = () => void session.abort();
  signal.addEventListener("abort", abort, { once: true });
  try {
    if (signal.aborted) throw aborted();
    await session.prompt(spec.task);
    if (signal.aborted) throw aborted();
    const error = session.agent.state.errorMessage;
    if (typeof error === "string" && error.length > 0) throw new Error(error);
    const text = assistantText(session.messages);
    if (text === undefined) throw new Error("Pi child returned no final assistant text");
    const response: SubagentDelegationTerminalResponse = {
      requestId: randomUUID(),
      ownerRunId: spec.ownerRunId,
      nodeId: spec.nodeId,
      status: "completed",
      agent: spec.agent,
      exitCode: 0,
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        cost: 0,
        turns: 0,
        toolCalls: 0,
        durationMs: Date.now() - startedAt,
      },
      result: spec.result.kind === "structured"
        ? { kind: "structured", value: parseStructuredText(text) }
        : { kind: "text", text },
    };
    return response;
  } catch (error) {
    if (signal.aborted) throw aborted();
    return {
      requestId: randomUUID(),
      ownerRunId: spec.ownerRunId,
      nodeId: spec.nodeId,
      status: error instanceof StructuredOutputError ? "structured_output_failed" : "failed",
      agent: spec.agent,
      exitCode: 1,
      error: error instanceof Error ? error.message : String(error),
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        cost: 0,
        turns: 0,
        toolCalls: 0,
        durationMs: Date.now() - startedAt,
      },
    };
  } finally {
    signal.removeEventListener("abort", abort);
    await session.dispose();
  }
}

/** Run one configured delegation leaf through its bound interactive session or a hosted Pi SDK child. */
export async function delegateSubagent(
  spec: DelegationSpec,
  signal: AbortSignal,
  hostedRunner: (
    spec: DelegationSpec,
    signal: AbortSignal,
  ) => Promise<SubagentDelegationTerminalResponse> = delegateWithPiSdk,
): Promise<SubagentDelegationTerminalResponse> {
  const bridge = (globalThis as BridgeGlobal)[BRIDGE];
  if (bridge === undefined) {
    if (spec.herdrLaunch !== undefined) {
      return await delegateWithHerdrBroker(spec, spec.herdrLaunch, signal);
    }
    return await hostedRunner(spec, signal);
  }
  return bridge.backend === "pi-herdr-subagents"
    ? delegateWithHerdr(bridge, spec, signal)
    : delegateWithEvents(bridge, spec, signal);
}
