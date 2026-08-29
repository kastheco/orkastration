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
import { planReviewWaves } from "../review-wave.ts";
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
        writablePaths: ["src/bug.js"],
      },
      {
        id: "style",
        severity: "low",
        category: "style",
        contract: "Optional rename.",
        evidence: ["name is awkward"],
        implicatedPaths: [],
        writablePaths: [],
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
        writablePaths: ["src/other.js"],
        introducedByFix: false,
      },
      {
        id: "regression",
        severity: "high",
        category: "correctness",
        contract: "The fix introduced a regression.",
        evidence: ["new scoped test fails"],
        implicatedPaths: ["src/bug.js"],
        writablePaths: ["src/bug.js"],
        introducedByFix: true,
      },
    ],
  });
  assert.equal(
    reReview.introducedFindings.find((finding) => finding.id === "unrelated")?.blocking,
    false,
  );
  assert.equal(
    reReview.introducedFindings.find((finding) => finding.id === "regression")?.blocking,
    true,
  );
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
      writablePaths: ["a.txt"],
      blocking: true,
    },
    {
      id: "finding-b",
      severity: "high",
      category: "acceptance",
      contract: "b.txt must contain fixed b",
      evidence: ["b.txt is broken"],
      implicatedPaths: ["b.txt"],
      writablePaths: ["b.txt"],
      blocking: true,
    },
  ], 2);

  const events = new FakeEvents();
  const uninstall = installDelegationBridge(events, {});
  let activeFixers = 0;
  let fixerRequests = 0;
  let maximumActiveFixers = 0;
  let reportNovelDeferredFinding = false;
  const reReviewTasks: string[] = [];
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
        reReviewTasks.push(request.task);
        const novel = reportNovelDeferredFinding
          && request.nodeId.includes(plan.fixerGroups[0]!.groupId);
        respond(events, request, {
          kind: "structured",
          value: {
            verdict: "accept",
            reason: "contract satisfied",
            introducedFindings: novel
              ? [{
                  id: "novel-out-of-scope",
                  severity: "high",
                  category: "correctness",
                  contract: "c.txt must preserve its contract",
                  evidence: ["c.txt contains a novel defect"],
                  implicatedPaths: ["c.txt"],
                  writablePaths: ["c.txt"],
                  introducedByFix: false,
                }]
              : [],
          },
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
  assert.equal(reReviewTasks.length, 2);
  assert.equal(reReviewTasks.every((task) => task.includes("Known sibling findings")), true);
  assert.equal(await readFile(join(repository, "a.txt"), "utf8"), "fixed a\n");
  assert.equal(await readFile(join(repository, "b.txt"), "utf8"), "fixed b\n");

  const integratedHead = await git(repository, ["rev-parse", "HEAD"]);
  reportNovelDeferredFinding = true;
  const repeated = await runFixWaves(actionContext(input, "workflow-run-1"), plan);
  assert.equal(repeated.status, "needs_owner");
  assert.equal(repeated.unresolved.length, 1);
  assert.match(repeated.unresolved[0]!.reason, /final reconciliation/);
  assert.equal(await git(repository, ["rev-parse", "HEAD"]), integratedHead);
  assert.equal(fixerRequests, 2, "a resumed action adopts existing exact fixer commits");
  uninstall();
});

