import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { promisify } from "node:util";

import {
  validateWorkflowDefinition,
  type WorkflowActionContext,
} from "@osolmaz/pi-workflows";
import type {
  SubagentDelegationRequest,
  SubagentDelegationResponse,
  SubagentDelegationValue,
} from "pi-subagents/delegation";

import reviewWorkflow from "../../../.pi/workflows/orkastrator-review.workflow.ts";
import { planReviewWaves } from "../../orkastrator/review-wave.ts";
import {
  delegateSubagent,
  installDelegationBridge,
  SUBAGENT_DELEGATION_CANCEL_EVENT,
  SUBAGENT_DELEGATION_REQUEST_EVENT,
  SUBAGENT_DELEGATION_RESPONSE_EVENT,
  type DelegationEvents,
} from "../delegation-bridge.ts";
import {
  parseInitialReviewResult,
  parseReReviewVerdict,
  parseReviewWorkflowInput,
  runFixWaves,
  type ReviewWorkflowInput,
} from "../review-runtime.ts";

const execFileAsync = promisify(execFile);

class FakeEvents implements DelegationEvents {
  readonly listeners = new Map<string, Set<(payload: unknown) => void>>();

  on(event: string, handler: (payload: unknown) => void): () => void {
    const handlers = this.listeners.get(event) ?? new Set();
    handlers.add(handler);
    this.listeners.set(event, handlers);
    return () => handlers.delete(handler);
  }

  emit(event: string, payload: unknown): void {
    for (const handler of this.listeners.get(event) ?? []) handler(payload);
  }
}

test("workflow definition is valid and rejects ambiguous input", () => {
  assert.doesNotThrow(() => validateWorkflowDefinition(reviewWorkflow));
  assert.deepEqual(parseReviewWorkflowInput({
    objective: "fix the committed change",
    repository: "/tmp/repository",
    reviewRevision: "a".repeat(40),
  }), {
    objective: "fix the committed change",
    repository: "/tmp/repository",
    reviewRevision: "a".repeat(40),
    maxParallelFixers: 3,
  });
  assert.throws(
    () => parseReviewWorkflowInput({
      objective: "fix it",
      repository: "relative",
      reviewRevision: "a".repeat(40),
    }),
    /absolute path/u,
  );
  assert.throws(
    () => parseReviewWorkflowInput({
      objective: "fix it",
      repository: "/tmp/repository",
      reviewRevision: "a".repeat(40),
      maxParallelFixers: 4,
    }),
    /integer from 1 to 3/u,
  );
});

test("blocking status is derived from review context instead of reviewer discretion", () => {
  const initial = parseInitialReviewResult({
    findings: [
      {
        id: "bug",
        severity: "high",
        category: "correctness",
        contract: "Fix the bug.",
        evidence: ["test fails"],
        implicatedPaths: ["src/bug.js"],
      },
      {
        id: "style",
        severity: "low",
        category: "style",
        contract: "Optional rename.",
        evidence: ["name is awkward"],
        implicatedPaths: [],
      },
    ],
  }, 2);
  assert.equal(initial.findings.find((finding) => finding.id === "bug")?.blocking, true);
  assert.equal(initial.findings.find((finding) => finding.id === "style")?.blocking, false);

  const reReview = parseReReviewVerdict({
    verdict: "accept",
    reason: "scoped contract passes",
    introducedFindings: [
      {
        id: "unrelated",
        severity: "high",
        category: "correctness",
        contract: "Another fixer owns this defect.",
        evidence: ["other test fails"],
        implicatedPaths: ["src/other.js"],
        introducedByFix: false,
      },
      {
        id: "regression",
        severity: "high",
        category: "correctness",
        contract: "The fix introduced a regression.",
        evidence: ["new scoped test fails"],
        implicatedPaths: ["src/bug.js"],
        introducedByFix: true,
      },
    ],
  });
  assert.equal(reReview.introducedFindings[0]?.blocking, false);
  assert.equal(reReview.introducedFindings[1]?.blocking, true);
});

test("delegation bridge correlates one terminal response and cancels on abort", async () => {
  const events = new FakeEvents();
  const uninstall = installDelegationBridge(events, {});
  events.on(SUBAGENT_DELEGATION_REQUEST_EVENT, (payload) => {
    const request = payload as SubagentDelegationRequest;
    events.emit(SUBAGENT_DELEGATION_RESPONSE_EVENT, {
      requestId: "different",
      ownerRunId: request.ownerRunId,
      nodeId: request.nodeId,
      status: "completed",
    } satisfies SubagentDelegationResponse);
    events.emit(SUBAGENT_DELEGATION_RESPONSE_EVENT, {
      requestId: request.requestId,
      ownerRunId: request.ownerRunId,
      nodeId: request.nodeId,
      status: "completed",
      result: { kind: "text", text: "done" },
    } satisfies SubagentDelegationResponse);
  });

  const response = await delegateSubagent({
    ownerRunId: "run-1",
    nodeId: "node-1",
    agent: "worker",
    task: "work",
    context: "fresh",
    cwd: "/tmp",
    result: { kind: "text" },
  }, new AbortController().signal);
  assert.equal(response.result?.kind, "text");

  const abortEvents = new FakeEvents();
  uninstall();
  const uninstallAbort = installDelegationBridge(abortEvents, {});
  let cancelled = false;
  abortEvents.on(SUBAGENT_DELEGATION_CANCEL_EVENT, () => {
    cancelled = true;
  });
  const controller = new AbortController();
  const pending = delegateSubagent({
    ownerRunId: "run-2",
    nodeId: "node-2",
    agent: "worker",
    task: "work",
    context: "fresh",
    cwd: "/tmp",
    result: { kind: "text" },
  }, controller.signal);
  controller.abort();
  await assert.rejects(pending, /aborted/u);
  assert.equal(cancelled, true);
  uninstallAbort();
});

