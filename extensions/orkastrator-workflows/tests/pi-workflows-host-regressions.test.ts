import assert from "node:assert/strict";
import { test } from "node:test";

import {
  encodeWorkerLine,
  parseWorkerMessage,
} from "../../../node_modules/@osolmaz/pi-workflows/dist/host/worker-protocol.js";
import { autodocWorkflow } from "../../../node_modules/@osolmaz/pi-workflows/dist/builtins/autodoc.workflow.js";
import { HostBackedWorkflowStore } from "../../../node_modules/@osolmaz/pi-workflows/dist/host/worker-store.js";

const LARGE_WORKFLOW_PAYLOAD = "x".repeat(1_200_000);

test("worker protocol round-trips workflow state larger than 1 MiB", () => {
  const message = {
    schema: "pi-workflows.worker-message.v1" as const,
    launchSchema: "pi-workflows.worker-launch.v1" as const,
    messageId: "large-state",
    kind: "node.finished" as const,
    operation: "store.writeSnapshot" as const,
    runId: "large-state-run",
    generation: 1,
    workerEpoch: "large-state-worker",
    expectedRevision: 1,
    attemptId: "large-state-attempt",
    payload: { state: LARGE_WORKFLOW_PAYLOAD },
  };

  const encoded = encodeWorkerLine(message);
  assert.ok(encoded.byteLength > 1024 * 1024);
  assert.ok(encoded.byteLength < 8 * 1024 * 1024);
  assert.deepEqual(parseWorkerMessage(encoded.subarray(0, -1)), message);
});

test("worker store restores optional settings scopes lost to JSON null", async () => {
  const store = new HostBackedWorkflowStore(
    "settings-run",
    {
      request: async () => ({ result: null }),
    } as never,
  );

  assert.equal(await store.findSettingsScope("settings-run", "implementation", 1), undefined);
  assert.equal(await store.getSettingsScopeAtChange("missing-scope", 0), undefined);
});

test("autodoc inspection is pinned to the prepared worktree", async () => {
  const launchRepository = "/tmp/launch-repository";
  const worktreePath = "/tmp/prepared-task-worktree";
  const input = {
    task: "record the selected plan",
    plan: { steps: ["update canonical docs"] },
    repository: launchRepository,
    preparedWorkspace: {
      schema: "pi-workflows.prepared-workspace.v1",
      mode: "worktree",
      repository: launchRepository,
      worktreePath,
      baseBranch: "main",
      baseRevision: "a".repeat(40),
      workBranch: "orkastrator/task",
      directDefaultBranchAuthorized: false,
      preExistingChangedPaths: ["user-notes.md"],
      evidence: ["prepared for test"],
      scope: `Only ${launchRepository}`,
    },
  };
  const nodes = autodocWorkflow.nodes as unknown as Record<
    "locatePlan" | "inspectDocumentation",
    { prompt(context: unknown): string | Promise<string> }
  >;
  const context = { input, outputs: {}, results: {}, state: {} };

  for (const nodeId of ["locatePlan", "inspectDocumentation"] as const) {
    const prompt = await nodes[nodeId].prompt(context);
    assert.match(prompt, new RegExp(`Repository: ${worktreePath}`));
    assert.doesNotMatch(prompt, new RegExp(`Repository: ${launchRepository}(?:\\n|$)`));
  }
});

test("deterministic worker transport failures receive bounded recovery", async () => {
  const runner = await import(
    "../../../node_modules/@osolmaz/pi-workflows/dist/host/runner.js"
  ) as Record<string, unknown>;
  const decide = runner.workerFailureRecoveryDecision;
  assert.equal(typeof decide, "function");
  if (typeof decide !== "function") return;

  const decisions = [1, 2, 3].map((count) => (
    decide as (failure: { count: number; diagnostic: string }) => {
      action: string;
      delayMs: number;
    }
  )({ count, diagnostic: "Worker protocol message exceeds 8 MiB" }));

  assert.deepEqual(decisions, [
    { action: "retry", delayMs: 1_000 },
    { action: "retry", delayMs: 2_000 },
    { action: "fail", delayMs: 0 },
  ]);
});