test("a novel deferred finding from a rejected round blocks later acceptance", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-deferred-"));
  const repository = join(root, "repo");
  await git(root, ["init", "--initial-branch=main", repository]);
  await git(repository, ["config", "user.name", "Orkastrator Test"]);
  await git(repository, ["config", "user.email", "orkastrator@example.test"]);
  await writeFile(join(repository, "a.txt"), "broken\n", "utf8");
  await git(repository, ["add", "a.txt"]);
  await git(repository, ["commit", "-m", "worker change"]);
  const reviewRevision = await git(repository, ["rev-parse", "HEAD"]);
  const plan = planReviewWaves([{
    id: "finding-a",
    severity: "high",
    category: "correctness",
    contract: "a.txt must be fixed",
    evidence: ["a.txt is broken"],
    implicatedPaths: ["a.txt"],
    writablePaths: ["a.txt"],
    blocking: true,
  }], 1);
  const events = new FakeEvents();
  const uninstall = installDelegationBridge(events, {});
  let fixerRequests = 0;
  events.on(SUBAGENT_DELEGATION_REQUEST_EVENT, (payload) => {
    const request = payload as SubagentDelegationRequest;
    void (async () => {
      if (request.nodeId.includes(":fix:")) {
        fixerRequests += 1;
        await writeFile(join(request.cwd, "a.txt"), "fixed\n", "utf8");
        await git(request.cwd, ["add", "a.txt"]);
        await git(request.cwd, ["commit", "-m", "fix a"]);
        respond(events, request, { kind: "text", text: "committed" });
        return;
      }
      if (request.nodeId.includes(":re-review:1")) {
        respond(events, request, {
          kind: "structured",
          value: {
            verdict: "reject",
            reason: "fix introduced a regression",
            introducedFindings: [
              {
                id: "novel-deferred",
                severity: "high",
                category: "correctness",
                contract: "novel.txt must preserve its contract",
                evidence: ["novel defect"],
                implicatedPaths: ["novel.txt"],
                writablePaths: ["novel.txt"],
                introducedByFix: false,
              },
              {
                id: "introduced-regression",
                severity: "high",
                category: "correctness",
                contract: "a.txt must not regress",
                evidence: ["regression"],
                implicatedPaths: ["a.txt"],
                writablePaths: ["a.txt"],
                introducedByFix: true,
              },
            ],
          },
        });
        return;
      }
      if (request.nodeId.includes(":re-review:2")) {
        respond(events, request, {
          kind: "structured",
          value: { verdict: "accept", reason: "fixed", introducedFindings: [] },
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

  const result = await runFixWaves(actionContext({
    objective: "repair a",
    repository,
    reviewRevision,
    maxParallelFixers: 1,
    stateRoot: join(root, "state"),
  }, "deferred-run"), plan);

  assert.equal(fixerRequests, 2);
  assert.equal(result.accepted[0]?.rounds, 2);
  assert.deepEqual(result.accepted[0]?.deferredFindings.map((finding) => finding.id), ["novel-deferred"]);
  assert.equal(result.status, "needs_owner");
  assert.match(result.unresolved[0]?.reason ?? "", /final reconciliation/);
  uninstall();
});

test("scope enforcement includes paths removed by a rename", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-scope-"));
  const repository = join(root, "repo");
  await git(root, ["init", "--initial-branch=main", repository]);
  await git(repository, ["config", "user.name", "Orkastrator Test"]);
  await git(repository, ["config", "user.email", "orkastrator@example.test"]);
  await git(repository, ["config", "diff.renames", "true"]);
  await writeFile(join(repository, "out-of-scope.txt"), "content\n", "utf8");
  await git(repository, ["add", "out-of-scope.txt"]);
  await git(repository, ["commit", "-m", "worker change"]);
  const reviewRevision = await git(repository, ["rev-parse", "HEAD"]);
  const plan = planReviewWaves([{
    id: "finding",
    severity: "high",
    category: "correctness",
    contract: "writable.txt must exist",
    evidence: ["missing file"],
    implicatedPaths: ["writable.txt"],
    writablePaths: ["writable.txt"],
    blocking: true,
  }], 1);
  const events = new FakeEvents();
  const uninstall = installDelegationBridge(events, {});
  let reReviews = 0;
  events.on(SUBAGENT_DELEGATION_REQUEST_EVENT, (payload) => {
    const request = payload as SubagentDelegationRequest;
    void (async () => {
      if (request.nodeId.includes(":fix:")) {
        await git(request.cwd, ["mv", "out-of-scope.txt", "writable.txt"]);
        await git(request.cwd, ["commit", "-m", "rename into scope"]);
        respond(events, request, { kind: "text", text: "committed" });
        return;
      }
      if (request.nodeId.includes(":re-review:")) reReviews += 1;
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

  await assert.rejects(
    runFixWaves(actionContext({
      objective: "create writable file",
      repository,
      reviewRevision,
      maxParallelFixers: 1,
      stateRoot: join(root, "state"),
    }, "scope-run"), plan),
    /outside declared scope: out-of-scope\.txt/u,
  );
  assert.equal(reReviews, 0);
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
