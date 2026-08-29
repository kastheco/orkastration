import assert from "node:assert/strict";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import { attachJsonlLineReader, serializeJsonLine } from "../rpc/jsonl.ts";

interface RpcRecord {
  type: string;
  id?: string;
  command?: string;
  success?: boolean;
  data?: unknown;
  error?: string;
}

interface PendingResponse {
  resolve: (record: RpcRecord) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface EventWaiter {
  type: string;
  resolve: (record: RpcRecord) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

class RpcProbe {
  readonly child: ChildProcessWithoutNullStreams;
  readonly events: RpcRecord[] = [];
  readonly records: RpcRecord[] = [];
  #nextRequest = 0;
  #pending = new Map<string, PendingResponse>();
  #waiters: EventWaiter[] = [];
  #stderr = "";
  #stdoutChunks: Buffer[] = [];
  #detachReader: () => void;

  constructor(binary: string, args: string[], cwd: string, env: NodeJS.ProcessEnv) {
    this.child = spawn(binary, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    this.child.stderr.on("data", (chunk: Buffer) => {
      this.#stderr += chunk.toString("utf8");
    });
    this.child.stdout.on("data", (chunk: Buffer) => {
      this.#stdoutChunks.push(Buffer.from(chunk));
    });
    this.#detachReader = attachJsonlLineReader(this.child.stdout, (line) => this.#onLine(line));
  }

  send(command: Record<string, unknown>, timeoutMs = 30_000): Promise<RpcRecord> {
    const id = `contract-${(this.#nextRequest += 1)}`;
    return new Promise<RpcRecord>((resolveResponse, rejectResponse) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        rejectResponse(new Error(`RPC response timed out for ${String(command.type)}`));
      }, timeoutMs);
      this.#pending.set(id, { resolve: resolveResponse, reject: rejectResponse, timer });
      this.child.stdin.write(serializeJsonLine({ ...command, id }));
    });
  }

  waitForEvent(type: string, timeoutMs = 120_000): Promise<RpcRecord> {
    return new Promise<RpcRecord>((resolveEvent, rejectEvent) => {
      const timer = setTimeout(() => {
        rejectEvent(new Error(`RPC event timed out for ${type}; stderr: ${this.#stderr}`));
      }, timeoutMs);
      this.#waiters.push({ type, resolve: resolveEvent, reject: rejectEvent, timer });
    });
  }

  async terminate(): Promise<{ code: number | null; signal: NodeJS.Signals | null }> {
    const closed = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(
      (resolveExit, rejectExit) => {
        const timer = setTimeout(() => rejectExit(new Error("Pi SIGTERM shutdown timed out")), 10_000);
        this.child.once("close", (code, signal) => {
          clearTimeout(timer);
          resolveExit({ code, signal });
        });
      },
    );
    this.child.kill("SIGTERM");
    const result = await closed;
    this.#detachReader();
    return result;
  }

  rawStdout(): Buffer {
    return Buffer.concat(this.#stdoutChunks);
  }

  forceStop(): void {
    this.#detachReader();
    this.child.kill("SIGKILL");
  }

  #onLine(line: string): void {
    const record = JSON.parse(line) as RpcRecord;
    this.records.push(record);
    if (record.type === "response" && record.id !== undefined) {
      const pending = this.#pending.get(record.id);
      if (pending !== undefined) {
        clearTimeout(pending.timer);
        this.#pending.delete(record.id);
        if (record.success !== true) {
          pending.reject(new Error(record.error ?? `RPC ${record.command ?? "command"} failed`));
        } else {
          pending.resolve(record);
        }
        return;
      }
    }
    this.events.push(record);
    for (let index = this.#waiters.length - 1; index >= 0; index -= 1) {
      const waiter = this.#waiters[index];
      if (waiter?.type !== record.type) continue;
      this.#waiters.splice(index, 1);
      clearTimeout(waiter.timer);
      waiter.resolve(record);
    }
  }
}

function data(record: RpcRecord): Record<string, unknown> {
  assert.equal(typeof record.data, "object");
  assert.notEqual(record.data, null);
  return record.data as Record<string, unknown>;
}

const live = process.env.ORKASTRATOR_LIVE_PI_RPC === "1";

