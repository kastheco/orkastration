import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { access, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
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

import cookWorkflow from "../../../.pi/workflows/orkastrator-cook.workflow.ts";
import implementWorkflow from "../../../.pi/workflows/orkastrator-implement.workflow.ts";
import reviewWorkflow from "../../../.pi/workflows/orkastrator-review.workflow.ts";
import {
  createImplementationWorktree,
  parseLifecycleInput,
  parseOwnerResolvedStatus,
  resolveReviewTarget,
  type WorktrunkRunner,
} from "../lifecycle-runtime.ts";
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
  sweepExpiredFixerWorktrees,
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

test("workflow definitions are valid and reject ambiguous input", () => {
  assert.doesNotThrow(() => validateWorkflowDefinition(reviewWorkflow));
  assert.doesNotThrow(() => validateWorkflowDefinition(implementWorkflow));
  assert.doesNotThrow(() => validateWorkflowDefinition(cookWorkflow));
  assert.deepEqual(parseLifecycleInput({
    task: "implement the committed change",
    repository: "/tmp/repository",
  }), {
    task: "implement the committed change",
    repository: "/tmp/repository",
    maxParallelFixers: 3,
    worktreeRetentionDays: 30,
  });
  assert.throws(
    () => parseLifecycleInput({ task: "implement", repository: "relative" }),
    /absolute path/u,
  );
  assert.deepEqual(parseReviewWorkflowInput({
    objective: "fix the committed change",
    repository: "/tmp/repository",
    reviewRevision: "a".repeat(40),
  }), {
    objective: "fix the committed change",
    repository: "/tmp/repository",
    reviewRevision: "a".repeat(40),
    maxParallelFixers: 3,
    worktreeRetentionDays: 30,
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
  assert.throws(
    () => parseReviewWorkflowInput({
      objective: "fix it",
      repository: "/tmp/repository",
      reviewRevision: "a".repeat(40),
      worktreeRetentionDays: 0,
    }),
    /integer from 1 to 365/u,
  );
});

test("Herdr launch bindings parse strictly and propagate into included review workflows", () => {
  const herdrLaunch = {
    version: 1 as const,
    transport: "unix" as const,
    launchId: "123e4567-e89b-42d3-a456-426614174000",
  };
  const lifecycle = parseLifecycleInput({
    task: "implement the committed change",
    repository: "/tmp/repository",
    herdrLaunch,
  });
  assert.deepEqual(lifecycle.herdrLaunch, herdrLaunch);
  const review = parseReviewWorkflowInput({
    objective: "review the committed change",
    repository: "/tmp/repository",
    reviewRevision: "a".repeat(40),
    herdrLaunch,
  });
  assert.deepEqual(review.herdrLaunch, herdrLaunch);
  assert.throws(
    () => parseLifecycleInput({
      task: "implement",
      repository: "/tmp/repository",
      herdrLaunch: { ...herdrLaunch, capability: "model-supplied" },
    }),
    /unknown or missing fields/u,
  );

  for (const workflow of [implementWorkflow, cookWorkflow]) {
    const includeReview = (workflow as unknown as {
      includes: { review: { input(context: unknown): unknown } };
    }).includes.review;
    const included = includeReview.input({
      input: lifecycle,
      outputs: {
        prepareReview: {
          repository: "/tmp/repository",
          reviewRevision: "b".repeat(40),
        },
      },
    } as never) as ReviewWorkflowInput;
    assert.deepEqual(included.herdrLaunch, herdrLaunch);
  }
});

test("implementation workflows create Worktrunk isolation at the required stage", () => {
  assert.equal(implementWorkflow.startAt, "createWorktree");
  assert.deepEqual(implementWorkflow.edges.slice(0, 2), [
    { from: "createWorktree", to: "plan" },
    { from: "plan", to: "classifyPlan" },
  ]);
  assert.equal(
    cookWorkflow.edges.some((edge) => "to" in edge && edge.from === "planning.ready" && edge.to === "createWorktree"),
    true,
  );
  assert.equal(
    cookWorkflow.edges.some((edge) => "to" in edge && edge.from === "createWorktree" && edge.to === "implementation"),
    true,
  );
  assert.equal(
    cookWorkflow.edges.some((edge) => "to" in edge && edge.from === "planning.ready" && edge.to === "implementation"),
    false,
  );
});

test("owner-resolved lifecycle status preserves partial acceptance and stop", () => {
  assert.equal(
    parseOwnerResolvedStatus({ status: "owner_accepted_partial" }),
    "owner_accepted_partial",
  );
  assert.equal(parseOwnerResolvedStatus({ status: "stopped" }), "stopped");
  assert.throws(
    () => parseOwnerResolvedStatus({ status: "completed" }),
    /must be owner_accepted_partial or stopped/u,
  );
});

test("Worktrunk creates and recovers one deterministic implementation worktree", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-worktrunk-"));
  const repository = join(root, "repo");
  const worktree = join(root, "implementation");
  await git(root, ["init", "--initial-branch=main", repository]);
  await git(repository, ["config", "user.name", "Orkastrator Test"]);
  await git(repository, ["config", "user.email", "orkastrator@example.test"]);
  await writeFile(join(repository, "base.txt"), "base\n", "utf8");
  await git(repository, ["add", "base.txt"]);
  await git(repository, ["commit", "-m", "base"]);

  const calls: string[][] = [];
  const runner: WorktrunkRunner = async (source, args) => {
    calls.push(args);
    const branch = args.includes("--create")
      ? args[args.indexOf("--create") + 1]!
      : args[1]!;
    if (args.includes("--create")) {
      if (calls.length > 1) throw new Error("branch already exists");
      await git(source, ["worktree", "add", "-b", branch, worktree, "HEAD"]);
    }
    return JSON.stringify({ action: "created", branch, path: worktree });
  };

  const first = await createImplementationWorktree(
    repository,
    "workflow-run",
    new AbortController().signal,
    runner,
  );
  const repeated = await createImplementationWorktree(
    repository,
    "workflow-run",
    new AbortController().signal,
    runner,
  );

  assert.equal(first.repository, worktree);
  assert.deepEqual(repeated, first);
  assert.match(first.branch, /^orkastrator\/[0-9a-f]{16}\/worker$/u);
  assert.equal(calls[0]?.includes("--base"), true);
  assert.equal(calls[0]?.includes("@"), true);
  assert.equal(calls[0]?.includes("--no-hooks"), true);
  assert.equal(calls[0]?.includes("--no-cd"), true);
  assert.equal(calls[1]?.includes("--create"), true);
  assert.equal(calls[2]?.includes("--create"), false);
  assert.equal(await git(worktree, ["rev-parse", "HEAD"]), first.baseRevision);
});

test("review target requires one reported clean implementation repository", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-review-target-"));
  const repository = join(root, "repo");
  await git(root, ["init", "--initial-branch=main", repository]);
  await git(repository, ["config", "user.name", "Orkastrator Test"]);
  await git(repository, ["config", "user.email", "orkastrator@example.test"]);
  await writeFile(join(repository, "change.txt"), "committed\n", "utf8");
  await git(repository, ["add", "change.txt"]);
  await git(repository, ["commit", "-m", "implementation"]);

  const completed = {
    status: "completed" as const,
    task: "implement",
    plan: {},
    implementation: { repositories: [repository] },
    verification: {},
    reviewRounds: [],
    ci: {},
    delivery: { repositories: [] },
  };
  const target = await resolveReviewTarget(completed, new AbortController().signal);
  assert.equal(target.repository, repository);
  assert.equal(target.reviewRevision, await git(repository, ["rev-parse", "HEAD"]));

  await assert.rejects(
    resolveReviewTarget(
      { ...completed, implementation: {}, delivery: {} },
      new AbortController().signal,
    ),
    /requires one reported implementation repository, observed 0/u,
  );

  await writeFile(join(repository, "dirty.txt"), "dirty\n", "utf8");
  await assert.rejects(
    resolveReviewTarget(completed, new AbortController().signal),
    /must be clean/u,
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
        activeFixers -= 1;
        respond(events, request, { kind: "text", text: "left verified changes for the supervisor" });
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
    worktreeRetentionDays: 30,
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
  assert.equal(reReviewTasks.every((task) => task.includes("Exact fixer diff:")), true);
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
  assert.deepEqual(repeated.retentionWarnings, []);

  const repositoryDigest = createHash("sha256").update(repository).digest("hex").slice(0, 12);
  const markerDirectory = join(
    input.stateRoot!,
    `repo-${repositoryDigest}`,
    "workflow-run-1",
    ".retention",
  );
  const markerFiles = await readdir(markerDirectory);
  assert.equal(markerFiles.length, 2);
  let integratedWorktree: string | undefined;
  for (const markerFile of markerFiles) {
    const markerPath = join(markerDirectory, markerFile);
    const record = JSON.parse(await readFile(markerPath, "utf8")) as Record<string, unknown>;
    if (record.status === "integrated") {
      integratedWorktree = record.worktree as string;
      record.deleteAfter = new Date(0).toISOString();
      await writeFile(markerPath, `${JSON.stringify(record, null, 2)}\n`, "utf8");
    }
  }
  assert.ok(integratedWorktree);
  await writeFile(join(integratedWorktree, "untracked-evidence.txt"), "preserve me\n", "utf8");
  const dirtySweep = await sweepExpiredFixerWorktrees(input, new AbortController().signal);
  assert.equal(dirtySweep.removed.length, 0);
  assert.equal(dirtySweep.preserved.length, 2);
  await rm(join(integratedWorktree, "untracked-evidence.txt"));

  const sweep = await sweepExpiredFixerWorktrees(input, new AbortController().signal);
  assert.equal(sweep.removed.length, 1);
  assert.equal(sweep.preserved.length, 1);
  await assert.rejects(access(sweep.removed[0]!), /ENOENT/u);
  await access(repeated.unresolved[0]!.worktree);
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
    worktreeRetentionDays: 30,
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
      worktreeRetentionDays: 30,
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
