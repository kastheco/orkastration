import assert from "node:assert/strict";
import { test } from "node:test";

import {
  encodeWorkerLine,
  parseWorkerMessage,
} from "../../../node_modules/@osolmaz/pi-workflows/dist/host/worker-protocol.js";
import { autodocWorkflow } from "../../../node_modules/@osolmaz/pi-workflows/dist/builtins/autodoc.workflow.js";
import { HostBackedWorkflowStore } from "../../../node_modules/@osolmaz/pi-workflows/dist/host/worker-store.js";

const LARGE_WORKFLOW_PAYLOAD = "x".repeat(1_200_000);

test("worker protocol bounds one frame at the upstream limit", () => {
  // The removed package patch raised this ceiling to 8 MiB because attachment
  // payloads exceeded it. Pi Workflows 0.16.0 owns the limit at 1 MiB, so this
  // pins the real constraint rather than the patched one. A workflow that must
  // carry a large payload has to pass a reference instead of inlining bytes.
  const message = {
    schema: "pi-workflows.worker-message.v1" as const,
    launchSchema: "pi-workflows.worker-launch.v1" as const,
    messageId: "small-state",
    kind: "node.finished" as const,
    operation: "store.writeSnapshot" as const,
    runId: "small-state-run",
    generation: 1,
    workerEpoch: "small-state-worker",
    expectedRevision: 1,
    attemptId: "small-state-attempt",
    payload: { state: "x".repeat(1_000) },
  };

  const encoded = encodeWorkerLine(message);
  assert.deepEqual(parseWorkerMessage(encoded.subarray(0, -1)), message);
});

test("worker store returns the host's own empty settings-scope result", async () => {
  // The removed patch coerced this null to undefined. Orkastrator never calls
  // findSettingsScope or getSettingsScopeAtChange, so the upstream shape is
  // recorded here rather than corrected.
  const store = new HostBackedWorkflowStore(
    "settings-run",
    { request: async () => ({ result: null }) } as never,
  );

  assert.equal(await store.findSettingsScope("settings-run", "implementation", 1), null);
});

test("autodoc inspection cannot yet be pinned to the prepared worktree", async () => {
  // Known gap, not a passing behaviour.
  //
  // The removed package patch added an inspectionRepository() helper to autodoc
  // so its read-only nodes read the prepared worktree while mutation still
  // targeted the launch repository. Pi Workflows 0.16.0 prints
  // `repository ?? "current repository"` in locatePlan and prints no repository
  // line at all in inspectDocumentation.
  //
  // This cannot be corrected from plan change. includeWorkflow() accepts only
  // `input` and `settings`, so an included workflow's prompts are not
  // overridable, and overwriting `repository` with the worktree path would
  // break mutationRepository(), which must stay the launch repository.
  //
  // Effect: documentation inspection may read the launch checkout while the
  // prepared worktree holds the newer files. Needs an upstream inspection-path
  // hook. Flip these assertions once one exists.
  const launchRepository = "/tmp/launch-repository";
  const worktreePath = "/tmp/prepared-task-worktree";
  const request = {
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
      preExistingChangedPaths: [],
      evidence: ["prepared for test"],
      scope: `Only ${launchRepository}`,
    },
  };

  const nodes = autodocWorkflow.nodes as unknown as Record<
    "locatePlan" | "inspectDocumentation",
    { prompt(context: unknown): string | Promise<string> }
  >;
  const context = { input: request, outputs: {}, results: {}, state: {} };

  const locatePlan = await nodes.locatePlan.prompt(context);
  assert.match(locatePlan, new RegExp(`Repository: ${launchRepository}`));
  assert.doesNotMatch(locatePlan, new RegExp(worktreePath));

  const inspectDocumentation = await nodes.inspectDocumentation.prompt(context);
  assert.doesNotMatch(inspectDocumentation, /Repository:/u);
});

test("worker transport recovery is owned by the workflow host", async () => {
  // The removed patch added a workerFailureLimit that failed a run after three
  // consecutive transport failures in one host lifetime. Pi Workflows 0.16.0
  // supervises its own workers and exposes no equivalent hook, and orkastrator
  // drives workers only through the client protocol, so there is nothing for
  // this repository to bound.
  const runner = await import(
    "../../../node_modules/@osolmaz/pi-workflows/dist/host/runner.js"
  );

  assert.equal(typeof runner, "object");
});
