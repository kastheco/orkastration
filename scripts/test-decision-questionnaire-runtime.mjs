import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const repository = fileURLToPath(new URL("..", import.meta.url));
const workflowsUrl = pathToFileURL(require.resolve("@osolmaz/pi-workflows")).href;
const workflowsExtensionPath = require.resolve("@osolmaz/pi-workflows/extension");
const orkastratorExtensionPath = join(repository, "extensions/orkastrator-workflows/index.ts");
const temporary = mkdtempSync(join(tmpdir(), "orkastrator-decision-runtime-"));
const workflowPath = join(temporary, "decision.workflow.ts");
const databasePath = join(temporary, ".pi", "agent", "workflows", "state.sqlite");
const expectedResponse = {
  choice: "replan",
  input: { instructions: "Use React, not Svelte." },
};

const directImportProbe = spawnSync(
  process.execPath,
  [
    "--input-type=module",
    "-e",
    `const extension = await import(${JSON.stringify(pathToFileURL(workflowsExtensionPath).href)}); console.log(typeof extension.answerExactPendingDecision);`,
  ],
  { cwd: repository, encoding: "utf8" },
);
assert.equal(directImportProbe.status, 0, directImportProbe.stderr);
assert.equal(directImportProbe.stdout.trim(), "undefined");

writeFileSync(
  workflowPath,
  `import {
  choice,
  compute,
  defineHumanChoices,
  defineWorkflow,
  humanDecision,
  humanDecisionEdge,
  textInput,
} from ${JSON.stringify(workflowsUrl)};

const choices = defineHumanChoices({
  continue: choice({ label: "Yes, continue" }),
  replan: choice({
    label: "Replan",
    input: textInput({
      name: "instructions",
      prompt: "What should change?",
      minLength: 1,
      maxLength: 4000,
    }),
  }),
});

export default defineWorkflow({
  name: "decision-runtime-test",
  startAt: "approval",
  nodes: {
    approval: humanDecision({
      audience: "operator",
      choices,
      request: () => ({
        title: "Approve the implementation plan?",
        subject: { plan: "Use the existing React island." },
        presentation: {
          schema: "pi-workflows.decision-presentation.v1",
          summary: "Runtime bridge test",
          blocks: [{ kind: "paragraph", text: "Use the existing React island." }],
        },
      }),
    }),
    continued: compute({ run: () => ({ status: "continued" }) }),
    replanned: compute({ run: () => ({ status: "replanned" }) }),
  },
  edges: [
    humanDecisionEdge({
      from: "approval",
      choices,
      cases: { continue: "continued", replan: "replanned" },
    }),
  ],
});
`,
);

const child = spawn(
  "pi",
  [
    "--mode", "rpc",
    "--no-session",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-context-files",
    "--no-builtin-tools",
    "-e", orkastratorExtensionPath,
  ],
  {
    cwd: repository,
    env: { ...process.env, HOME: temporary },
    stdio: ["pipe", "pipe", "pipe"],
  },
);

let stdout = "";
let stderr = "";
const eventLog = [];
child.stdout.setEncoding("utf8");
child.stderr.setEncoding("utf8");
child.stderr.on("data", (chunk) => { stderr += chunk; });
child.on("error", (error) => { stderr += `\nspawn error: ${error.message}`; });
child.on("exit", (code, signal) => { stderr += `\nchild exit: ${code ?? "null"}/${signal ?? "none"}`; });
child.stdout.on("data", (chunk) => {
  stdout += chunk;
  while (true) {
    const newline = stdout.indexOf("\n");
    if (newline === -1) break;
    const line = stdout.slice(0, newline);
    stdout = stdout.slice(newline + 1);
    if (line.trim() === "") continue;
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      continue;
    }
    eventLog.push(event);
    if (eventLog.length > 50) eventLog.shift();
    if (event.type !== "extension_ui_request") continue;
    if (event.method === "select") {
      child.stdin.write(`${JSON.stringify({
        type: "extension_ui_response",
        id: event.id,
        value: event.options.at(-1),
      })}\n`);
    } else if (event.method === "input") {
      child.stdin.write(`${JSON.stringify({
        type: "extension_ui_response",
        id: event.id,
        value: expectedResponse.input.instructions,
      })}\n`);
    }
  }
});

child.stdin.write(`${JSON.stringify({
  type: "prompt",
  message: `/workflow ${workflowPath} --input-json {}`,
})}\n`);

let hostClient;
try {
  const { HostStateStore } = await import(
    pathToFileURL(join(repository, "node_modules/@osolmaz/pi-workflows/dist/host/state.js")).href
  );
  const { WorkflowClient } = await import(
    pathToFileURL(require.resolve("@osolmaz/pi-workflows/client")).href
  );
  hostClient = new WorkflowClient({ clientId: "runtime-test-cleanup", databasePath });

  const deadline = Date.now() + 20_000;
  let verified = false;
  while (Date.now() < deadline) {
    if (existsSync(databasePath)) {
      const store = new HostStateStore(databasePath, { readOnly: true });
      try {
        const row = store.state.connection
          .prepare("SELECT request_id AS requestId FROM interactive_requests WHERE kind = 'decision' LIMIT 1")
          .get();
        const interaction = row === undefined ? undefined : store.getInteraction(row.requestId);
        const submission = interaction?.acceptedSubmissionId === null
          || interaction?.acceptedSubmissionId === undefined
          ? undefined
          : store.interactionSubmission(interaction.requestId, interaction.acceptedSubmissionId);
        const continuation = interaction === undefined
          ? undefined
          : store.state.connection
              .prepare("SELECT status FROM runs WHERE parent_run_id = ? LIMIT 1")
              .get(interaction.runId);
        if (
          interaction?.status === "settled"
          && (submission?.outcome === "accepted" || submission?.outcome === "adopted")
          && continuation?.status === "completed"
        ) {
          assert.deepEqual(submission.payload, expectedResponse);
          assert.equal(submission.receipt.source.channel, "pi");
          assert.equal(submission.receipt.source.actorId, interaction.targetSessionId);
          assert.equal(submission.receipt.response.choice, "replan");
          assert.equal(submission.receipt.response.input.instructions, "Use React, not Svelte.");
          verified = true;
        }
      } finally {
        store.close();
      }
    }
    if (verified) break;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  if (!verified) {
    throw new Error(
      `Timed out waiting for a durable protected decision.\n${stderr}\n${JSON.stringify(eventLog, null, 2)}`,
    );
  }
  console.log("runtime decision questionnaire passed");
} finally {
  try {
    await hostClient?.request({ operation: "host.stop" });
  } catch {
    // Best-effort cleanup after a failed runtime assertion.
  }
  child.kill("SIGTERM");
  if (child.exitCode === null && child.signalCode === null) {
    await new Promise((resolve) => child.once("exit", resolve));
  }
  rmSync(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
}
