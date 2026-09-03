import {
  action,
  compute,
  defineWorkflow,
  includeWorkflow,
  includedResult,
  type WorkflowNodeContext,
} from "@osolmaz/pi-workflows";
import {
  autodocWorkflow,
  autoplanWorkflow,
  parsePlanApprovalPolicy,
  planApprovalWorkflow,
  type AutodocInput,
  type AutoplanInput,
  type AutoplanReady,
  type DocumentedPlan,
  type PlanApprovalContinue,
  type PlanApprovalPolicy,
  type PlanApprovalResolution,
  type ResolvedPlanApprovalPolicy,
} from "@osolmaz/pi-workflows/builtins";

import {
  createPreparedTaskWorktree,
  type PreparedTaskWorkspace,
} from "../../extensions/orkastrator-workflows/lifecycle-runtime.ts";

export type PlanChangeInput = {
  task: string;
  scope?: string;
  constraints?: string[];
  repository?: string;
  documents?: string[];
  previousPlan?: unknown;
  newEvidence?: unknown;
  approval?: PlanApprovalPolicy;
  preparedWorkspace?: NonNullable<AutodocInput["preparedWorkspace"]>;
  directDefaultBranchAuthorized?: boolean;
  verificationChecks?: NonNullable<AutodocInput["verificationChecks"]>;
};

export type NormalizedPlanChangeInput = Omit<PlanChangeInput, "approval"> & {
  approval: ResolvedPlanApprovalPolicy;
};

export type PlanChangeReady = {
  status: "ready";
  plan: unknown;
  planDigest: string;
  documents: string[];
  revision: number;
  approval: PlanApprovalResolution;
  documentation: { files: string[] };
  preparedWorkspace?: PreparedTaskWorkspace;
};

export type PlanChangeBlocked = {
  status: "blocked";
  reason: string;
  evidence: unknown;
};

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value.trim();
}

function requireExactKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
  label: string,
): void {
  const unexpected = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unexpected.length > 0) throw new Error(`${label} has unknown field ${unexpected[0]}`);
}

function parseStringArray(value: unknown, label: string): string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`${label} must be an array of strings`);
  }
  return [...value] as string[];
}

function parseAutodocOptions(input: Record<string, unknown>): AutodocInput {
  const parsed = autodocWorkflow.input?.({
    task: "validate plan-change options",
    plan: {},
    ...(input.preparedWorkspace === undefined
      ? {}
      : { preparedWorkspace: input.preparedWorkspace }),
    ...(input.directDefaultBranchAuthorized === undefined
      ? {}
      : { directDefaultBranchAuthorized: input.directDefaultBranchAuthorized }),
    ...(input.verificationChecks === undefined
      ? {}
      : { verificationChecks: input.verificationChecks }),
  });
  if (parsed === undefined || parsed instanceof Promise) {
    throw new Error("builtin:autodoc input validation must be synchronous");
  }
  return parsed;
}

function parseInput(value: unknown): NormalizedPlanChangeInput {
  const input = requireRecord(value, "plan change input");
  requireExactKeys(
    input,
    [
      "task",
      "scope",
      "constraints",
      "repository",
      "documents",
      "previousPlan",
      "newEvidence",
      "approval",
      "preparedWorkspace",
      "directDefaultBranchAuthorized",
      "verificationChecks",
    ],
    "plan change input",
  );
  const constraints = parseStringArray(input.constraints, "plan change constraints");
  const documents = parseStringArray(input.documents, "plan change documents");
  const autodocOptions = parseAutodocOptions(input);
  return {
    task: requireString(input.task, "plan change task"),
    ...(input.scope !== undefined
      ? { scope: requireString(input.scope, "plan change scope") }
      : {}),
    ...(constraints !== undefined ? { constraints } : {}),
    ...(input.repository !== undefined
      ? { repository: requireString(input.repository, "plan change repository") }
      : {}),
    ...(documents !== undefined ? { documents } : {}),
    ...(input.previousPlan !== undefined ? { previousPlan: input.previousPlan } : {}),
    ...(input.newEvidence !== undefined ? { newEvidence: input.newEvidence } : {}),
    ...(autodocOptions.preparedWorkspace === undefined
      ? {}
      : { preparedWorkspace: autodocOptions.preparedWorkspace }),
    ...(autodocOptions.directDefaultBranchAuthorized === undefined
      ? {}
      : { directDefaultBranchAuthorized: autodocOptions.directDefaultBranchAuthorized }),
    ...(autodocOptions.verificationChecks === undefined
      ? {}
      : { verificationChecks: autodocOptions.verificationChecks }),
    approval: parsePlanApprovalPolicy(input.approval),
  };
}

function currentDesign(context: Pick<WorkflowNodeContext, "outputs">): AutoplanReady {
  const result = includedResult(autoplanWorkflow, context.outputs.design);
  if (result.exit !== "ready") throw new Error("plan change design did not return a ready plan");
  return result.output;
}

