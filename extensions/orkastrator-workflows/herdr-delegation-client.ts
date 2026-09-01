import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createConnection, type Socket } from "node:net";

import type { SubagentDelegationTerminalResponse } from "pi-subagents/delegation";

import type { DelegationSpec } from "./delegation-bridge.ts";
import {
  herdrBrokerDescriptorPath,
  parseHerdrBrokerDescriptor,
  type HerdrLaunchBinding,
} from "./herdr-launch.ts";

const MAX_FRAME_BYTES = 1_048_576;

export async function delegateWithHerdrBroker(
  spec: DelegationSpec,
  binding: HerdrLaunchBinding,
  signal: AbortSignal,
  env: NodeJS.ProcessEnv = process.env,
): Promise<SubagentDelegationTerminalResponse> {
  const descriptor = await readDescriptor(binding, env);
  const requestId = randomUUID();
  return await new Promise<SubagentDelegationTerminalResponse>((resolve, reject) => {
    let settled = false;
    let bytes = 0;
    let input = "";
    const socket = createConnection(descriptor.socketPath);
    const finish = (
      outcome: { response: SubagentDelegationTerminalResponse } | { error: Error },
    ): void => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      socket.removeAllListeners();
      socket.destroy();
      if ("response" in outcome) resolve(outcome.response);
      else reject(outcome.error);
    };
    const abort = (): void => finish({ error: aborted() });
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) {
      abort();
      return;
    }
    socket.setEncoding("utf8");
    socket.once("connect", () => {
      const { herdrLaunch: _binding, panePlacement: _placement, ...wireSpec } = spec;
      socket.write(`${JSON.stringify({
        version: 1,
        type: "delegate",
        launchId: binding.launchId,
        capability: descriptor.capability,
        requestId,
        spec: wireSpec,
      })}\n`);
    });
    socket.on("data", (chunk: string) => {
      bytes += Buffer.byteLength(chunk);
      if (bytes > MAX_FRAME_BYTES) {
        finish({ error: new Error("Herdr broker response exceeded the frame limit") });
        return;
      }
      input += chunk;
      const newline = input.indexOf("\n");
      if (newline === -1) return;
      try {
        const envelope = parseResponse(input.slice(0, newline), requestId);
        if (envelope.type === "error") finish({ error: new Error(envelope.error) });
        else finish({ response: envelope.response });
      } catch (error) {
        finish({ error: error instanceof Error ? error : new Error(String(error)) });
      }
    });
    socket.once("error", (error) => {
      finish({ error: new Error(`Herdr delegation broker connection failed: ${error.message}`) });
    });
    socket.once("close", () => {
      if (!settled) finish({ error: new Error("Herdr delegation broker disconnected before returning a result") });
    });
  });
}

async function readDescriptor(binding: HerdrLaunchBinding, env: NodeJS.ProcessEnv) {
  let raw: string;
  try {
    raw = await readFile(herdrBrokerDescriptorPath(binding.launchId, env), "utf8");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(`Herdr launch ${binding.launchId} is unavailable; the originating session may have closed (${message})`);
  }
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error(`Herdr launch ${binding.launchId} has an invalid broker descriptor`);
  }
  const descriptor = parseHerdrBrokerDescriptor(value);
  if (descriptor.launchId !== binding.launchId) {
    throw new Error(`Herdr launch ${binding.launchId} broker descriptor does not match the workflow binding`);
  }
  return descriptor;
}

function parseResponse(frame: string, requestId: string):
  | { type: "result"; response: SubagentDelegationTerminalResponse }
  | { type: "error"; error: string } {
  let value: unknown;
  try {
    value = JSON.parse(frame);
  } catch {
    throw new Error("Herdr delegation broker returned invalid JSON");
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Herdr delegation broker returned an invalid envelope");
  }
  const envelope = value as Record<string, unknown>;
  if (envelope.version !== 1 || envelope.requestId !== requestId) {
    throw new Error("Herdr delegation broker returned a mismatched response");
  }
  if (envelope.type === "error" && typeof envelope.error === "string") {
    return { type: "error", error: envelope.error };
  }
  if (envelope.type !== "result" || envelope.response === null || typeof envelope.response !== "object") {
    throw new Error("Herdr delegation broker returned an invalid terminal result");
  }
  return { type: "result", response: envelope.response as SubagentDelegationTerminalResponse };
}

function aborted(): DOMException {
  return new DOMException("Subagent delegation aborted", "AbortError");
}

export const __clientTest__ = { parseResponse };