test(
  "Pi 0.84.3 accepts a prompt, settles, reports stats, aborts, and runs shutdown",
  { skip: live ? false : "set ORKASTRATOR_LIVE_PI_RPC=1 to spend one live model probe" },
  async () => {
    const repository = resolve(import.meta.dirname, "../../../");
    const temporary = mkdtempSync(join(tmpdir(), "orkastrator-pi-rpc-"));
    const marker = join(temporary, "shutdown.jsonl");
    const extension = join(temporary, "shutdown-probe.ts");
    writeFileSync(
      extension,
      [
        'import { appendFileSync } from "node:fs";',
        "export default function (pi: any) {",
        '  pi.on("session_shutdown", async (event: any) => {',
        "    appendFileSync(process.env.ORKASTRATOR_SHUTDOWN_MARKER!,",
        '      JSON.stringify({ type: event.type, reason: event.reason }) + "\\n");',
        "  });",
        "}",
      ].join("\n"),
    );
    const probe = new RpcProbe(
      join(repository, "node_modules/.bin/pi"),
      [
        "--mode",
        "rpc",
        "--no-session",
        "--no-extensions",
        "--extension",
        extension,
        "--model",
        process.env.ORKASTRATOR_PI_CONTRACT_MODEL ?? "openai-codex/gpt-5.6-sol:low",
      ],
      repository,
      { ...process.env, ORKASTRATOR_SHUTDOWN_MARKER: marker },
    );
    try {
      const state = data(await probe.send({ type: "get_state" }));
      assert.equal(state.thinkingLevel, "low");
      assert.equal(state.isStreaming, false);

      const firstSettled = probe.waitForEvent("agent_settled");
      const promptResponse = await probe.send({
        type: "prompt",
        message: "Reply with exactly CONTRACT_OK. Do not call tools.",
      });
      assert.equal(promptResponse.success, true);
      const firstSettledEvent = await firstSettled;
      assert.ok(probe.records.indexOf(promptResponse) < probe.records.indexOf(firstSettledEvent));
      assert.equal(data(await probe.send({ type: "get_last_assistant_text" })).text, "CONTRACT_OK");
      const stats = data(await probe.send({ type: "get_session_stats" }));
      assert.equal(stats.userMessages, 1);
      assert.equal(stats.assistantMessages, 1);
      assert.equal(stats.toolCalls, 0);
      assert.equal(stats.toolResults, 0);
      assert.equal(stats.totalMessages, 2);
      const tokens = data({ type: "stats", data: stats.tokens });
      assert.equal(typeof tokens.input, "number");
      assert.equal(typeof tokens.output, "number");
      assert.equal(typeof tokens.total, "number");
      assert.equal(typeof stats.cost, "number");
      const contextUsage = data({ type: "stats", data: stats.contextUsage });
      assert.equal(typeof contextUsage.tokens, "number");
      assert.equal(typeof contextUsage.contextWindow, "number");
      assert.equal(typeof contextUsage.percent, "number");

      const started = probe.waitForEvent("agent_start");
      const ended = probe.waitForEvent("agent_end");
      const secondSettled = probe.waitForEvent("agent_settled");
      await probe.send({
        type: "prompt",
        message: "Write a very long explanation of every integer from 1 through 5000.",
      });
      const startedEvent = await started;
      assert.equal((await probe.send({ type: "abort" })).success, true);
      const endedEvent = await ended;
      const secondSettledEvent = await secondSettled;
      assert.ok(probe.records.indexOf(startedEvent) < probe.records.indexOf(endedEvent));
      assert.ok(probe.records.indexOf(endedEvent) < probe.records.indexOf(secondSettledEvent));

      assert.deepEqual(await probe.terminate(), { code: 143, signal: null });
      const rawStdout = probe.rawStdout();
      assert.equal(rawStdout.includes(Buffer.from("\r\n")), false);
      assert.equal(rawStdout.subarray(-1).equals(Buffer.from("\n")), true);
      assert.deepEqual(
        readFileSync(marker, "utf8").trim().split("\n").map((line) => JSON.parse(line)),
        [{ type: "session_shutdown", reason: "quit" }],
      );
    } finally {
      if (probe.child.exitCode === null) probe.forceStop();
      rmSync(temporary, { recursive: true, force: true });
    }
  },
);