test("bounded fixer groups run concurrently, re-review, and integrate serially", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-workflow-"));
  const repository = join(root, "repo");
  await git(root, ["init", "--initial-branch=main", repository]);
  await git(repository, ["config", "user.name", "Orkastrator Test"]);
  await git(repository, ["config", "user.email", "orkastrator@example.test"]);
  await writeFile(join(repository, "a.txt"), "broken a\n", "utf8");
  await writeFile(join(repository, "b.txt"), "broken b\n", "utf8");
  await git(repository, ["add", "a.txt", "b.txt"]);
  await git(repository, ["commit", "-m", "worker change"]);
  const reviewRevision = await git(repository, ["rev-parse", "HEAD"]);
  const plan = planReviewWaves([
    {
      id: "finding-a",
      severity: "high",
      category: "correctness",
      contract: "a.txt must contain fixed a",
      evidence: ["a.txt is broken"],
      implicatedPaths: ["a.txt"],
      blocking: true,
    },
    {
      id: "finding-b",
      severity: "high",
      category: "acceptance",
      contract: "b.txt must contain fixed b",
      evidence: ["b.txt is broken"],
      implicatedPaths: ["b.txt"],
      blocking: true,
    },
  ], 2);

  const events = new FakeEvents();
  const uninstall = installDelegationBridge(events, {});
  let activeFixers = 0;
  let fixerRequests = 0;
  let maximumActiveFixers = 0;
  events.on(SUBAGENT_DELEGATION_REQUEST_EVENT, (payload) => {
    const request = payload as SubagentDelegationRequest;
    void (async () => {
      if (request.nodeId.includes(":fix:")) {
        fixerRequests += 1;
        activeFixers += 1;
        maximumActiveFixers = Math.max(maximumActiveFixers, activeFixers);
        const writable = /Writable paths: (\[[^\n]+\])/u.exec(request.task)?.[1];
        assert.ok(writable);
        const [path] = JSON.parse(writable) as string[];
        await new Promise((resolve) => setTimeout(resolve, 20));
        await writeFile(join(request.cwd, path!), `fixed ${path![0]}\n`, "utf8");
        await git(request.cwd, ["add", path!]);
        await git(request.cwd, ["commit", "-m", `fix ${request.nodeId}`]);
        activeFixers -= 1;
        respond(events, request, { kind: "text", text: "committed" });
        return;
      }
      if (request.nodeId.includes(":re-review:")) {
        respond(events, request, {
          kind: "structured",
          value: { verdict: "accept", reason: "contract satisfied", introducedFindings: [] },
        });
        return;
      }
      throw new Error(`unexpected delegation ${request.nodeId}`);
    })().catch((error: unknown) => {
      events.emit(SUBAGENT_DELEGATION_RESPONSE_EVENT, {
        requestId: request.requestId,
        ownerRunId: request.ownerRunId,
        nodeId: request.nodeId,
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
      } satisfies SubagentDelegationResponse);
    });
  });

  const input: ReviewWorkflowInput = {
    objective: "repair both files",
    repository,
    reviewRevision,
    maxParallelFixers: 2,
    stateRoot: join(root, "state"),
  };
  const result = await runFixWaves(actionContext(input, "workflow-run-1"), plan);

  assert.equal(result.status, "completed");
  assert.equal(result.accepted.length, 2);
  assert.equal(result.unresolved.length, 0);
  assert.equal(maximumActiveFixers, 2);
  assert.equal(fixerRequests, 2);
  assert.equal(await readFile(join(repository, "a.txt"), "utf8"), "fixed a\n");
  assert.equal(await readFile(join(repository, "b.txt"), "utf8"), "fixed b\n");

  const integratedHead = await git(repository, ["rev-parse", "HEAD"]);
  const repeated = await runFixWaves(actionContext(input, "workflow-run-1"), plan);
  assert.equal(repeated.status, "completed");
  assert.equal(await git(repository, ["rev-parse", "HEAD"]), integratedHead);
  assert.equal(fixerRequests, 2, "a resumed action adopts existing exact fixer commits");
  uninstall();
});

function actionContext(
  input: ReviewWorkflowInput,
  runId: string,
): WorkflowActionContext<ReviewWorkflowInput> {
  return {
    input,
    outputs: {},
    results: {},
    state: { runId },
    signal: new AbortController().signal,
    publishUpdate: async () => ({
      updateId: "update",
      seq: 1,
      at: new Date(0).toISOString(),
      type: "test",
      key: "test",
    }),
  } as unknown as WorkflowActionContext<ReviewWorkflowInput>;
}

function respond(
  events: FakeEvents,
  request: SubagentDelegationRequest,
  result: SubagentDelegationValue,
): void {
  events.emit(SUBAGENT_DELEGATION_RESPONSE_EVENT, {
    requestId: request.requestId,
    ownerRunId: request.ownerRunId,
    nodeId: request.nodeId,
    status: "completed",
    result,
  } satisfies SubagentDelegationResponse);
}

async function git(cwd: string, args: string[]): Promise<string> {
  const result = await execFileAsync("git", ["-C", cwd, ...args], { encoding: "utf8" });
  return result.stdout.trim();
}
