#!/usr/bin/env node
/**
 * Disposable end-to-end test for `/kas:cook`.
 *
 * One command:
 *
 *   npm run test:cook-lifecycle
 *
 * What runs for real: the installed `pi` CLI in RPC mode, the Orkastrator
 * extension as an installed package, its pinned Pi Workflows host and worker
 * processes, the real `pi-subagents` backend (selected because `HERDR_PANE_ID`
 * is absent), the session-owned Unix broker, Worktrunk task worktrees, and the
 * fixture's `npm test`. What is scripted: the model. Every model turn is served
 * by a local OpenAI-compatible server whose answers are keyed on the workflow
 * step contract, so the run is deterministic and never spends provider quota.
 *
 * Protected decisions are answered only through Pi's RPC extension UI
 * sub-protocol (`extension_ui_request` -> `extension_ui_response`), which is
 * the same trusted client boundary ClickClack and the TUI use. Nothing in this
 * script talks to the workflow host to forge an approval.
 *
 * Flags:
 *   --keep    preserve the temporary root after success
 *   --runs N  repeat the full lifecycle N times on fresh fixtures (default 1)
 */
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { createCookLifecycleScript } from "./lib/cook-lifecycle-script.mjs";
import { startScriptedModelServer } from "./lib/scripted-model-server.mjs";

const require = createRequire(import.meta.url);
const repository = fileURLToPath(new URL("..", import.meta.url));
const piSubagentsRoot = join(repository, "node_modules", "pi-subagents");
const piCli = join(repository, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "bundle", "cli.js");
const workflowsRoot = resolve(dirname(require.resolve("@osolmaz/pi-workflows")), "..", "..");
for (const required of [piSubagentsRoot, piCli, workflowsRoot]) {
  if (!existsSync(required)) throw new Error(`missing dependency path ${required}; run npm install first`);
}

const TASK = "Fix the implementation of `sum(a, b)` so the existing tests pass. Keep the exported API unchanged and do not modify unrelated files.";
const FIXTURE_TASK_DIGEST_PREFIX = "orkastrator/";

const args = process.argv.slice(2);
const keep = args.includes("--keep");
const runsIndex = args.indexOf("--runs");
const runCount = runsIndex === -1 ? 1 : Number.parseInt(args[runsIndex + 1] ?? "1", 10);
if (!Number.isInteger(runCount) || runCount < 1) {
  console.error("--runs requires a positive integer");
  process.exit(2);
}

function git(cwd, ...rest) {
  return execFileSync("git", ["-C", cwd, ...rest], { encoding: "utf8" }).trim();
}

function writeFixtureRepository(root) {
  mkdirSync(join(root, "src"), { recursive: true });
  mkdirSync(join(root, "test"), { recursive: true });
  writeFileSync(join(root, "package.json"), `${JSON.stringify({
    name: "orkastrator-cook-fixture",
    version: "0.0.0",
    private: true,
    type: "commonjs",
    scripts: { test: "node --test" },
  }, null, 2)}\n`);
  writeFileSync(join(root, "src", "sum.js"), [
    "\"use strict\";",
    "",
    "function sum(a, b) {",
    "  return a - b;",
    "}",
    "",
    "module.exports = { sum };",
    "",
  ].join("\n"));
  writeFileSync(join(root, "test", "sum.test.js"), [
    "\"use strict\";",
    "const assert = require(\"node:assert/strict\");",
    "const { test } = require(\"node:test\");",
    "const { sum } = require(\"../src/sum.js\");",
    "",
    "test(\"sum adds two numbers\", () => {",
    "  assert.equal(sum(2, 3), 5);",
    "  assert.equal(sum(-1, 1), 0);",
    "});",
    "",
  ].join("\n"));
  writeFileSync(join(root, "AGENTS.md"), [
    "# Fixture repository",
    "",
    "This repository owns every task launched against it. Run `npm test` to verify changes.",
    "",
  ].join("\n"));
  writeFileSync(join(root, ".gitignore"), "node_modules/\n");
  git(root, "init", "-q", "-b", "main");
  git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "add", ".");
  git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "chore: fixture baseline with a broken sum");
  return git(root, "rev-parse", "HEAD");
}

