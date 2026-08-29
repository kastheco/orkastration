import { spawn } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, isAbsolute } from "node:path";
import { StringDecoder } from "node:string_decoder";

import type { OwnedProcessIdentity } from "../ledger/types.ts";
import { serializeJsonLine } from "./jsonl.ts";

const STDERR_LIMIT = 32 * 1024;
const MAX_RPC_RECORD_CHARS = 1_048_576;
const MAX_EVENT_TEXT_CHARS = 4_000;
const TERM_GRACE_MS = 2_000;
const KILL_GRACE_MS = 2_000;
const POLL_MS = 25;

export type PiAttemptEvent =
  | { type: "started"; identity: OwnedProcessIdentity }
  | { type: "prompt_accepted" }
  | { type: "tool_activity"; toolName: string }
  | { type: "usage"; input: number; output: number; total: number; cost: number }
  | { type: "settled" }
  | { type: "blocked"; message: string }
  | { type: "error"; message: string }
  | { type: "abort" }
  | { type: "exit"; code: number | null; signal: NodeJS.Signals | null };

export interface PiAttemptSpec {
  executable: string;
  cwd: string;
  sessionFile: string;
  attemptToken: string;
  prompt: string;
  model: string;
  thinking: "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
  journalOwnership(identity: OwnedProcessIdentity | null): Promise<void> | void;
  recordEvent(event: PiAttemptEvent): Promise<void> | void;
}

export interface PiAttemptResult {
  status: "settled" | "blocked" | "error" | "cancelled";
  usage?: { input: number; output: number; total: number; cost: number };
  error?: string;
  stderr: string;
  stderrTruncated: boolean;
  exitCode: number | null;
  exitSignal: NodeJS.Signals | null;
}

interface RpcRecord {
  type: string;
  id?: string;
  success?: boolean;
  error?: string;
  data?: unknown;
  toolName?: string;
}

interface Outcome {
  status: PiAttemptResult["status"];
  usage?: PiAttemptResult["usage"];
  error?: string;
}

class Deferred<T> {
  readonly promise: Promise<T>;
  resolve!: (value: T) => void;

  constructor() {
    this.promise = new Promise<T>((resolve) => {
      this.resolve = resolve;
    });
  }
}

class StrictPiDecoder {
  readonly #decoder = new StringDecoder("utf8");
  #buffer = "";

  push(chunk: Buffer): string[] {
    this.#buffer += this.#decoder.write(chunk);
    if (this.#buffer.length > MAX_RPC_RECORD_CHARS) {
      throw new Error("Pi RPC record exceeds the bounded framing limit");
    }
    const lines: string[] = [];
    while (true) {
      const newline = this.#buffer.indexOf("\n");
      if (newline < 0) return lines;
      const line = this.#buffer.slice(0, newline);
      this.#buffer = this.#buffer.slice(newline + 1);
      if (line.endsWith("\r")) throw new Error("Pi RPC emitted CRLF framing");
      if (line.length === 0) throw new Error("Pi RPC emitted an empty record");
      lines.push(line);
    }
  }

