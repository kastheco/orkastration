import assert from "node:assert/strict";
import { test } from "node:test";

import {
  encodeWorkerLine,
  parseWorkerMessage,
} from "../../../node_modules/@osolmaz/pi-workflows/dist/host/worker-protocol.js";

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