function currentDocumentation(context: Pick<WorkflowNodeContext, "outputs">): DocumentedPlan {
  const result = includedResult(autodocWorkflow, context.outputs.documentation);
  if (result.exit !== "ready") {
    throw new Error("plan change documentation did not return a ready plan");
  }
  return result.output;
}

function currentApproval(context: Pick<WorkflowNodeContext, "outputs">): PlanApprovalContinue {
  const result = includedResult(planApprovalWorkflow, context.outputs.approval);
  if (result.exit !== "continue") {
    throw new Error("plan change approval did not return continue");
  }
  return result.output;
}

function latestReplanInstructions(outputs: Record<string, unknown>): string | undefined {
  if (outputs.approval === undefined) return undefined;
  const result = includedResult(planApprovalWorkflow, outputs.approval);
  return result.exit === "replan" ? result.output.instructions : undefined;
}

function parsePreparedTaskWorkspace(value: unknown): PreparedTaskWorkspace {
  const parsed = parseAutodocOptions({ preparedWorkspace: value }).preparedWorkspace;
  if (parsed === undefined) throw new Error("plan change prepared workspace is missing");
  return parsed;
}

function currentPreparedWorkspace(
  context: Pick<WorkflowNodeContext, "input" | "outputs" | "state">,
): PreparedTaskWorkspace | undefined {
  const request = context.input as NormalizedPlanChangeInput;
  if (request.preparedWorkspace !== undefined) return request.preparedWorkspace;
  if (context.outputs.prepareWorkspace !== undefined) {
    return parsePreparedTaskWorkspace(context.outputs.prepareWorkspace);
  }
  for (let index = context.state.steps.length - 1; index >= 0; index -= 1) {
    const step = context.state.steps[index];
    if (step?.nodeId === "prepareWorkspace" && step.outcome === "ok") {
      return parsePreparedTaskWorkspace(step.output);
    }
  }
  return undefined;
}

function blockedResult(context: WorkflowNodeContext): PlanChangeBlocked {
  if (context.outputs.approval !== undefined) {
    const approval = includedResult(planApprovalWorkflow, context.outputs.approval);
    if (approval.exit === "stop") {
      return {
        status: "blocked",
        reason: "The operator stopped the proposed plan change.",
        evidence: approval.output,
      };
    }
  }
  const candidates = [
    "replanGuard",
    "assessPlan",
    "prepareWorkspace",
    "approval",
    "documentation",
    "design",
  ];
  for (let index = context.state.steps.length - 1; index >= 0; index -= 1) {
    const step = context.state.steps[index];
    if (step === undefined || !candidates.some((candidate) => step.nodeId.startsWith(candidate))) {
      continue;
    }
    const output = step.output as Record<string, unknown> | null;
    if (output !== null && typeof output === "object") {
      const reason = output.reason;
      if (typeof reason === "string" && reason.length > 0) {
        return { status: "blocked", reason, evidence: step.output };
      }
    }
  }
  return {
    status: "blocked",
    reason: "The plan change could not continue within its configured policy.",
    evidence: null,
  };
}