function writeFakeBinaries(binDir) {
  mkdirSync(binDir, { recursive: true });
  // autoimplement's runReview action executes `pi-reviewer --base <branch>` as
  // a real subprocess. The fixture reviewer returns a clean, well-formed result.
  const reviewer = join(binDir, "pi-reviewer");
  writeFileSync(reviewer, [
    "#!/bin/sh",
    "echo '{\"findings\":[],\"summary\":\"fixture reviewer: no findings\"}'",
    "",
  ].join("\n"));
  chmodSync(reviewer, 0o755);
  // Guard: nothing in the scripted lifecycle should reach GitHub.
  const gh = join(binDir, "gh");
  writeFileSync(gh, [
    "#!/bin/sh",
    "echo 'fixture gh: network access is not available in the lifecycle test' >&2",
    "exit 97",
    "",
  ].join("\n"));
  chmodSync(gh, 0o755);
}

function writeAgentDir(agentDir, modelBaseUrl) {
  mkdirSync(join(agentDir, "sessions"), { recursive: true });
  writeFileSync(join(agentDir, "models.json"), `${JSON.stringify({
    providers: {
      scripted: {
        name: "Scripted lifecycle model",
        baseUrl: modelBaseUrl,
        api: "openai-completions",
        apiKey: "scripted-key",
        compat: { supportsDeveloperRole: false, supportsReasoningEffort: false },
        models: [{ id: "lifecycle-model", contextWindow: 200_000, maxTokens: 16_000 }],
      },
    },
  }, null, 2)}\n`);
  writeFileSync(join(agentDir, "auth.json"), "{}\n");
  writeFileSync(join(agentDir, "settings.json"), `${JSON.stringify({
    defaultProvider: "scripted",
    defaultModel: "lifecycle-model",
    defaultProjectTrust: "always",
    quietStartup: true,
    enableInstallTelemetry: false,
    packages: [repository, piSubagentsRoot],
  }, null, 2)}\n`);
  mkdirSync(join(agentDir, "orkastrator"), { recursive: true });
  const orkastratorConfig = join(agentDir, "orkastrator", "config.json");
  writeFileSync(orkastratorConfig, `${JSON.stringify({
    review: {
      initial: { model: "scripted/lifecycle-model", thinking: "low" },
      fixer: { model: "scripted/lifecycle-model", thinking: "low" },
      reReview: { model: "scripted/lifecycle-model", thinking: "low" },
    },
  }, null, 2)}\n`);
  return orkastratorConfig;
}

