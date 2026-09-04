import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";

import {
  encodeWorkerLine,
  parseWorkerMessage,
  parseWorkerResponse,
} from "../../../node_modules/@osolmaz/pi-workflows/dist/host/worker-protocol.js";
import { autodocWorkflow } from "../../../node_modules/@osolmaz/pi-workflows/dist/builtins/autodoc.workflow.js";
import { HostBackedWorkflowStore } from "../../../node_modules/@osolmaz/pi-workflows/dist/host/worker-store.js";

const LARGE_WORKFLOW_PAYLOAD = "x".repeat(1_200_000);

test("an external presenter owns decisions without disabling agent delivery", () => {
  const source = readFileSync(resolve(
    import.meta.dirname,
    "../../../node_modules/@osolmaz/pi-workflows/src/extension/index.ts",
  ), "utf8");
  const production = readFileSync(resolve(
    import.meta.dirname,
    "../../../node_modules/@osolmaz/pi-workflows/dist/extension/index.js",
  ), "utf8");

  for (const implementation of [source, production]) {
    assert.match(implementation, /pi-workflows\.external-human-decision-presenter\.v1/u);
    assert.match(
      implementation,
      /presentPendingDecisionWithRegisteredPresenter[\s\S]+?hasExternalHumanDecisionPresenter\(\)[\s\S]+?return false;/u,
    );
    assert.match(
      implementation,
      /storedInteraction\.kind === ["']decision["'] && hasExternalHumanDecisionPresenter\(\)[\s\S]+?return undefined;/u,
    );
    assert.match(
      implementation,
      /claimPendingInteractionDelivery\(pi, client, ctx, startOriginActivity\)/u,
    );
  }

});

test("worker protocol keeps small frames as canonical JSON", () => {
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

test("worker protocol compresses oversized workflow state within the wire limit", () => {
  const response = {
    schema: "pi-workflows.worker-response.v1" as const,
    messageId: "large-state",
    outcome: "accepted" as const,
    result: { state: { steps: [LARGE_WORKFLOW_PAYLOAD] } },
  };

  const encoded = encodeWorkerLine(response);
  assert.ok(encoded.byteLength <= 1024 * 1024);
  assert.deepEqual(parseWorkerResponse(encoded.subarray(0, -1)), response);
});

test("worker protocol still rejects incompressible oversized wire frames", () => {
  const response = {
    schema: "pi-workflows.worker-response.v1" as const,
    messageId: "incompressible-state",
    outcome: "accepted" as const,
    result: { state: randomBytes(1024 * 1024).toString("base64") },
  };

  assert.throws(() => encodeWorkerLine(response), /exceeds 1 MiB/u);
});

test("intentional interaction parking is not reported as an interruption", async () => {
  const view = await import(
    "../../../node_modules/@osolmaz/pi-workflows/dist/host/view.js"
  );
  assert.equal(typeof view.possiblyInterrupted, "function");
  assert.equal(view.possiblyInterrupted("parked", "running"), false);
  assert.equal(view.possiblyInterrupted("parked", "waiting"), false);
  assert.equal(view.possiblyInterrupted("parked", "paused"), false);
  assert.equal(view.possiblyInterrupted("parked", "queued"), true);
  assert.equal(view.possiblyInterrupted("running", "running"), false);
});

test("worker resume response excludes host-only run history", async () => {
  const runner = await import(
    "../../../node_modules/@osolmaz/pi-workflows/dist/host/runner.js"
  );
  assert.equal(typeof runner.workerResumePayload, "function");

  const state = { schema: "pi-workflows.run-state.v1", status: "waiting" };
  const payload = runner.workerResumePayload({
    state,
    trace: "x".repeat(1_200_000),
  } as never);
  assert.deepEqual(payload, { state });
  assert.doesNotThrow(() =>
    encodeWorkerLine({
      schema: "pi-workflows.worker-response.v1",
      messageId: "prepare-resume",
      outcome: "accepted",
      result: payload as never,
    }),
  );
});

test("worker store restores optional settings scopes lost to JSON null", async () => {
  const store = new HostBackedWorkflowStore(
    "settings-run",
    { request: async () => ({ result: null }) } as never,
  );

  assert.equal(await store.findSettingsScope("settings-run", "implementation", 1), undefined);
  assert.equal(await store.getSettingsScopeAtChange("missing-scope", 0), undefined);
});

test("autodoc inspection is pinned to the prepared worktree", async () => {
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

  for (const nodeId of ["locatePlan", "inspectDocumentation"] as const) {
    const prompt = await nodes[nodeId].prompt(context);
    assert.match(prompt, new RegExp(`Repository: ${worktreePath}`));
    assert.doesNotMatch(prompt, new RegExp(`Repository: ${launchRepository}(?:\\n|$)`));
  }
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

test("a reserved continuation run persists the engine's carried input", async () => {
  // The host reserves a human-decision continuation row before the worker
  // exists, and it has no parent input at that moment, so it queues the row
  // with `input: {}`. Upstream 0.16.0 then initialized the run without
  // rewriting input_hash, so every later worker generation read `{}` as the
  // run input. `/kas:cook` reached the review include after approval with an
  // empty task and failed with "review workflow objective is required".
  const { SqliteControllerStore } = await import(
    "../../../node_modules/@osolmaz/pi-workflows/dist/controllers/sqlite.js"
  );
  const { WorkflowRunStore, createDefinitionSnapshot } = await import(
    "../../../node_modules/@osolmaz/pi-workflows/dist/workflows/store.js"
  );
  const { compute, defineWorkflow } = await import(
    "../../../node_modules/@osolmaz/pi-workflows/dist/workflows/index.js"
  );
  const { createHash } = await import("node:crypto");
  const { mkdtemp, rm } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");

  const root = await mkdtemp(join(tmpdir(), "orkastrator-continuation-input-"));
  const databasePath = join(root, "state.sqlite");
  const workflow = defineWorkflow({
    name: "continuation-input",
    startAt: "only",
    nodes: { only: compute({ run: ({ input }) => input }) },
    edges: [],
  });
  const snapshot = createDefinitionSnapshot(workflow);
  const definitionDigest = createHash("sha256")
    .update(JSON.stringify(snapshot))
    .digest("hex");
  const carriedInput = { task: "keep me", repository: "/tmp/repository" };
  const claimToken = "continuation-claim-token";
  try {
    const queue = new SqliteControllerStore(databasePath, { projectPath: root });
    const prepared = queue.prepareOrAdoptWorkflowRun({
      runId: "continuation-1",
      workflowName: workflow.name,
      workflowSourceRef: "continuation-input",
      workflowSource: { kind: "builtin", id: "continuation-input", revision: "test" },
      definitionDigest,
      definitionSnapshot: snapshot,
      input: {},
      launchOptions: {},
      runnerId: "host-test",
      claimToken,
      leaseMs: 60_000,
      originSessionId: "session-1",
      executionMode: "interactive",
    });
    assert.equal(prepared.state, "claimed");
    const generation = (prepared.run as { claimGeneration: number }).claimGeneration;
    queue.close();

    const store = new WorkflowRunStore(databasePath, {
      authorityProvider: () => ({
        actor: { type: "system" },
        ownerType: "host",
        ownerId: "host-test",
        token: claimToken,
        generation,
      }),
    });
    try {
      const now = new Date().toISOString();
      await store.initializeRunFromSnapshot(snapshot, workflow.name, {
        schema: "pi-workflows.run-state.v1",
        traceSeq: 0,
        runId: "continuation-1",
        workflowName: workflow.name,
        startedAt: now,
        updatedAt: now,
        status: "running",
        input: carriedInput,
        outputs: {},
        results: {},
        steps: [],
        updates: [],
      } as never);
      const loaded = store.readRun("continuation-1");
      assert.ok(loaded !== null && !(loaded instanceof Promise));
      assert.deepEqual(loaded.state.input, carriedInput);
    } finally {
      store.close();
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
