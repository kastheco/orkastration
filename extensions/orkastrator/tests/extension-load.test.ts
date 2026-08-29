import assert from "node:assert/strict";
import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import { RunLedger } from "../ledger/file-ledger.ts";
import { attachJsonlLineReader, serializeJsonLine } from "../rpc/jsonl.ts";

interface RecordValue {
  type: string;
  id?: string;
  success?: boolean;
  error?: string;
  data?: unknown;
  method?: string;
  message?: string;
}

class PiProbe {
  readonly child: ChildProcessWithoutNullStreams;
  readonly events: RecordValue[] = [];
  readonly #pending = new Map<string, (record: RecordValue) => void>();
  readonly #detach: () => void;
  #request = 0;
  #stderr = "";

  constructor(repository: string, stateRoot: string, extraExtensions: string[] = []) {
    this.child = spawn(
      join(repository, "node_modules/.bin/pi"),
      [
        "--mode",
        "rpc",
        "--no-session",
        "--approve",
        ...extraExtensions.flatMap((extension) => ["--extension", extension]),
      ],
      {
        cwd: repository,
        env: {
          ...process.env,
          ORKASTRATOR_STATE_DIR: stateRoot,
          PI_CODING_AGENT_DIR: join(stateRoot, "pi-agent"),
        },
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    this.child.stderr.on("data", (chunk: Buffer) => {
      this.#stderr += chunk.toString("utf8");
    });
    this.#detach = attachJsonlLineReader(this.child.stdout, (line) => {
      const record = JSON.parse(line) as RecordValue;
      if (record.type === "response" && record.id !== undefined) {
        const resolveResponse = this.#pending.get(record.id);
        if (resolveResponse !== undefined) {
          this.#pending.delete(record.id);
          resolveResponse(record);
          return;
        }
      }
      this.events.push(record);
    });
  }

  send(command: Record<string, unknown>): Promise<RecordValue> {
    return new Promise((resolveResponse, rejectResponse) => {
      const id = `load-${(this.#request += 1)}`;
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        rejectResponse(new Error(`RPC response timed out; stderr: ${this.#stderr}`));
      }, 30_000);
      this.#pending.set(id, (record) => {
        clearTimeout(timer);
        if (record.success !== true) {
          rejectResponse(new Error(record.error ?? "RPC command failed"));
        } else {
          resolveResponse(record);
        }
      });
      this.child.stdin.write(serializeJsonLine({ ...command, id }));
    });
  }

  async signal(signal: "SIGTERM" | "SIGHUP"): Promise<number | null> {
    const closed = new Promise<number | null>((resolveClose, rejectClose) => {
      const timer = setTimeout(
        () => rejectClose(new Error(`Pi shutdown timed out; stderr: ${this.#stderr}`)),
        10_000,
      );
      this.child.once("close", (code) => {
        clearTimeout(timer);
        resolveClose(code);
      });
    });
    this.child.kill(signal);
    return closed;
  }

  close(): void {
    this.#detach();
    if (this.child.exitCode === null) this.child.kill("SIGKILL");
  }
}

const repository = resolve(import.meta.dirname, "../../../");

test("trusted project discovery loads the repository-local Orkastrator extension", async () => {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-extension-load-"));
  const probe = new PiProbe(repository, join(temporary, "state"));
  try {
    const commands = await probe.send({ type: "get_commands" });
    const data = commands.data as { commands: Array<{ name: string }> };
    assert.equal(
      data.commands.some((command) => command.name === "orkastrator-runs"),
      true,
    );
    await probe.send({ type: "prompt", message: "/orkastrator-runs" });
    await new Promise<void>((resolveTick) => setImmediate(resolveTick));
    const notification = probe.events.find(
      (event) =>
        event.type === "extension_ui_request" &&
        event.method === "notify" &&
        /owns no active Orkastrator run/u.test(event.message ?? ""),
    );
    assert.match(notification?.message ?? "", /owns no active Orkastrator run/u);
    assert.equal(await probe.signal("SIGTERM"), 143);
  } finally {
    probe.close();
    rmSync(temporary, { recursive: true, force: true });
  }
});

for (const signal of ["SIGTERM", "SIGHUP"] as const) {
  test(`real Pi ${signal} durably interrupts a reload-bound active run`, async () => {
    const temporary = mkdtempSync(join(tmpdir(), `orkastrator-${signal.toLowerCase()}-`));
    const stateRoot = join(temporary, "state");
    const reloadExtension = join(temporary, "reload-extension.ts");
    writeFileSync(
      reloadExtension,
      `export default function (pi) {\n  pi.registerCommand("orkastrator-test-reload", {\n    description: "Reload extensions for lifecycle testing",\n    handler: async (_args, ctx) => ctx.reload(),\n  });\n}\n`,
    );
    const probe = new PiProbe(repository, stateRoot, [reloadExtension]);
    try {
      const state = await probe.send({ type: "get_state" });
      const sessionId = (state.data as { sessionId: string }).sessionId;
      assert.match(sessionId, /^[0-9a-f-]{36}$/u);
      assert.ok(probe.child.pid);

      const ledger = new RunLedger({ root: stateRoot });
      const run = ledger.createRun({
        objective: `Prove ${signal} shutdown.`,
        supervisorSessionId: sessionId,
        repositoryRoot: repository,
        policySnapshot: "version: 1\n",
        hostPid: probe.child.pid,
      });
      ledger.prepareReload(run.runId, sessionId, repository, probe.child.pid);
      await probe.send({ type: "prompt", message: "/orkastrator-test-reload" });
      assert.equal(ledger.loadRun(run.runId).record.generation, 2);

      const exitCode = await probe.signal(signal);
      assert.equal(exitCode, signal === "SIGTERM" ? 143 : 129);
      const interrupted = ledger.loadRun(run.runId).record;
      assert.equal(interrupted.state, "interrupted");
      assert.equal(interrupted.reason, "session_shutdown:quit");
      assert.equal(ledger.events(run.runId).at(-1)?.ruleId, "lifecycle.session_shutdown");
    } finally {
      probe.close();
      rmSync(temporary, { recursive: true, force: true });
    }
  });
}
