import {
  action,
  compute,
  defineWorkflow,
  includeWorkflow,
  includedResult,
} from "@osolmaz/pi-workflows";
import {
  autoimplementWorkflow,
  planChangeWorkflow,
  type AutoimplementInput,
} from "@osolmaz/pi-workflows/builtins";

import {
  parseLifecycleInput,
  parseOwnerResolvedStatus,
  resolveReviewTarget,
  type OrkastratorLifecycleInput,
} from "../../extensions/orkastrator-workflows/lifecycle-runtime.ts";
import reviewWorkflow from "./orkastrator-review.workflow.ts";

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
  startAt: "start",
  maxSteps: 440,
  includes: {
    planning: includeWorkflow(planChangeWorkflow, {
      input: ({ input }) => {
        const request = input as OrkastratorLifecycleInput;
        return {
          task: request.task,
          repository: request.repository,
          approval: { mode: "required" as const, audience: "operator", maxReplans: 3 },
        };
      },
    }),
    implementation: includeWorkflow(autoimplementWorkflow, {
      input: ({ input, outputs }): AutoimplementInput => {
        const request = input as OrkastratorLifecycleInput;
        const planning = includedResult(planChangeWorkflow, outputs.planning);
        if (planning.exit !== "ready") {
          throw new Error("implementation requires an approved documented plan");
        }
        return {
          task: request.task,
          plan: planning.output.plan,
          repository: request.repository,
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
        };
      },
    }),
  },
  nodes: {
    start: compute({ run: () => ({ route: "plan" }) }),
    prepareReview: action({
      statusDetail: "freezing the committed review target",
      timeoutMs: 30_000,
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
        stage: outputs.implementation === undefined ? "planning" : "implementation",
        planning: outputs.planning,
        implementation: outputs.implementation,
      }),
    }),
    completed: compute({
      run: ({ outputs }) => ({
        status: "completed",
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
          planning: outputs.planning,
          implementation: outputs.implementation,
          review: outputs.review,
        };
      },
    }),
  },
  edges: [
    { from: "start", to: "planning" },
    { from: "planning.ready", to: "implementation" },
    { from: "planning.blocked", to: "blocked" },
    { from: "implementation.completed", to: "prepareReview" },
    { from: "implementation.blocked", to: "blocked" },
    { from: "prepareReview", to: "review" },
    { from: "review.completed", to: "completed" },
    { from: "review.owner_resolved", to: "ownerResolved" },
  ],
});
