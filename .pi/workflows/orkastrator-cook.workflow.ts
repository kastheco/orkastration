import {
  action,
  agent,
  compute,
  defineWorkflow,
  includeWorkflow,
  includedResult,
  type WorkflowNodeContext,
} from "@osolmaz/pi-workflows";
import {
  autoimplementWorkflow,
  type AutoimplementInput,
} from "@osolmaz/pi-workflows/builtins";

import planChangeWorkflow from "./orkastrator-plan-change.workflow.ts";

import {
  captureRepositoryBaseline,
  parseLifecycleInput,
  parseOwnerResolvedStatus,
  parseRepositoryResolution,
  resolveReviewTarget,
  verifyImplementationRepository,
  type OrkastratorLifecycleInput,
  type RepositoryResolution,
} from "../../extensions/orkastrator-workflows/lifecycle-runtime.ts";
import reviewWorkflow from "./orkastrator-review.workflow.ts";

function resolvedRepository(outputs: Record<string, unknown>): string {
  const resolution = outputs.resolveRepository as RepositoryResolution | undefined;
  if (resolution?.status !== "resolved" || resolution.repository === undefined) {
    throw new Error("implementation requires an evidence-backed repository resolution");
  }
  return resolution.repository;
}

export default defineWorkflow<OrkastratorLifecycleInput>({
  source: import.meta.url,
  name: "orkastrator-cook",
  input: parseLifecycleInput,
  title: ({ input }) => `orkastrator cook: ${input.task.slice(0, 60)}`,
  presentationPrompt: ({ finalOutput }) => [
    "Report the complete Orkastrator cook lifecycle result.",
    "State the approved documented plan, what was implemented, the validation and delivery result, the Orkastrator review result, and any owner decision still required.",
    JSON.stringify(finalOutput),
  ].join("\n"),
  startAt: "resolveRepository",
  maxSteps: 440,
  includes: {
    planning: includeWorkflow(planChangeWorkflow, {
      input: ({ input, outputs }) => {
        const request = input as OrkastratorLifecycleInput;
        return {
          task: request.task,
          repository: resolvedRepository(outputs),
          approval: { mode: "required" as const, audience: "operator", maxReplans: 3 },
        };
      },
    }),
    implementation: includeWorkflow({
      workflow: "builtin:autoimplement",
      contract: autoimplementWorkflow,
      input: ({ input, outputs }): AutoimplementInput => {
        const request = input as OrkastratorLifecycleInput;
        const planning = includedResult(planChangeWorkflow, outputs.planning);
        if (planning.exit !== "ready") {
          throw new Error("implementation requires an approved documented plan");
        }
        const preparedWorkspace = planning.output.preparedWorkspace;
        if (preparedWorkspace === undefined) {
          throw new Error("implementation requires the prepared planning worktree");
        }
        return {
          task: request.task,
          plan: planning.output.plan,
          repository: preparedWorkspace.repository,
          preparedWorkspace,
          documents: planning.output.documents,
          documentation: {
            status: "current",
            planDigest: planning.output.planDigest,
            documents: planning.output.documents,
          },
          merge: false,
        };
      },
    }),
    review: includeWorkflow(reviewWorkflow, {
      input: ({ input, outputs }) => {
        const request = input as OrkastratorLifecycleInput;
        const target = outputs.prepareReview as {
          repository: string;
          reviewRevision: string;
        };
        return {
          objective: request.task,
          repository: target.repository,
          reviewRevision: target.reviewRevision,
          maxParallelFixers: request.maxParallelFixers,
          worktreeRetentionDays: request.worktreeRetentionDays,
          ...(request.herdrLaunch === undefined ? {} : { herdrLaunch: request.herdrLaunch }),
        };
      },
    }),
  },
  nodes: {
    resolveRepository: agent({
      statusDetail: "resolving the implementation repository from ticket evidence",
      timeoutMs: 10 * 60_000,
      prompt: ({ input }) => {
        const request = input as OrkastratorLifecycleInput;
        return [
          "Resolve the single Git repository that owns this implementation before planning or editing.",
          "Inspect the launch repository's AGENTS.md, CLAUDE.md, routing documents, child Git repositories, and task/ticket evidence.",
          "If the task names a ticket, retrieve or inspect that ticket when an available tool can do so. Treat ticket labels, title prefixes, and explicit repository routing as primary evidence.",
          "Code ownership and repository guidance may corroborate the ticket. Do not select a sibling merely because related code exists there.",
          "The selected path must be the launch repository itself or an actual descendant Git repository. Do not edit any repository.",
          "If evidence selects multiple repositories or remains ambiguous, return blocked instead of guessing.",
          `Task: ${JSON.stringify(request.task)}`,
          `Launch repository: ${request.repository}`,
        ].join("\n");
      },
      expectedOutput: `{ "status": "resolved", "repository": "/absolute/git/root", "reason": "why this repository owns the task", "evidence": ["ticket or repository evidence"] } | { "status": "blocked", "reason": "why ownership is ambiguous", "evidence": ["evidence checked"] }`,
      validate: (value, context) => parseRepositoryResolution(
        value,
        (context.input as OrkastratorLifecycleInput).repository,
      ),
    }),
    classifyRepository: compute({
      run: ({ outputs }) => ({
        route: (outputs.resolveRepository as RepositoryResolution).status === "resolved"
          ? "verify"
          : "blocked",
      }),
    }),
    verifyRepository: action({
      statusDetail: "verifying the resolved Git repository",
      timeoutMs: 30_000,
      effect: {
        type: "orkastrator.verify-implementation-repository",
        idempotencyKey: (context) => `${context.state.runId}:verify-implementation-repository`,
        request: (context: WorkflowNodeContext) => ({
          repository: resolvedRepository(context.outputs),
          coordinationRepository: (context.input as OrkastratorLifecycleInput).repository,
        }),
        recovery: "idempotent",
      },
      run: async (context) => await verifyImplementationRepository(
        resolvedRepository(context.outputs),
        (context.input as OrkastratorLifecycleInput).repository,
        context.signal,
      ),
    }),
    captureLaunchBaseline: action({
      statusDetail: "recording the launch repository baseline before workspace preparation",
      timeoutMs: 30_000,
      effect: {
        type: "orkastrator.capture-launch-repository-baseline",
        idempotencyKey: (context) => `${context.state.runId}:capture-launch-repository-baseline`,
        request: (context: WorkflowNodeContext) => ({
          repository: (context.input as OrkastratorLifecycleInput).repository,
        }),
        recovery: "idempotent",
      },
      run: async (context) => await captureRepositoryBaseline(
        (context.input as OrkastratorLifecycleInput).repository,
        context.signal,
      ),
    }),
    prepareReview: action({
      statusDetail: "freezing the committed review target",
      timeoutMs: 30_000,
      effect: {
        type: "orkastrator.prepare-review",
        idempotencyKey: (context) => `${context.state.runId}:prepare-review`,
        request: (context: WorkflowNodeContext) => ({
          implementation: context.outputs.implementation,
        }),
        // Resolving the committed review target only reads git state.
        recovery: "idempotent",
      },
      run: async (context) => {
        const result = includedResult(autoimplementWorkflow, context.outputs.implementation);
        if (result.exit !== "completed") {
          throw new Error("review preparation requires completed autoimplementation");
        }
        return await resolveReviewTarget(result.output, context.signal);
      },
    }),
    blocked: compute({
      run: ({ outputs }) => ({
        status: "blocked",
        stage: outputs.resolveRepository !== undefined
          && (outputs.resolveRepository as RepositoryResolution).status === "blocked"
          ? "repository"
          : outputs.implementation === undefined
            ? "planning"
            : "implementation",
        repositoryResolution: outputs.resolveRepository,
        launchBaseline: outputs.captureLaunchBaseline,
        planning: outputs.planning,
        implementation: outputs.implementation,
      }),
    }),
    completed: compute({
      run: ({ outputs }) => ({
        status: "completed",
        launchBaseline: outputs.captureLaunchBaseline,
        planning: outputs.planning,
        implementation: outputs.implementation,
        review: outputs.review,
      }),
    }),
    ownerResolved: compute({
      run: ({ outputs }) => {
        const review = outputs.review as { exit?: unknown; output?: unknown };
        if (review.exit !== "owner_resolved") {
          throw new Error("owner resolution requires the owner_resolved review exit");
        }
        return {
          status: parseOwnerResolvedStatus(review.output),
          launchBaseline: outputs.captureLaunchBaseline,
          planning: outputs.planning,
          implementation: outputs.implementation,
          review: outputs.review,
        };
      },
    }),
  },
  edges: [
    {
      from: "resolveRepository",
      to: "classifyRepository",
    },
    {
      from: "classifyRepository",
      switch: { on: "$.route", cases: { verify: "verifyRepository", blocked: "blocked" } },
    },
    { from: "verifyRepository", to: "captureLaunchBaseline" },
    { from: "captureLaunchBaseline", to: "planning" },
    { from: "planning.ready", to: "implementation" },
    { from: "planning.blocked", to: "blocked" },
    { from: "implementation.completed", to: "prepareReview" },
    { from: "implementation.blocked", to: "blocked" },
    { from: "prepareReview", to: "review" },
    { from: "review.completed", to: "completed" },
    { from: "review.owner_resolved", to: "ownerResolved" },
  ],
});
