import {
  action,
  choice,
  compute,
  defineHumanChoices,
  defineWorkflow,
  humanDecision,
  humanDecisionEdge,
  type WorkflowNodeContext,
} from "@osolmaz/pi-workflows";

import type { FrozenReviewPlan } from "../../extensions/orkastrator-workflows/review-wave.ts";
import {
  parseReviewWorkflowInput,
  runFixWaves,
  runInitialReview,
  type FixWaveResult,
  type ReviewWorkflowInput,
} from "../../extensions/orkastrator-workflows/review-runtime.ts";

const IMPLEMENTATION_REVISION = 7;

const ownerChoices = defineHumanChoices({
  accept_partial: choice({ label: "Accept integrated fixes" }),
  stop: choice({ label: "Stop and preserve evidence" }),
});

export default defineWorkflow<ReviewWorkflowInput>({
  source: import.meta.url,
  name: "orkastrator-review",
  input: parseReviewWorkflowInput,
  presentationPrompt: ({ finalOutput }) => [
    `Report the Orkastrator review workflow result concisely (implementation revision ${IMPLEMENTATION_REVISION}).`,
    "State whether review was clean, all fixes integrated, or owner intervention remains.",
    "Name unresolved fixer groups and preserved worktrees when present.",
    JSON.stringify(finalOutput),
  ].join("\n"),
  startAt: "initialReview",
  maxSteps: 14,
  exits: {
    completed: {
      from: "reviewCompleted",
      validate: (value: unknown) => value,
    },
    owner_resolved: {
      from: "ownerResolved",
      validate: (value: unknown) => value,
    },
  },
  nodes: {
    initialReview: action({
      statusDetail: "running immutable initial review",
      timeoutMs: 35 * 60_000,
      effect: {
        type: "orkastrator.initial-review",
        // The review target is an immutable commit, so the same run reviewing
        // the same revision is the same effect.
        idempotencyKey: (context) => {
          const input = context.input as ReviewWorkflowInput;
          return `${context.state.runId}:initial-review:${input.reviewRevision}`;
        },
        request: (context: WorkflowNodeContext) => {
          const input = context.input as ReviewWorkflowInput;
          return {
            repository: input.repository,
            reviewRevision: input.reviewRevision,
            objective: input.objective,
          };
        },
        // Reviewing only reads the repository and spawns a reviewer child, so an
        // uncertain exit is safe to retry.
        recovery: "idempotent",
      },
      run: async (context) => await runInitialReview(
        context as Parameters<typeof runInitialReview>[0],
      ),
    }),
    classifyReview: compute({
      run: ({ outputs }) => {
        const plan = outputs.initialReview as FrozenReviewPlan;
        return { route: plan.fixerGroups.length === 0 ? "clean" : "fix" };
      },
    }),
    clean: compute({
      run: ({ outputs }) => ({
        status: "completed",
        reason: "initial review found no blocking findings",
        review: outputs.initialReview,
      }),
    }),
    fixWaves: action({
      statusDetail: "running bounded fixer and re-review waves",
      timeoutMs: 4 * 60 * 60_000,
      effect: {
        type: "orkastrator.fix-waves",
        // Frozen finding IDs identify the exact repair attempt, so a resumed run
        // cannot be confused with a different plan over the same revision.
        idempotencyKey: (context) => {
          const input = context.input as ReviewWorkflowInput;
          const plan = context.outputs.initialReview as FrozenReviewPlan;
          const groups = plan.fixerGroups.map((group) => group.groupId).join(",");
          return `${context.state.runId}:fix-waves:${input.reviewRevision}:${groups}`;
        },
        request: (context: WorkflowNodeContext) => {
          const input = context.input as ReviewWorkflowInput;
          const plan = context.outputs.initialReview as FrozenReviewPlan;
          return {
            repository: input.repository,
            reviewRevision: input.reviewRevision,
            fixerGroups: plan.fixerGroups.map((group) => group.groupId),
          };
        },
        // Fix waves create worktrees, commit fixer output, and integrate onto the
        // reviewed branch. Replaying an uncertain exit could duplicate or
        // half-apply that integration, so recovery stays manual.
        recovery: "manual",
      },
      run: async (context) => await runFixWaves(
        context as Parameters<typeof runFixWaves>[0],
        context.outputs.initialReview as FrozenReviewPlan,
      ),
    }),
    classifyFixes: compute({
      run: ({ outputs }) => ({
        route: (outputs.fixWaves as FixWaveResult).status === "completed"
          ? "completed"
          : "needs_owner",
      }),
    }),
    completed: compute({
      run: ({ outputs }) => ({
        status: "completed",
        reason: "every blocking finding passed scoped re-review and integrated serially",
        review: outputs.initialReview,
        fixes: outputs.fixWaves,
      }),
    }),
    ownerDecision: humanDecision({
      audience: "operator",
      choices: ownerChoices,
      request: ({ outputs }) => {
        const result = outputs.fixWaves as FixWaveResult;
        return {
          title: "Resolve remaining Orkastrator review risk",
          subject: result,
          presentation: {
            schema: "pi-workflows.decision-presentation.v1",
            summary: "Some blocking findings or integration conflicts remain after bounded fix rounds.",
            blocks: [
              {
                kind: "bullets",
                items: result.unresolved.map((item) =>
                  `${item.groupId}: ${item.reason} (${item.worktree})`
                ),
              },
            ],
          },
        };
      },
    }),
    acceptedPartial: compute({
      run: ({ outputs }) => ({
        status: "owner_accepted_partial",
        decision: outputs.ownerDecision,
        review: outputs.initialReview,
        fixes: outputs.fixWaves,
      }),
    }),
    stopped: compute({
      run: ({ outputs }) => ({
        status: "stopped",
        decision: outputs.ownerDecision,
        review: outputs.initialReview,
        fixes: outputs.fixWaves,
      }),
    }),
    reviewCompleted: compute({
      run: ({ outputs }) => outputs.completed ?? outputs.clean,
    }),
    ownerResolved: compute({
      run: ({ outputs }) => outputs.acceptedPartial ?? outputs.stopped,
    }),
  },
  edges: [
    { from: "initialReview", to: "classifyReview" },
    {
      from: "classifyReview",
      switch: { on: "$.route", cases: { clean: "clean", fix: "fixWaves" } },
    },
    { from: "fixWaves", to: "classifyFixes" },
    {
      from: "classifyFixes",
      switch: {
        on: "$.route",
        cases: { completed: "completed", needs_owner: "ownerDecision" },
      },
    },
    humanDecisionEdge({
      from: "ownerDecision",
      choices: ownerChoices,
      cases: { accept_partial: "acceptedPartial", stop: "stopped" },
    }),
    { from: "clean", to: "reviewCompleted" },
    { from: "completed", to: "reviewCompleted" },
    { from: "acceptedPartial", to: "ownerResolved" },
    { from: "stopped", to: "ownerResolved" },
  ],
});
