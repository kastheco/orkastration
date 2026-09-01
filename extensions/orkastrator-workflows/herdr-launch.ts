import { tmpdir } from "node:os";
import { join } from "node:path";

const LAUNCH_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;

export interface HerdrLaunchBinding {
  version: 1;
  transport: "unix";
  launchId: string;
}

export interface HerdrBrokerDescriptor {
  version: 1;
  launchId: string;
  socketPath: string;
  capability: string;
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

export function parseHerdrLaunchBinding(value: unknown, label = "Herdr launch binding"): HerdrLaunchBinding {
  const input = requireRecord(value, label);
  const keys = Object.keys(input).sort();
  if (keys.join("\0") !== ["launchId", "transport", "version"].join("\0")) {
    throw new Error(`${label} has unknown or missing fields`);
  }
  if (input.version !== 1 || input.transport !== "unix") {
    throw new Error(`${label} must use version 1 Unix transport`);
  }
  if (typeof input.launchId !== "string" || !LAUNCH_ID.test(input.launchId)) {
    throw new Error(`${label} launchId must be a lowercase UUID v4`);
  }
  return { version: 1, transport: "unix", launchId: input.launchId };
}

export function parseOptionalHerdrLaunchBinding(
  value: unknown,
  label?: string,
): HerdrLaunchBinding | undefined {
  return value === undefined ? undefined : parseHerdrLaunchBinding(value, label);
}

export function herdrBrokerRuntimeDirectory(env: NodeJS.ProcessEnv = process.env): string {
  const uid = typeof process.getuid === "function" ? process.getuid() : "user";
  return join(env.XDG_RUNTIME_DIR ?? tmpdir(), `orkastrator-${uid}`);
}

export function herdrBrokerDescriptorPath(
  launchId: string,
  env: NodeJS.ProcessEnv = process.env,
): string {
  if (!LAUNCH_ID.test(launchId)) throw new Error("Herdr launch descriptor requires a valid launchId");
  return join(herdrBrokerRuntimeDirectory(env), `launch-${launchId}.json`);
}

export function parseHerdrBrokerDescriptor(value: unknown): HerdrBrokerDescriptor {
  const input = requireRecord(value, "Herdr broker descriptor");
  const keys = Object.keys(input).sort();
  if (keys.join("\0") !== ["capability", "launchId", "socketPath", "version"].join("\0")) {
    throw new Error("Herdr broker descriptor has unknown or missing fields");
  }
  if (input.version !== 1) throw new Error("Herdr broker descriptor must use version 1");
  if (typeof input.launchId !== "string" || !LAUNCH_ID.test(input.launchId)) {
    throw new Error("Herdr broker descriptor launchId is invalid");
  }
  if (typeof input.socketPath !== "string" || input.socketPath.length === 0 || input.socketPath.length > 100) {
    throw new Error("Herdr broker descriptor socketPath is invalid");
  }
  if (typeof input.capability !== "string" || !/^[A-Za-z0-9_-]{32,128}$/u.test(input.capability)) {
    throw new Error("Herdr broker descriptor capability is invalid");
  }
  return {
    version: 1,
    launchId: input.launchId,
    socketPath: input.socketPath,
    capability: input.capability,
  };
}