  end(): void {
    this.#buffer += this.#decoder.end();
    if (this.#buffer.length > 0) throw new Error("Pi RPC ended with an unterminated record");
  }
}

/** Run one fresh owned Pi RPC attempt and return only after its process group is absent. */
export async function runOwnedPiAttempt(
  spec: PiAttemptSpec,
  signal: AbortSignal,
): Promise<PiAttemptResult> {
  validateSpec(spec);
  mkdirSync(dirname(spec.sessionFile), { recursive: true, mode: 0o700 });
  if (existsSync(spec.sessionFile)) throw new Error("Pi attempt session file already exists");

  const child = spawn(
    spec.executable,
    [
      "--mode", "rpc",
      "--session", spec.sessionFile,
      "--no-extensions",
      "--approve",
      "--model", spec.model,
      "--thinking", spec.thinking,
    ],
    { cwd: spec.cwd, detached: true, stdio: ["pipe", "pipe", "pipe"] },
  );
  const pid = child.pid;
  if (pid === undefined) {
    child.once("error", () => undefined);
    throw new Error("Pi worker did not expose a PID");
  }
  const identity: OwnedProcessIdentity = {
    pid,
    processGroupId: pid,
    sessionFile: spec.sessionFile,
    attemptToken: spec.attemptToken,
  };

  let stderr = Buffer.alloc(0);
  let stderrTruncated = false;
  child.stderr.on("data", (chunk: Buffer) => {
    stderr = Buffer.concat([stderr, chunk]);
    if (stderr.length > STDERR_LIMIT) {
      stderr = stderr.subarray(stderr.length - STDERR_LIMIT);
      stderrTruncated = true;
    }
  });

  const closed = new Deferred<{ code: number | null; signal: NodeJS.Signals | null }>();
  child.once("close", (code, closeSignal) => closed.resolve({ code, signal: closeSignal }));
  child.once("error", (error) => closed.resolve({ code: null, signal: null }));

  const outcome = new Deferred<Outcome>();
  let outcomeSet = false;
  let protocolError: string | undefined;
  let errorEmitted = false;
  let promptAccepted = false;
  let settled = false;
  let usage: PiAttemptResult["usage"];
  const promptId = `prompt-${spec.attemptToken}`;
  const statsId = `stats-${spec.attemptToken}`;
  const finish = (value: Outcome): void => {
    if (outcomeSet) return;
    outcomeSet = true;
    outcome.resolve(value);
  };
  const emit = async (event: PiAttemptEvent): Promise<void> => spec.recordEvent(event);

  const reader = (async () => {
    const decoder = new StrictPiDecoder();
    try {
      for await (const raw of child.stdout) {
        for (const line of decoder.push(Buffer.from(raw))) {
          let record: RpcRecord;
          try {
            record = JSON.parse(line) as RpcRecord;
          } catch {
            throw new Error("Pi RPC emitted malformed JSON");
          }
          if (record === null || typeof record !== "object" || typeof record.type !== "string") {
            throw new Error("Pi RPC emitted a malformed record");
          }
          if (record.type === "response" && record.id === promptId) {
            if (record.success !== true) {
              const message = boundedMessage(record.error ?? "Pi rejected the prompt");
              await emit({ type: "blocked", message });
              finish({ status: "blocked", error: message });
            } else {
              promptAccepted = true;
              await emit({ type: "prompt_accepted" });
              if (settled && usage !== undefined) finish({ status: "settled", usage });
            }
            continue;
          }
          if (record.type === "response" && record.id === statsId) {
            if (record.success !== true) throw new Error(record.error ?? "Pi stats request failed");
            usage = parseUsage(record.data);
            await emit({ type: "usage", ...usage });
            if (settled && promptAccepted) finish({ status: "settled", usage });
            continue;
          }
          if (record.type === "agent_settled") {
            settled = true;
            await emit({ type: "settled" });
            await writeRecord(child.stdin, { type: "get_session_stats", id: statsId });
            continue;
          }
          if (record.type === "tool_execution_start") {
            await emit({
              type: "tool_activity",
              toolName: boundedMessage(record.toolName ?? "unknown"),
            });
            continue;
          }
          if (record.type === "agent_error" || record.type === "error") {
            const message = boundedMessage(record.error ?? "Pi worker reported an error");
            await emit({ type: "error", message });
            errorEmitted = true;
            finish({ status: "error", error: message });
          }
        }
      }
      decoder.end();
    } catch (error) {
      const message = boundedMessage(error instanceof Error ? error.message : String(error));
      protocolError = message;
      await emit({ type: "error", message }).then(
        () => { errorEmitted = true; },
        () => undefined,
      );
      finish({ status: "error", error: message });
    }
  })();

  let journaled = false;
  let selected: Outcome;
  try {
    await spec.journalOwnership(identity);
    journaled = true;
    await emit({ type: "started", identity });
    if (signal.aborted) {
      await emit({ type: "abort" });
      selected = { status: "cancelled" };
    } else {
      await writeRecord(child.stdin, { type: "prompt", id: promptId, message: spec.prompt });
      const aborted = new Deferred<Outcome>();
      const onAbort = (): void => aborted.resolve({ status: "cancelled" });
      signal.addEventListener("abort", onAbort, { once: true });
      selected = await Promise.race([
        outcome.promise,
        aborted.promise,
        closed.promise.then((): Outcome => ({
          status: "error",
          error: "Pi worker exited before settlement",
        })),
      ]);
      signal.removeEventListener("abort", onAbort);
      if (selected.status === "cancelled") {
        await emit({ type: "abort" });
        if (promptAccepted && child.stdin.writable) {
          await writeRecord(child.stdin, { type: "abort", id: `abort-${spec.attemptToken}` }).catch(
            () => undefined,
          );
        }
      }
    }
  } catch (error) {
    selected = { status: "error", error: error instanceof Error ? error.message : String(error) };
  }

  const exit = await terminateProcessGroup(identity.processGroupId, closed.promise);
  await reader.catch(() => undefined);
  if (protocolError !== undefined) selected = { status: "error", error: protocolError };
  if (selected.status === "error" && !errorEmitted) {
    await emit({ type: "error", message: selected.error ?? "Pi worker failed" });
  }
  let exitEventError: unknown;
  try {
    await emit({ type: "exit", code: exit.code, signal: exit.signal });
  } catch (error) {
    exitEventError = error;
  }
  if (journaled) await spec.journalOwnership(null);
  if (exitEventError !== undefined) throw exitEventError;
  return {
    status: selected.status,
    ...(selected.usage === undefined ? {} : { usage: selected.usage }),
    ...(selected.error === undefined ? {} : { error: selected.error }),
    stderr: stderr.toString("utf8"),
    stderrTruncated,
    exitCode: exit.code,
    exitSignal: exit.signal,
  };
}

function validateSpec(spec: PiAttemptSpec): void {
  if (!isAbsolute(spec.executable)) throw new Error("Pi executable must be absolute");
  if (!isAbsolute(spec.cwd)) throw new Error("Pi cwd must be absolute");
  if (!isAbsolute(spec.sessionFile)) throw new Error("Pi session file must be absolute");
  if (spec.attemptToken.length === 0 || spec.prompt.trim().length === 0 || spec.model.length === 0) {
    throw new Error("Pi attempt identity, prompt, and model are required");
  }
}

async function writeRecord(
  stdin: NodeJS.WritableStream,
  record: Record<string, unknown>,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    stdin.write(serializeJsonLine(record), (error?: Error | null) => {
      if (error) reject(error);
      else resolve();
    });
  });
}

