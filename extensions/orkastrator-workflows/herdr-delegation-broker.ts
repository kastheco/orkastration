import { createHash, randomBytes, randomUUID } from "node:crypto";
import { chmod, mkdir, rename, unlink, writeFile } from "node:fs/promises";
import { createServer, type Server, type Socket } from "node:net";
import { isAbsolute, join } from "node:path";

import {
  delegateSubagent,
  type DelegationSpec,
} from "./delegation-bridge.ts";
import {
  herdrBrokerDescriptorPath,
  herdrBrokerRuntimeDirectory,
  type HerdrBrokerDescriptor,
  type HerdrLaunchBinding,
} from "./herdr-launch.ts";

const MAX_FRAME_BYTES = 1_048_576;
const THINKING_LEVELS = new Set(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);

type LaunchRecord = {
  binding: HerdrLaunchBinding;
  capability: string;
  rootPaneId: string;
  runIds: Set<string>;
  active: Map<string, { controller: AbortController; socket: Socket }>;
  accepting: boolean;
};

type DelegateEnvelope = {
  version: 1;
  type: "delegate";
  launchId: string;
  capability: string;
  requestId: string;
  spec: DelegationSpec;
};

export interface SessionDelegationBrokerOptions {
  sessionId: string;
  env?: NodeJS.ProcessEnv;
  run?: typeof delegateSubagent;
  continuationRunIds?: (parentRunId: string) => string[];
}

export class SessionDelegationBroker {
  readonly socketPath: string;

  private readonly env: NodeJS.ProcessEnv;
  private readonly run: typeof delegateSubagent;
  private readonly continuationRunIds: (parentRunId: string) => string[];
  private readonly launches = new Map<string, LaunchRecord>();
  private readonly sockets = new Set<Socket>();
  private server: Server | undefined;
  private closing = false;

  constructor(options: SessionDelegationBrokerOptions) {
    this.env = options.env ?? process.env;
    this.run = options.run ?? delegateSubagent;
    this.continuationRunIds = options.continuationRunIds ?? (() => []);
    const digest = createHash("sha256").update(options.sessionId).digest("hex").slice(0, 12);
    const nonce = randomBytes(6).toString("hex");
    this.socketPath = join(herdrBrokerRuntimeDirectory(this.env), `broker-${digest}-${nonce}.sock`);
  }

  async start(): Promise<void> {
    if (this.server !== undefined) return;
    if (this.closing) throw new Error("Herdr delegation broker is closing");
    await mkdir(herdrBrokerRuntimeDirectory(this.env), { recursive: true, mode: 0o700 });
    await chmod(herdrBrokerRuntimeDirectory(this.env), 0o700);
    const server = createServer((socket) => this.accept(socket));
    await new Promise<void>((resolve, reject) => {
      const fail = (error: Error): void => reject(error);
      server.once("error", fail);
      server.listen(this.socketPath, () => {
        server.off("error", fail);
        resolve();
      });
    });
    await chmod(this.socketPath, 0o600);
    this.server = server;
  }

  async registerLaunch(rootPaneId: string): Promise<HerdrLaunchBinding> {
    const server = this.server;
    if (server === undefined || this.closing) {
      throw new Error("Herdr delegation broker is unavailable");
    }
    if (rootPaneId.trim().length === 0) throw new Error("Herdr workflow root pane is required");
    const binding: HerdrLaunchBinding = {
      version: 1,
      transport: "unix",
      launchId: randomUUID(),
    };
    const capability = randomBytes(32).toString("base64url");
    this.launches.set(binding.launchId, {
      binding,
      capability,
      rootPaneId,
      runIds: new Set(),
      active: new Map(),
      accepting: true,
    });
    const descriptor: HerdrBrokerDescriptor = {
      version: 1,
      launchId: binding.launchId,
      socketPath: this.socketPath,
      capability,
    };
    const destination = herdrBrokerDescriptorPath(binding.launchId, this.env);
    const temporary = `${destination}.${process.pid}.${randomBytes(4).toString("hex")}.tmp`;
    try {
      await writeFile(temporary, `${JSON.stringify(descriptor)}\n`, { mode: 0o600, flag: "wx" });
      await rename(temporary, destination);
      await chmod(destination, 0o600);
      if (
        this.closing
        || this.server !== server
        || !this.launches.has(binding.launchId)
      ) {
        await this.removeDescriptor(binding.launchId);
        throw new Error("Herdr delegation broker closed during launch registration");
      }
      return binding;
    } catch (error) {
      this.launches.delete(binding.launchId);
      await unlink(temporary).catch(() => undefined);
      await this.removeDescriptor(binding.launchId);
      throw error;
    }
  }