export const planChangeWorkflow = defineWorkflow({
  source: import.meta.url,
  name: "orkastrator-plan-change",
  input: parseInput,
  startAt: "start",
  maxSteps: 400,
  includes: {
    design: includeWorkflow({
      workflow: "builtin:autoplan",
      contract: autoplanWorkflow,
      input: ({ input, outputs }): AutoplanInput => {
        const request = input as NormalizedPlanChangeInput;
        const prior =
          outputs.design === undefined
            ? request.previousPlan
            : (() => {
                const result = includedResult(autoplanWorkflow, outputs.design);
                return result.exit === "ready" ? result.output.plan : request.previousPlan;
              })();
        const instructions = latestReplanInstructions(outputs);
        return {
          problem: request.task,
          ...(request.scope !== undefined ? { scope: request.scope } : {}),
          ...(request.constraints !== undefined ? { constraints: request.constraints } : {}),
          ...(prior !== undefined ? { previousPlan: prior } : {}),
          ...(instructions === undefined
            ? request.newEvidence !== undefined
              ? { newEvidence: request.newEvidence }
              : {}
            : {
                newEvidence: {
                  priorEvidence: request.newEvidence,
                  operatorInstructions: instructions,
                },
              }),
        };
      },
    }),
    documentation: includeWorkflow({
      workflow: "builtin:autodoc",
      contract: autodocWorkflow,
      input: (context): AutodocInput => {
        const request = context.input as NormalizedPlanChangeInput;
        const design = currentDesign(context);
        const workspace = currentPreparedWorkspace(context);
        return {
          task: request.task,
          plan: design.plan,
          ...(request.repository !== undefined ? { repository: request.repository } : {}),
          ...(workspace === undefined
            ? {}
            : { preparedWorkspace: workspace as NonNullable<AutodocInput["preparedWorkspace"]> }),
          ...(request.directDefaultBranchAuthorized === undefined
            ? {}
            : { directDefaultBranchAuthorized: request.directDefaultBranchAuthorized }),
          ...(request.verificationChecks === undefined
            ? {}
            : { verificationChecks: request.verificationChecks as NonNullable<AutodocInput["verificationChecks"]> }),
          ...(request.documents !== undefined ? { documents: request.documents } : {}),
          evidence: request.newEvidence,
        };
      },
    }),
    approval: includeWorkflow({
      workflow: "builtin:plan-approval",
      contract: planApprovalWorkflow,
      input: (context) => {
        const request = context.input as NormalizedPlanChangeInput;
        const documented = currentDocumentation(context);
        const revision =
          context.state.steps.filter((step) => step.nodeId === "approval/routePolicy").length + 1;
        return {
          task: request.task,
          plan: documented.plan,
          planDigest: documented.planDigest,
          approval: request.approval,
          revision,
        };
      },
    }),
  },
  exits: {
    ready: {
      from: "finalize",
      validate: (value: unknown): PlanChangeReady => value as PlanChangeReady,
    },
    blocked: {
      from: "blocked",
      validate: (value: unknown): PlanChangeBlocked => value as PlanChangeBlocked,
    },
  },
  nodes: {
    start: compute({ run: () => ({ route: "design" }) }),
    assessPlan: compute({
      run: (context) => {
        const request = context.input as NormalizedPlanChangeInput;
        const design = currentDesign(context);
        if (request.previousPlan !== undefined && design.changed !== true) {
          return {
            route: "blocked",
            reason: "Planning returned the same plan for the unresolved evidence.",
            evidence: design,
          };
        }
        return {
          route:
            request.repository !== undefined || request.preparedWorkspace !== undefined
              ? "prepare"
              : "document",
          planDigest: design.planDigest,
        };
      },
    }),
    prepareWorkspace: action({
      statusDetail: "preparing the durable task worktree before documentation changes",
      timeoutMs: 30_000,
      effect: {
        type: "orkastrator.prepare-task-worktree",
        idempotencyKey: (context) => `${context.state.runId}:prepare-task-worktree`,
        request: (context: WorkflowNodeContext) => {
          const request = context.input as NormalizedPlanChangeInput;
          return {
            repository: request.repository ?? request.preparedWorkspace?.repository,
            runId: context.state.runId,
          };
        },
        recovery: "idempotent",
      },
      run: async (context) => {
        const existing = currentPreparedWorkspace(context);
        if (existing !== undefined) return existing;
        const request = context.input as NormalizedPlanChangeInput;
        if (request.repository === undefined) {
          throw new Error("plan change workspace preparation requires a repository");
        }
        return await createPreparedTaskWorktree(
          request.repository,
          context.state.runId,
          context.signal,
        );
      },
    }),
    replanGuard: compute({
      run: (context) => {
        const request = context.input as NormalizedPlanChangeInput;
        const replans = context.state.steps.filter(
          (step) => step.nodeId === "approval/replan",
        ).length;
        return replans > request.approval.maxReplans
          ? {
              route: "blocked",
              reason: `Plan approval reached the ${request.approval.maxReplans}-replan safety limit.`,
              replans,
              limit: request.approval.maxReplans,
            }
          : { route: "design", replans, limit: request.approval.maxReplans };
      },
    }),
    finalize: compute({
      run: (context) => {
        const documented = currentDocumentation(context);
        const approval = currentApproval(context);
        const revision = context.state.steps.filter(
          (step) => step.nodeId === "approval/routePolicy",
        ).length;
        const workspace = currentPreparedWorkspace(context);
        return {
          status: "ready",
          plan: documented.plan,
          planDigest: documented.planDigest,
          documents: documented.documentation.files,
          revision,
          approval: approval.resolution,
          documentation: { files: documented.documentation.files },
          ...(workspace === undefined ? {} : { preparedWorkspace: workspace }),
        } satisfies PlanChangeReady;
      },
    }),
    blocked: compute({ run: blockedResult }),
  },
  edges: [
    { from: "start", to: "design" },
    { from: "design.ready", to: "assessPlan" },
    { from: "design.blocked", to: "blocked" },
    {
      from: "assessPlan",
      switch: {
        on: "$.route",
        cases: { prepare: "prepareWorkspace", document: "documentation", blocked: "blocked" },
      },
    },
    { from: "prepareWorkspace", to: "documentation" },
    { from: "documentation.ready", to: "approval" },
    { from: "documentation.blocked", to: "blocked" },
    { from: "approval.continue", to: "finalize" },
    { from: "approval.stop", to: "blocked" },
    { from: "approval.replan", to: "replanGuard" },
    {
      from: "replanGuard",
      switch: { on: "$.route", cases: { design: "design", blocked: "blocked" } },
    },
  ],
});

export default planChangeWorkflow;