function boundedMessage(message: string): string {
  return message.slice(0, MAX_EVENT_TEXT_CHARS);
}

function parseUsage(data: unknown): { input: number; output: number; total: number; cost: number } {
  const value = data as { tokens?: Record<string, unknown>; cost?: unknown };
  const input = Number(value?.tokens?.input ?? 0);
  const output = Number(value?.tokens?.output ?? 0);
  const total = Number(value?.tokens?.total ?? input + output);
  const cost = Number(value?.cost ?? 0);
  if (![input, output, total, cost].every(Number.isFinite)) throw new Error("Pi emitted invalid usage");
  return { input, output, total, cost };
}

async function terminateProcessGroup(
  processGroupId: number,
  closed: Promise<{ code: number | null; signal: NodeJS.Signals | null }>,
): Promise<{ code: number | null; signal: NodeJS.Signals | null }> {
  signalGroup(processGroupId, "SIGTERM");
  if (!(await waitForGroupAbsence(processGroupId, TERM_GRACE_MS))) {
    signalGroup(processGroupId, "SIGKILL");
    if (!(await waitForGroupAbsence(processGroupId, KILL_GRACE_MS))) {
      throw new Error(`Pi process group ${processGroupId} survived SIGKILL`);
    }
  }
  return Promise.race([
    closed,
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error("Pi child close was not observed after reap")), KILL_GRACE_MS),
    ),
  ]);
}

function signalGroup(processGroupId: number, signal: NodeJS.Signals): void {
  try {
    process.kill(-processGroupId, signal);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
  }
}

async function waitForGroupAbsence(processGroupId: number, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      process.kill(-processGroupId, 0);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ESRCH") return true;
      throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
  return false;
}