  bindRun(launchId: string, runId: string): void {
    const launch = this.requireLaunch(launchId);
    if (runId.trim().length === 0) throw new Error("Herdr launch runId is required");
    if (launch.runIds.size > 0 && !launch.runIds.has(runId)) {
      throw new Error(`Herdr launch ${launchId} is already bound to another workflow lineage`);
    }
    launch.runIds.add(runId);
  }

  bindContinuation(launchId: string, parentRunId: string, continuationRunId: string): void {
    const launch = this.requireLaunch(launchId);
    if (!launch.runIds.has(parentRunId)) {
      throw new Error(`Herdr continuation parent ${parentRunId} is outside the bound workflow lineage`);
    }
    if (continuationRunId.trim().length === 0) {
      throw new Error("Herdr continuation runId is required");
    }
    launch.runIds.add(continuationRunId);
  }

  cancelRun(runId: string): void {
    for (const launch of this.launches.values()) {
      if (!launch.runIds.has(runId)) continue;
      launch.accepting = false;
      for (const active of launch.active.values()) {
        active.controller.abort();
        active.socket.destroy();
      }
      void this.removeDescriptor(launch.binding.launchId);
    }
  }

  async releaseLaunch(launchId: string): Promise<void> {
    const launch = this.launches.get(launchId);
    if (launch === undefined) return;
    launch.accepting = false;
    this.launches.delete(launchId);
    for (const active of launch.active.values()) {
      active.controller.abort();
      active.socket.destroy();
    }
    await this.removeDescriptor(launchId);
  }

