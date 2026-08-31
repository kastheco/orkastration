import {
  action,
  agent,
  compute,
  defineWorkflow,
  includeWorkflow,
  includedResult,
} from "@osolmaz/pi-workflows";
import {
  autoimplementWorkflow,
  type AutoimplementInput,
} from "@osolmaz/pi-workflows/builtins";

import {
  parseLifecycleInput,
  parseOwnerResolvedStatus,
  resolveReviewTarget,
  type OrkastratorLifecycleInput,
} from "../../extensions/orkastrator-workflows/lifecycle-runtime.ts";
import reviewWorkflow from "./orkastrator-review.workflow.ts";

type ImplementationPlan = {
  status: "ready" | "blocked";
  plan?: unknown;
  reason: string;
};

function parseImplementationPlan(value: unknown): ImplementationPlan {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("implementation plan must be an object");
  }
  const plan = value as Record<string, unknown>;
  if (plan.status !== "ready" && plan.status !== "blocked") {
    throw new Error("implementation plan status must be ready or blocked");
  }
  if (typeof plan.reason !== "string" || plan.reason.trim().length === 0) {
    throw new Error("implementation plan reason must be a non-empty string");
  }
  if (plan.status === "ready" && plan.plan === undefined) {
    throw new Error("a ready implementation plan must include plan");
  }
  return {
    status: plan.status,
    ...(plan.plan === undefined ? {} : { plan: plan.plan }),
    reason: plan.reason,
  };
}

export default defineWorkflow<OrkastratorLifecycleInput>({
  source: import.meta.url,
  name: "orkastrator-implement",
  input: parseLifecycleInput,
  title: ({ input }) => `orkastrator implement: ${input.task.slice(0, 60)}`,
  presentationPrompt: ({ finalOutput }) => [
    "Report the complete Orkastrator implementation lifecycle result.",
    "State what was implemented, the validation and delivery result, the Orkastrator review result, and any owner decision still required.",
    JSON.stringify(finalOutput),
  ].join("\n"),
  startAt: "plan",
  maxSteps: 400,
  includes: {
    implementation: includeWorkflow(autoimplementWorkflow, {
      input: ({ input, outputs }): AutoimplementInput => {
        const request = input as OrkastratorLifecycleInput;
        const planning = outputs.plan as ImplementationPlan;
        return {
          task: request.task,
          plan: planning.plan,
          repository: request.repository,
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
    plan: agent({
      statusDetail: "preparing an implementation-ready plan",
      timeoutMs: 45 * 60_000,
      prompt: ({ input }) => [
        "Prepare the smallest implementation-ready plan for this request.",
        "Inspect the repository and use normal user-question tools if a material decision is missing.",
        "Do not edit files, start another workflow, delegate the lifecycle, or claim a blocker when a safe in-scope plan is available.",
        `Request: ${JSON.stringify((input as OrkastratorLifecycleInput).task)}`,
        `Repository: ${(input as OrkastratorLifecycleInput).repository}`,
      ].join("\n"),
      expectedOutput: `{ "status": "ready" | "blocked", "plan": { "summary": "implementation plan", "steps": ["ordered step"], "validation": ["check"] }, "reason": "short justification" }`,
      validate: parseImplementationPlan,
    }),
    classifyPlan: compute({
      run: ({ outputs }) => ({
        route: (outputs.plan as ImplementationPlan).status === "ready"
          ? "implement"
          : "blocked",
      }),
    }),
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
        result: outputs.implementation ?? outputs.plan,
      }),
    }),
    completed: compute({
      run: ({ outputs }) => ({
        status: "completed",
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
          implementation: outputs.implementation,
          review: outputs.review,
        };
      },
    }),
  },
  edges: [
    { from: "plan", to: "classifyPlan" },
    {
      from: "classifyPlan",
      switch: { on: "$.route", cases: { implement: "implementation", blocked: "blocked" } },
    },
    { from: "implementation.completed", to: "prepareReview" },
    { from: "implementation.blocked", to: "blocked" },
    { from: "prepareReview", to: "review" },
    { from: "review.completed", to: "completed" },
    { from: "review.owner_resolved", to: "ownerResolved" },
  ],
});