class RpcPi {
  constructor(options) {
    this.lines = [];
    this.stderr = "";
    this.uiRequests = [];
    this.uiResponses = [];
    this.extensionErrors = [];
    this.pendingResponses = new Map();
    this.onEvent = options.onEvent;
    this.child = spawn(process.execPath, [
      piCli,
      "--mode", "rpc",
      "--session-id", options.sessionId,
      "--session-dir", options.sessionDir,
      "--no-skills",
      "--no-themes",
      "--no-prompt-templates",
      "--no-context-files",
      "--offline",
      "--provider", "scripted",
      "--model", "lifecycle-model",
    ], {
      cwd: options.cwd,
      env: options.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let buffer = "";
    this.child.stdout.setEncoding("utf8");
    this.child.stderr.setEncoding("utf8");
    this.child.stderr.on("data", (chunk) => { this.stderr += chunk; });
    this.child.stdout.on("data", (chunk) => {
      buffer += chunk;
      while (true) {
        const newline = buffer.indexOf("\n");
        if (newline === -1) break;
        let line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (line.trim() === "") continue;
        let event;
        try {
          event = JSON.parse(line);
        } catch {
          continue;
        }
        this.lines.push(event);
        if (this.lines.length > 400) this.lines.shift();
        this.handle(event);
      }
    });
    this.exited = new Promise((resolveExit) => {
      this.child.once("exit", (code, signal) => resolveExit({ code, signal }));
    });
  }

  handle(event) {
    if (event.type === "response" && typeof event.id === "string") {
      const pending = this.pendingResponses.get(event.id);
      if (pending !== undefined) {
        this.pendingResponses.delete(event.id);
        pending(event);
      }
    }
    if (event.type === "extension_error") this.extensionErrors.push(event);
    if (event.type === "extension_ui_request") {
      this.uiRequests.push(event);
      if (this.uiRequests.length > 200) this.uiRequests.shift();
    }
    this.onEvent?.(event);
  }

  send(command) {
    this.child.stdin.write(`${JSON.stringify(command)}\n`);
  }

  async request(command, timeoutMs = 20_000) {
    const id = `lifecycle-${randomUUID()}`;
    const response = new Promise((resolveResponse, reject) => {
      const timer = setTimeout(() => {
        this.pendingResponses.delete(id);
        reject(new Error(`Pi RPC ${command.type} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pendingResponses.set(id, (event) => {
        clearTimeout(timer);
        resolveResponse(event);
      });
    });
    this.send({ id, ...command });
    return await response;
  }

  respondToUi(id, value) {
    this.uiResponses.push({ id, ...value });
    this.send({ type: "extension_ui_response", id, ...value });
  }

  async stop() {
    if (this.child.exitCode !== null || this.child.signalCode !== null) return;
    const race = (ms) => Promise.race([
      this.exited.then(() => true),
      new Promise((resolveRace) => setTimeout(() => resolveRace(false), ms)),
    ]);
    this.child.stdin.end();
    if (await race(3_000)) return;
    this.child.kill("SIGTERM");
    if (await race(3_000)) return;
    this.child.kill("SIGKILL");
    await this.exited;
  }
}

async function waitFor(predicate, describe, timeoutMs, intervalMs = 150) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await predicate();
    if (last) return last;
    await new Promise((resolveWait) => setTimeout(resolveWait, intervalMs));
  }
  throw new Error(`Timed out after ${timeoutMs}ms waiting for ${describe}`);
}

function readRuns(WorkflowRunStore, databasePath) {
  if (!existsSync(databasePath)) return [];
  const store = new WorkflowRunStore(databasePath, { readOnly: true });
  try {
    return store.listRuns().map((run) => ({
      runId: run.runId,
      workflowName: run.state.workflowName,
      status: run.state.status,
      parentRunId: run.state.parentRunId,
      waitingOn: run.state.waitingOn,
      currentNode: run.state.currentNode,
      error: run.state.error,
      finalOutput: run.state.finalOutput,
      stepCount: run.state.steps.length,
      lastSteps: run.state.steps.slice(-5).map((step) => `${step.nodeId}:${step.outcome}`),
    }));
  } catch {
    return [];
  } finally {
    store.close();
  }
}

/** Follow parent -> continuation links from the launched run to the newest run. */
function lineage(runs, rootRunId) {
  const chain = [];
  let current = runs.find((run) => run.runId === rootRunId);
  const seen = new Set();
  while (current !== undefined && !seen.has(current.runId)) {
    seen.add(current.runId);
    chain.push(current);
    current = runs.find((run) => run.parentRunId === current.runId);
  }
  return chain;
}

async function runLifecycle(iteration) {
  const temporary = mkdtempSync(join(tmpdir(), `orkastrator-cook-${iteration}-`));
  const home = join(temporary, "home");
  const agentDir = join(home, ".pi", "agent");
  const runtimeDir = join(temporary, "runtime");
  const subagentTemp = join(temporary, "subagents");
  const binDir = join(temporary, "bin");
  const fixture = join(temporary, "fixture");
  const logs = [];
  const log = (message) => {
    const line = `[${new Date().toISOString()}] ${message}`;
    logs.push(line);
    console.log(line);
  };
  mkdirSync(fixture, { recursive: true });
  mkdirSync(runtimeDir, { recursive: true, mode: 0o700 });
  mkdirSync(subagentTemp, { recursive: true });
  const baseline = writeFixtureRepository(fixture);
  writeFakeBinaries(binDir);

  const scripted = createCookLifecycleScript({ task: TASK, fixtureRepository: fixture });
  const model = await startScriptedModelServer(scripted.script, { log });
  const orkastratorConfig = writeAgentDir(agentDir, model.baseUrl);
  const databasePath = join(agentDir, "workflows", "state.sqlite");

  const env = { ...process.env };
  delete env.HERDR_PANE_ID;
  delete env.HERDR_ENV;
  delete env.HERDR_SOCKET_PATH;
  delete env.HERDR_TAB_ID;
  delete env.HERDR_WORKSPACE_ID;
  delete env.PI_SUBAGENT_CHILD;
  delete env.PI_SUBAGENT_PARENT_SESSION;
  delete env.PI_SESSION_ID;
  delete env.PI_SESSION_FILE;
  Object.assign(env, {
    HOME: home,
    PI_CODING_AGENT_DIR: agentDir,
    XDG_RUNTIME_DIR: runtimeDir,
    XDG_CONFIG_HOME: join(home, ".config"),
    PI_SUBAGENTS_TEMP_ROOT: subagentTemp,
    ORKASTRATOR_CONFIG: orkastratorConfig,
    PATH: `${binDir}:${env.PATH ?? ""}`,
    NO_COLOR: "1",
    PI_OFFLINE: "1",
    PI_SKIP_VERSION_CHECK: "1",
  });

  const { WorkflowRunStore } = await import(
    pathToFileURL(join(workflowsRoot, "dist/workflows/store.js")).href
  );
  const { WorkflowClient } = await import(
    pathToFileURL(join(workflowsRoot, "dist/client/index.js")).href
  );

  const sessionId = randomUUID();
  const decisions = [];
  let launchedRunId;
  const pi = new RpcPi({
    sessionId,
    sessionDir: join(agentDir, "sessions"),
    cwd: fixture,
    env,
    onEvent: (event) => {
      if (event.type === "extension_error") log(`extension_error ${event.extensionPath}: ${event.error}`);
      if (event.type === "extension_ui_request" && event.method === "notify") {
        log(`notify[${event.notifyType ?? "info"}]: ${event.message}`);
      }
      if (event.type === "tool_execution_end" && event.toolName === "workflow") {
        const details = event.result?.details;
        if (details?.action === "start" && typeof details.runId === "string") {
          launchedRunId = details.runId;
          log(`workflow started: ${launchedRunId}`);
        }
      }
      if (event.type === "tool_execution_end" && event.isError) {
        log(`tool ${event.toolName} error: ${JSON.stringify(event.result?.content).slice(0, 400)}`);
      }
      if (event.type === "extension_ui_request" && event.method === "select") {
        // Trusted client boundary: choose the plan-approval "continue" option.
        const options = Array.isArray(event.options) ? event.options : [];
        const choice = options.find((option) => /Yes, continue/u.test(String(option)));
        decisions.push({ title: event.title, options, choice });
        log(`decision presented: ${String(event.title).split("\n")[0]} -> ${choice ?? "<no continue option>"}`);
        if (choice !== undefined) pi.respondToUi(event.id, { value: choice });
        else pi.respondToUi(event.id, { cancelled: true });
      }
      if (event.type === "extension_ui_request" && event.method === "input") {
        log(`unexpected input dialog: ${event.title}`);
        pi.respondToUi(event.id, { cancelled: true });
      }
      if (event.type === "extension_ui_request" && event.method === "confirm") {
        log(`unexpected confirm dialog: ${event.title}`);
        pi.respondToUi(event.id, { confirmed: false });
      }
    },
  });

  const diagnostics = () => {
    const runs = readRuns(WorkflowRunStore, databasePath);
    const chain = launchedRunId === undefined ? [] : lineage(runs, launchedRunId);
    const latest = chain.at(-1);
    return [
      `temporary root: ${temporary}`,
      `launched run: ${launchedRunId ?? "<none>"}`,
      `lineage: ${chain.map((run) => `${run.runId}[${run.status}]`).join(" -> ") || "<none>"}`,
      `terminal status: ${latest?.status ?? "<unknown>"}`,
      `current node: ${latest?.currentNode ?? latest?.waitingOn ?? "<none>"}`,
      `last steps: ${latest?.lastSteps?.join(", ") ?? "<none>"}`,
      `run error: ${latest?.error ?? "<none>"}`,
      `extension errors: ${JSON.stringify(pi.extensionErrors)}`,
      `scripted model errors: ${JSON.stringify(model.errors)}`,
      `scripted steps seen: ${scripted.seenNodes.join(", ")}`,
      `decisions: ${JSON.stringify(decisions)}`,
      `pi stderr (tail):\n${pi.stderr.slice(-4_000)}`,
      `pi events (tail):\n${pi.lines.slice(-25).map((line) => JSON.stringify(line).slice(0, 300)).join("\n")}`,
    ].join("\n");
  };

  let failed = false;
  try {
    // 1. Startup and command registration.
    const commands = await waitFor(async () => {
      const response = await pi.request({ type: "get_commands" }, 60_000).catch(() => undefined);
      const list = response?.data?.commands;
      if (!Array.isArray(list)) return undefined;
      return list.some((command) => command.name === "kas:cook")
        && list.some((command) => command.name === "workflow")
        ? list
        : undefined;
    }, "kas:cook and workflow commands to register", 90_000, 500);
    const cook = commands.find((command) => command.name === "kas:cook");
    const workflow = commands.filter((command) => command.name === "workflow");
    assert.equal(workflow.length, 1, `expected exactly one workflow command, saw ${JSON.stringify(workflow)}`);
    const commandPath = (command) => command.sourceInfo?.path ?? command.path ?? "";
    assert.ok(resolve(commandPath(cook)).startsWith(resolve(repository)), `kas:cook must come from ${repository}, saw ${JSON.stringify(cook)}`);
    assert.ok(resolve(commandPath(workflow[0])).startsWith(resolve(repository)), `workflow command must come from Orkastrator's pi-workflows copy, saw ${JSON.stringify(workflow[0])}`);
    assert.equal(pi.extensionErrors.length, 0, `extension errors during startup: ${JSON.stringify(pi.extensionErrors)}`);
    const state = await pi.request({ type: "get_state" });
    assert.equal(state.data?.model?.provider, "scripted");
    assert.equal(state.data?.model?.id, "lifecycle-model");
    log("commands registered from the installed Orkastrator package");

    // 2. Launch /kas:cook and observe the run start.
    const prompt = await pi.request({ type: "prompt", message: `/kas:cook ${TASK}` });
    assert.equal(prompt.success, true, `prompt rejected: ${prompt.error}`);
    await waitFor(() => launchedRunId, "the workflow tool to start an orkastrator-cook run", 120_000, 250);
    const brokerDescriptors = () => readdirSync(join(runtimeDir, `orkastrator-${process.getuid()}`)).filter((name) => name.startsWith("launch-"));
    await waitFor(() => brokerDescriptors().length > 0 || undefined, "the desktop broker launch descriptor", 15_000);
    log(`broker descriptor bound for launch: ${brokerDescriptors().join(", ")}`);

    // 3. Observe until terminal, following continuation runs across the protected decision.
    let terminal;
    await waitFor(() => {
      const runs = readRuns(WorkflowRunStore, databasePath);
      const chain = lineage(runs, launchedRunId);
      const latest = chain.at(-1);
      if (latest === undefined) return undefined;
      if (["completed", "failed", "timed_out", "cancelled"].includes(latest.status)) {
        terminal = { chain, latest };
        return terminal;
      }
      if (pi.extensionErrors.length > 0) {
        throw new Error(`extension error while running: ${JSON.stringify(pi.extensionErrors)}`);
      }
      if (model.errors.length > 0) {
        throw new Error(`scripted model error: ${model.errors.join("; ")}`);
      }
      return undefined;
    }, "the workflow lineage to reach a terminal state", 20 * 60_000, 500);

    // 4. Assert the lifecycle result.
    assert.equal(terminal.latest.status, "completed", `terminal run ${terminal.latest.runId} ended ${terminal.latest.status}: ${terminal.latest.error ?? ""}`);
    assert.ok(terminal.chain.length >= 2, "a protected decision must have produced at least one continuation run");
    assert.equal(decisions.length >= 1, true, "the plan approval decision must have been presented through the RPC UI boundary");
    assert.ok(decisions.every((decision) => decision.choice !== undefined), "every presented decision offered the continue choice");
    const finalOutput = terminal.latest.finalOutput;
    assert.equal(finalOutput?.status, "completed", `cook final output: ${JSON.stringify(finalOutput).slice(0, 800)}`);
    assert.equal(finalOutput?.review?.exit, "completed", `review exit: ${JSON.stringify(finalOutput?.review).slice(0, 400)}`);
    const seen = scripted.seenNodes.join("\n");
    for (const required of ["resolveRepository", "planning/design/plan", "planning/documentation/updateDocumentation", "implementation/implement", "implementation/planVerification", "implementation/publish", "implementation/assessReview", "implementation/finalizeDelivery"]) {
      assert.ok(seen.includes(required), `expected the scripted model to serve ${required}; served:\n${seen}`);
    }
    const reviewerTurns = model.requests.filter((request) => request.toolNames.includes("structured_output"));
    assert.ok(reviewerTurns.length >= 1, "the initial review must have run as a real pi-subagents reviewer child");
    log(`lineage completed: ${terminal.chain.map((run) => run.runId).join(" -> ")}`);

    // 5. Repository assertions: launch checkout untouched, task worktree fixed and clean.
    assert.equal(git(fixture, "rev-parse", "HEAD"), baseline, "the launch repository HEAD must not move");
    assert.equal(git(fixture, "status", "--porcelain"), "", "the launch repository must stay clean");
    assert.match(readFileSync(join(fixture, "src", "sum.js"), "utf8"), /return a - b;/u, "the launch checkout keeps the defect");
    const worktrees = git(fixture, "worktree", "list", "--porcelain").split("\n\n").map((block) => block.split("\n")[0]?.replace(/^worktree /u, "")).filter((path) => path !== undefined && path !== fixture);
    assert.equal(worktrees.length, 1, `expected one prepared task worktree, saw ${JSON.stringify(worktrees)}`);
    const taskWorktree = worktrees[0];
    const taskBranch = git(taskWorktree, "branch", "--show-current");
    assert.ok(taskBranch.startsWith(FIXTURE_TASK_DIGEST_PREFIX) && taskBranch.endsWith("/task"), `unexpected task branch ${taskBranch}`);
    assert.equal(git(taskWorktree, "status", "--porcelain"), "", "the task worktree must be committed and clean after review");
    assert.match(readFileSync(join(taskWorktree, "src", "sum.js"), "utf8"), /return a \+ b;/u, "the task worktree carries the fix");
    assert.ok(existsSync(join(taskWorktree, "docs", "plan.md")), "autodoc wrote docs/plan.md in the task worktree");
    execFileSync("npm", ["test", "--silent"], { cwd: taskWorktree, stdio: "pipe", env });
    const commits = git(taskWorktree, "rev-list", "--count", `${baseline}..HEAD`);
    assert.equal(commits, "2", `expected the docs and fix commits on the task branch, saw ${commits}`);
    log(`task worktree ${taskWorktree} on ${taskBranch} passes npm test`);

    // 6. Cleanup behaviour of the runtime.
    await waitFor(() => brokerDescriptors().length === 0 || undefined, "broker launch descriptors to be released after the terminal run", 15_000);
    await waitFor(async () => {
      const current = await pi.request({ type: "get_state" });
      return !current.data?.isStreaming && current.data?.pendingMessageCount === 0 ? true : undefined;
    }, "the Pi session to go idle", 60_000, 500);
    assert.equal(pi.extensionErrors.length, 0, `extension errors: ${JSON.stringify(pi.extensionErrors)}`);
    assert.deepEqual(model.errors, [], `the scripted model must never be asked a question it cannot answer: ${JSON.stringify(model.errors)}`);
    assert.ok(scripted.presentations.length >= 1, "the completed workflow must have delivered its presentation prompt to the origin session");
    log("lifecycle passed");
  } catch (error) {
    failed = true;
    const report = `${error instanceof Error ? error.stack ?? error.message : String(error)}\n\n${diagnostics()}`;
    writeFileSync(join(temporary, "failure-report.txt"), `${report}\n\n${logs.join("\n")}\n`);
    throw new Error(`cook lifecycle run ${iteration} failed; artifacts retained at ${temporary}\n${report}`);
  } finally {
    await pi.stop();
    const client = new WorkflowClient({ clientId: `cook-lifecycle-cleanup-${randomUUID()}`, databasePath, env });
    try {
      if (existsSync(databasePath)) await client.request({ operation: "host.stop" });
    } catch {
      // The host was already stopped or never started.
    } finally {
      await client.close().catch(() => undefined);
    }
    await waitFor(() => !existsSync(join(agentDir, "workflows", "host", "host.sock")) || undefined, "the workflow host socket to disappear", 15_000).catch((error) => log(String(error)));
    await model.close();
    if (!failed && !keep) {
      // Task worktrees live beside the fixture inside `temporary`, so one removal covers them.
      rmSync(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    } else {
      writeFileSync(join(temporary, "lifecycle.log"), `${logs.join("\n")}\n`);
      console.log(`artifacts retained at ${temporary}`);
    }
  }
}

for (let iteration = 1; iteration <= runCount; iteration += 1) {
  console.log(`=== cook lifecycle run ${iteration}/${runCount} ===`);
  await runLifecycle(iteration);
}
console.log(`cook lifecycle passed ${runCount} time(s)`);