  async close(): Promise<void> {
    this.closing = true;
    const launchIds = [...this.launches.keys()];
    await Promise.all(launchIds.map(async (launchId) => await this.releaseLaunch(launchId)));
    const server = this.server;
    this.server = undefined;
    for (const socket of this.sockets) socket.destroy();
    this.sockets.clear();
    if (server !== undefined) {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
    await unlink(this.socketPath).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== "ENOENT") throw error;
    });
  }

  private accept(socket: Socket): void {
    let bytes = 0;
    let input = "";
    let handled = false;
    this.sockets.add(socket);
    const frameTimeout = setTimeout(() => {
      if (handled) return;
      handled = true;
      this.writeError(socket, undefined, "Herdr broker request frame timed out");
    }, 10_000);
    frameTimeout.unref();
    socket.setEncoding("utf8");
    socket.on("data", (chunk: string) => {
      if (handled) return;
      bytes += Buffer.byteLength(chunk);
      if (bytes > MAX_FRAME_BYTES) {
        handled = true;
        clearTimeout(frameTimeout);
        this.writeError(socket, undefined, "Herdr broker request exceeded the frame limit");
        return;
      }
      input += chunk;
      const newline = input.indexOf("\n");
      if (newline === -1) return;
      handled = true;
      clearTimeout(frameTimeout);
      const frame = input.slice(0, newline);
      if (input.slice(newline + 1).trim().length > 0) {
        this.writeError(socket, undefined, "Herdr broker accepts one request per connection");
        return;
      }
      void this.handleFrame(socket, frame);
    });
    socket.on("error", () => undefined);
    socket.once("close", () => {
      clearTimeout(frameTimeout);
      this.sockets.delete(socket);
    });
  }

  private async handleFrame(socket: Socket, frame: string): Promise<void> {
    let envelope: DelegateEnvelope;
    try {
      envelope = parseDelegateEnvelope(frame);
    } catch (error) {
      this.writeError(socket, undefined, errorMessage(error));
      return;
    }
    const launch = this.launches.get(envelope.launchId);
    if (launch === undefined || launch.capability !== envelope.capability || !launch.accepting) {
      this.writeError(socket, envelope.requestId, "Herdr launch is unavailable or unauthorized");
      return;
    }
    if (launch.runIds.size > 0 && !launch.runIds.has(envelope.spec.ownerRunId)) {
      try {
        for (const parentRunId of [...launch.runIds]) {
          for (const continuationRunId of this.continuationRunIds(parentRunId)) {
            launch.runIds.add(continuationRunId);
          }
        }
      } catch {
        this.writeError(socket, envelope.requestId, "Herdr workflow lineage could not be verified");
        return;
      }
    }
    if (launch.runIds.size > 0 && !launch.runIds.has(envelope.spec.ownerRunId)) {
      this.writeError(socket, envelope.requestId, "Herdr launch belongs to another workflow lineage");
      return;
    }
    if (launch.runIds.size === 0) launch.runIds.add(envelope.spec.ownerRunId);
    if (launch.active.has(envelope.requestId)) {
      this.writeError(socket, envelope.requestId, "Herdr delegation requestId is already active");
      return;
    }

    const controller = new AbortController();
    launch.active.set(envelope.requestId, { controller, socket });
    const timeout = envelope.spec.timeoutMs === undefined
      ? undefined
      : setTimeout(() => controller.abort(), envelope.spec.timeoutMs);
    let terminal = false;
    const disconnected = (): void => {
      if (!terminal) controller.abort();
    };
    socket.once("close", disconnected);
    try {
      const { herdrLaunch: _binding, panePlacement: _placement, ...delegation } = envelope.spec;
      const response = await this.run({
        ...delegation,
        panePlacement: {
          parentPaneId: launch.rootPaneId,
          stackId: launch.binding.launchId,
          firstDirection: "right",
        },
      }, controller.signal);
      terminal = true;
      this.write(socket, {
        version: 1,
        type: "result",
        requestId: envelope.requestId,
        response,
      });
    } catch (error) {
      terminal = true;
      this.writeError(socket, envelope.requestId, errorMessage(error));
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
      socket.off("close", disconnected);
      launch.active.delete(envelope.requestId);
    }
  }

  private writeError(socket: Socket, requestId: string | undefined, error: string): void {
    this.write(socket, {
      version: 1,
      type: "error",
      ...(requestId === undefined ? {} : { requestId }),
      error,
    });
  }

  private write(socket: Socket, value: unknown): void {
    if (socket.destroyed) return;
    const frame = `${JSON.stringify(value)}\n`;
    if (Buffer.byteLength(frame) > MAX_FRAME_BYTES) {
      const requestId = value !== null && typeof value === "object" && !Array.isArray(value)
        ? (value as { requestId?: unknown }).requestId
        : undefined;
      socket.end(`${JSON.stringify({
        version: 1,
        type: "error",
        ...(typeof requestId === "string" ? { requestId } : {}),
        error: "Herdr broker response exceeded the frame limit",
      })}\n`);
      return;
    }
    socket.end(frame);
  }

  private requireLaunch(launchId: string): LaunchRecord {
    const launch = this.launches.get(launchId);
    if (launch === undefined) throw new Error(`Unknown Herdr launch ${launchId}`);
    return launch;
  }

  private async removeDescriptor(launchId: string): Promise<void> {
    await unlink(herdrBrokerDescriptorPath(launchId, this.env)).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== "ENOENT") throw error;
    });
  }
}

function parseDelegateEnvelope(frame: string): DelegateEnvelope {
  let value: unknown;
  try {
    value = JSON.parse(frame);
  } catch {
    throw new Error("Herdr broker request must be valid JSON");
  }
  const input = requireRecord(value, "Herdr broker request");
  const unexpected = Object.keys(input).find(
    (key) => !["version", "type", "launchId", "capability", "requestId", "spec"].includes(key),
  );
  if (unexpected !== undefined) throw new Error(`Herdr broker request has unsupported field ${unexpected}`);
  if (input.version !== 1 || input.type !== "delegate") {
    throw new Error("Herdr broker request must use delegate protocol version 1");
  }
  for (const field of ["launchId", "capability", "requestId"] as const) {
    if (typeof input[field] !== "string" || input[field].length === 0) {
      throw new Error(`Herdr broker request ${field} is required`);
    }
  }
  const spec = parseDelegationSpec(input.spec);
  return {
    version: 1,
    type: "delegate",
    launchId: input.launchId as string,
    capability: input.capability as string,
    requestId: input.requestId as string,
    spec,
  };
}

function parseDelegationSpec(value: unknown): DelegationSpec {
  const input = requireRecord(value, "Herdr delegation spec");
  const allowed = new Set([
    "ownerRunId",
    "nodeId",
    "agent",
    "task",
    "context",
    "cwd",
    "model",
    "thinking",
    "timeoutMs",
    "toolBudget",
    "artifacts",
    "result",
  ]);
  const unexpected = Object.keys(input).find((key) => !allowed.has(key));
  if (unexpected !== undefined) throw new Error(`Herdr delegation has unsupported field ${unexpected}`);
  for (const field of ["ownerRunId", "nodeId", "task", "cwd"] as const) {
    if (typeof input[field] !== "string" || input[field].trim().length === 0) {
      throw new Error(`Herdr delegation ${field} is required`);
    }
  }
  if (!isAbsolute(input.cwd as string)) throw new Error("Herdr delegation cwd must be absolute");
  if (input.agent !== "reviewer" && input.agent !== "worker") {
    throw new Error("Herdr delegation agent must be reviewer or worker");
  }
  if (input.context !== "fresh") throw new Error("Herdr delegation context must be fresh");
  if (input.thinking !== undefined && (typeof input.thinking !== "string" || !THINKING_LEVELS.has(input.thinking))) {
    throw new Error("Herdr delegation thinking level is invalid");
  }
  if (input.model !== undefined && (typeof input.model !== "string" || input.model.length > 256)) {
    throw new Error("Herdr delegation model is invalid");
  }
  if (
    input.timeoutMs !== undefined
    && (!Number.isSafeInteger(input.timeoutMs) || (input.timeoutMs as number) < 1 || (input.timeoutMs as number) > 4 * 60 * 60_000)
  ) {
    throw new Error("Herdr delegation timeoutMs must be an integer from 1 to 14400000");
  }
  const parsedToolBudget = parseToolBudget(input.toolBudget);
  const result = requireRecord(input.result, "Herdr delegation result");
  let parsedResult: DelegationSpec["result"];
  if (result.kind === "text") parsedResult = { kind: "text" };
  else if (result.kind === "structured") {
    parsedResult = { kind: "structured", schema: requireRecord(result.schema, "Herdr delegation result schema") };
  } else {
    throw new Error("Herdr delegation result kind must be text or structured");
  }
  const model = typeof input.model === "string" ? input.model : undefined;
  const thinking = typeof input.thinking === "string"
    ? input.thinking as NonNullable<DelegationSpec["thinking"]>
    : undefined;
  const timeoutMs = typeof input.timeoutMs === "number" ? input.timeoutMs : undefined;
  const toolBudget = parsedToolBudget;
  return {
    ownerRunId: input.ownerRunId as string,
    nodeId: input.nodeId as string,
    agent: input.agent as "reviewer" | "worker",
    task: input.task as string,
    context: "fresh",
    cwd: input.cwd as string,
    ...(model === undefined ? {} : { model }),
    ...(thinking === undefined ? {} : { thinking }),
    ...(timeoutMs === undefined ? {} : { timeoutMs }),
    ...(toolBudget === undefined ? {} : { toolBudget }),
    ...(typeof input.artifacts === "boolean" ? { artifacts: input.artifacts } : {}),
    result: parsedResult,
  };
}

function parseToolBudget(value: unknown): NonNullable<DelegationSpec["toolBudget"]> | undefined {
  if (value === undefined) return undefined;
  const budget = requireRecord(value, "Herdr delegation toolBudget");
  const unexpected = Object.keys(budget).find((key) => key !== "soft" && key !== "hard");
  if (unexpected !== undefined) throw new Error(`Herdr delegation toolBudget has unsupported field ${unexpected}`);
  if (!Number.isSafeInteger(budget.hard) || (budget.hard as number) < 1 || (budget.hard as number) > 10_000) {
    throw new Error("Herdr delegation toolBudget hard must be an integer from 1 to 10000");
  }
  if (
    budget.soft !== undefined
    && (!Number.isSafeInteger(budget.soft) || (budget.soft as number) < 0 || (budget.soft as number) > (budget.hard as number))
  ) {
    throw new Error("Herdr delegation toolBudget soft must be an integer no greater than hard");
  }
  return {
    hard: budget.hard as number,
    ...(budget.soft === undefined ? {} : { soft: budget.soft as number }),
  };
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function errorMessage(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).slice(0, 4_000);
}

export const __brokerTest__ = {
  parseDelegateEnvelope,
  parseDelegationSpec,
};
