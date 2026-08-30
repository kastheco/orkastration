import { execFile } from "node:child_process";
import { realpath } from "node:fs/promises";
import { isAbsolute, resolve } from "node:path";
import { promisify } from "node:util";

import type { WorkflowActionContext } from "@osolmaz/pi-workflows";

import {
  buildReReviewPacket,
  parseInitialReviewOutput,
  type FixerGroup,
  type FrozenReviewPlan,
  type InitialReviewFinding,
} from "./review-wave.ts";
import { delegateSubagent, type DelegationSpec } from "./delegation-bridge.ts";
import {
  ensureOwnedFixerWorktree,
  recordFixerWorktreeOutcome,
  sweepExpiredFixerWorktrees,
} from "./worktree-retention.ts";

export { sweepExpiredFixerWorktrees } from "./worktree-retention.ts";

const execFileAsync = promisify(execFile);
const SHA = /^[0-9a-f]{40}$/u;
const MAX_GIT_OUTPUT = 1_048_576;
const DEFAULT_WORKTREE_RETENTION_DAYS = 30;
const MAX_WORKTREE_RETENTION_DAYS = 365;
const INITIAL_REVIEW_SCHEMA = reviewEnvelopeSchema();
const RE_REVIEW_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["verdict", "reason", "introducedFindings"],
  properties: {
    verdict: { enum: ["accept", "reject"] },
    reason: { type: "string", minLength: 1, maxLength: 4_000 },
    introducedFindings: {
      type: "array",
      maxItems: 100,
      items: {
        ...findingSchema(),
        required: [...findingSchema().required, "introducedByFix"],
        properties: {
          ...findingSchema().properties,
          introducedByFix: { type: "boolean" },
        },
      },
    },
  },
} as const;

export interface ReviewWorkflowInput {
  objective: string;
  repository: string;
  reviewRevision: string;
  maxParallelFixers: number;
  worktreeRetentionDays: number;
  stateRoot?: string;
}

export interface AcceptedFix {
  groupId: string;
  commitSha: string;
  changedPaths: string[];
  rounds: number;
  worktree: string;
  deferredFindings: InitialReviewFinding[];
}

export interface UnresolvedFix {
  groupId: string;
  reason: string;
  evidence: string[];
  worktree: string;
}

export interface FixWaveResult {
  status: "completed" | "needs_owner";
  baseRevision: string;
  integratedHead: string;
  accepted: AcceptedFix[];
  unresolved: UnresolvedFix[];
  retentionWarnings: string[];
}

export interface ReReviewVerdict {
  verdict: "accept" | "reject";
  reason: string;
  introducedFindings: Array<InitialReviewFinding & { introducedByFix: boolean }>;
}

export function parseReviewWorkflowInput(value: unknown): ReviewWorkflowInput {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("review workflow input must be an object");
  }
  const input = value as Record<string, unknown>;
  const allowed = new Set([
    "maxParallelFixers",
    "objective",
    "repository",
    "reviewRevision",
    "stateRoot",
    "worktreeRetentionDays",
  ]);
  const unknown = Object.keys(input).find((key) => !allowed.has(key));
  if (unknown !== undefined) throw new Error(`review workflow input has unknown field ${unknown}`);
  if (typeof input.objective !== "string" || input.objective.trim().length === 0) {
    throw new Error("review workflow objective is required");
  }
  if (typeof input.repository !== "string" || !isAbsolute(input.repository)) {
    throw new Error("review workflow repository must be an absolute path");
  }
  if (typeof input.reviewRevision !== "string" || !SHA.test(input.reviewRevision)) {
    throw new Error("review workflow reviewRevision must be a lowercase 40-character Git SHA");
  }
  const maxParallelFixers = input.maxParallelFixers ?? 3;
  if (!Number.isSafeInteger(maxParallelFixers) || (maxParallelFixers as number) < 1 || (maxParallelFixers as number) > 3) {
    throw new Error("review workflow maxParallelFixers must be an integer from 1 to 3");
  }
  const worktreeRetentionDays = input.worktreeRetentionDays ?? DEFAULT_WORKTREE_RETENTION_DAYS;
  if (
    !Number.isSafeInteger(worktreeRetentionDays)
    || (worktreeRetentionDays as number) < 1
    || (worktreeRetentionDays as number) > MAX_WORKTREE_RETENTION_DAYS
  ) {
    throw new Error("review workflow worktreeRetentionDays must be an integer from 1 to 365");
  }
  if (input.stateRoot !== undefined && (typeof input.stateRoot !== "string" || !isAbsolute(input.stateRoot))) {
    throw new Error("review workflow stateRoot must be an absolute path");
  }
  return {
    objective: input.objective,
    repository: input.repository,
    reviewRevision: input.reviewRevision,
    maxParallelFixers: maxParallelFixers as number,
    worktreeRetentionDays: worktreeRetentionDays as number,
    ...(input.stateRoot === undefined ? {} : { stateRoot: input.stateRoot as string }),
  };
}

export async function runInitialReview(
  context: WorkflowActionContext<ReviewWorkflowInput>,
): Promise<FrozenReviewPlan> {
  const input = parseReviewWorkflowInput(context.input);
  const repository = await verifyReviewRepository(input, context.signal, true);
  await sweepExpiredFixerWorktrees(input, context.signal);
  const response = await delegateSubagent({
    ownerRunId: context.state.runId,
    nodeId: "initial-review",
    agent: "reviewer",
    task: initialReviewPrompt(input),
    context: "fresh",
    cwd: repository,
    model: "anthropic/claude-opus-5",
    thinking: "medium",
    timeoutMs: 30 * 60_000,
    toolBudget: { soft: 60, hard: 100 },
    artifacts: true,
    result: { kind: "structured", schema: INITIAL_REVIEW_SCHEMA },
  }, context.signal);
  const value = requireStructuredResult(response, "initial reviewer");
  return parseInitialReviewResult(value, input.maxParallelFixers);
}

export async function runFixWaves(
  context: WorkflowActionContext<ReviewWorkflowInput>,
  plan: FrozenReviewPlan,
): Promise<FixWaveResult> {
  const input = parseReviewWorkflowInput(context.input);
  const repository = await verifyReviewRepository(input, context.signal, false);
  const accepted: AcceptedFix[] = [];
  const unresolved: UnresolvedFix[] = [];
  const groups = new Map(plan.fixerGroups.map((group) => [group.groupId, group]));

  for (const wave of plan.waves) {
    const results = await Promise.all(wave.groupIds.map(async (groupId) => {
      const group = groups.get(groupId);
      if (group === undefined) throw new Error(`review wave references unknown group ${groupId}`);
      const groupIds = new Set(group.findings.map((finding) => finding.id));
      const knownSiblingFindings = plan.findings
        .filter((finding) => finding.blocking && !groupIds.has(finding.id))
        .map((finding) => ({
          id: finding.id,
          category: finding.category,
          writablePaths: finding.writablePaths,
        }));
      return await runFixerGroup(
        context,
        input,
        repository,
        group,
        knownSiblingFindings,
      );
    }));
    for (const result of results) {
      if ("commitSha" in result) accepted.push(result);
      else unresolved.push(result);
    }
  }

  for (const fix of accepted) {
    if (fix.deferredFindings.length === 0) continue;
    unresolved.push({
      groupId: fix.groupId,
      reason: "novel out-of-scope findings require final reconciliation",
      evidence: fix.deferredFindings.map((finding) => `${finding.id}: ${finding.contract}`),
      worktree: fix.worktree,
    });
  }

  let integratedHead = await git(repository, ["rev-parse", "HEAD"], context.signal);
  for (const fix of accepted) {
    if (await isAncestor(repository, fix.commitSha, integratedHead, context.signal)) continue;
    try {
      await git(repository, ["merge", "--no-edit", fix.commitSha], context.signal);
      integratedHead = await git(repository, ["rev-parse", "HEAD"], context.signal);
    } catch (error) {
      await git(repository, ["merge", "--abort"], context.signal).catch(() => undefined);
      unresolved.push({
        groupId: fix.groupId,
        reason: "serial integration conflict",
        evidence: [boundedError(error), `commit=${fix.commitSha}`, `expectedHead=${integratedHead}`],
        worktree: fix.worktree,
      });
      break;
    }
  }
  integratedHead = await git(repository, ["rev-parse", "HEAD"], context.signal);
  const retentionWarnings: string[] = [];
  const preserveByGroup = new Map(
    unresolved.map((item) => [item.groupId, { worktree: item.worktree }]),
  );
  for (const fix of accepted) {
    if (!(await isAncestor(repository, fix.commitSha, integratedHead, context.signal))) {
      preserveByGroup.set(fix.groupId, { worktree: fix.worktree });
    }
  }
  for (const fix of accepted) {
    if (preserveByGroup.has(fix.groupId)) continue;
    try {
      await recordFixerWorktreeOutcome(input, context.state.runId, {
        status: "integrated",
        groupId: fix.groupId,
        worktree: fix.worktree,
        commitSha: fix.commitSha,
        integratedHead,
      }, context.signal);
    } catch (error) {
      retentionWarnings.push(`could not schedule ${fix.groupId} cleanup: ${boundedError(error)}`);
    }
  }
  for (const [groupId, item] of preserveByGroup) {
    try {
      await recordFixerWorktreeOutcome(input, context.state.runId, {
        status: "unresolved",
        groupId,
        worktree: item.worktree,
      }, context.signal);
    } catch (error) {
      retentionWarnings.push(`could not preserve ${groupId} retention state: ${boundedError(error)}`);
    }
  }
  return {
    status: unresolved.length === 0 ? "completed" : "needs_owner",
    baseRevision: input.reviewRevision,
    integratedHead,
    accepted,
    unresolved,
    retentionWarnings,
  };
}

async function runFixerGroup(
  context: WorkflowActionContext<ReviewWorkflowInput>,
  input: ReviewWorkflowInput,
  repository: string,
  group: FixerGroup,
  knownSiblingFindings: Array<{
    id: string;
    category: InitialReviewFinding["category"];
    writablePaths: string[];
  }>,
): Promise<AcceptedFix | UnresolvedFix> {
  const worktree = await ensureOwnedFixerWorktree(input, context.state.runId, group.groupId, context.signal);
  let rejectionEvidence: string[] = [];
  const deferredFindings = new Map<string, InitialReviewFinding>();
  for (let round = 1; round <= 2; round += 1) {
    let head = await git(worktree, ["rev-parse", "HEAD"], context.signal);
    if (head === input.reviewRevision) {
      const response = await delegateSubagent({
        ownerRunId: context.state.runId,
        nodeId: `${group.groupId}:fix:${round}`,
        agent: "worker",
        task: fixerPrompt(input, group, round, rejectionEvidence),
        context: "fresh",
        cwd: worktree,
        model: "openai-codex/gpt-5.6-terra",
        thinking: "medium",
        timeoutMs: 60 * 60_000,
        toolBudget: { soft: 100, hard: 160 },
        artifacts: true,
        result: { kind: "text" },
      }, context.signal);
      requireCompleted(response, `fixer ${group.groupId}`);
      head = await git(worktree, ["rev-parse", "HEAD"], context.signal);
    }

    const changedPaths = await inspectExactFixCommit(
      worktree,
      input.reviewRevision,
      head,
      group.writablePaths,
      context.signal,
    );
    const packet = buildReReviewPacket(planForGroup(group), {
      groupId: group.groupId,
      findingIds: group.findings.map((finding) => finding.id),
      baseSha: input.reviewRevision,
      commitSha: head,
      changedPaths,
      validationEvidence: ["exactly one commit", "clean worktree", "changed paths within declared scope"],
    });
    const response = await delegateSubagent({
      ownerRunId: context.state.runId,
      nodeId: `${group.groupId}:re-review:${round}`,
      agent: "reviewer",
      task: reReviewPrompt(input, packet, knownSiblingFindings),
      context: "fresh",
      cwd: worktree,
      model: "anthropic/claude-sonnet-5",
      thinking: "medium",
      timeoutMs: 30 * 60_000,
      toolBudget: { soft: 50, hard: 90 },
      artifacts: true,
      result: { kind: "structured", schema: RE_REVIEW_SCHEMA },
    }, context.signal);
    const verdict = parseReReviewVerdict(requireStructuredResult(response, `re-review ${group.groupId}`));
    const introducedBlocking = verdict.introducedFindings.filter(
      (finding) => finding.introducedByFix && finding.blocking,
    );
    for (const finding of verdict.introducedFindings) {
      if (finding.introducedByFix || deferredFindings.has(finding.id)) continue;
      const { introducedByFix: _, ...deferredFinding } = finding;
      deferredFindings.set(deferredFinding.id, deferredFinding);
    }
    if (verdict.verdict === "accept" && introducedBlocking.length === 0) {
      return {
        groupId: group.groupId,
        commitSha: head,
        changedPaths,
        rounds: round,
        worktree,
        deferredFindings: [...deferredFindings.values()],
      };
    }
    rejectionEvidence = [
      verdict.reason,
      ...introducedBlocking.map((finding) => `${finding.id}: ${finding.contract}`),
    ];
    if (round < 2) {
      await git(worktree, ["reset", "--hard", input.reviewRevision], context.signal);
      await git(worktree, ["clean", "-fd"], context.signal);
    }
  }
  return {
    groupId: group.groupId,
    reason: "fix rejected after two mandatory re-review rounds",
    evidence: rejectionEvidence,
    worktree,
  };
}

async function verifyReviewRepository(
  input: ReviewWorkflowInput,
  signal: AbortSignal,
  requireExactRevision: boolean,
): Promise<string> {
  const repository = await realpath(input.repository);
  const topLevel = await git(repository, ["rev-parse", "--show-toplevel"], signal);
  if (await realpath(topLevel) !== repository) {
    throw new Error("review workflow repository must name the worktree root");
  }
  const head = await git(repository, ["rev-parse", "HEAD"], signal);
  if (head !== input.reviewRevision) {
    if (requireExactRevision || !(await isAncestor(repository, input.reviewRevision, head, signal))) {
      throw new Error(`review revision drift: expected ${input.reviewRevision}, observed ${head}`);
    }
  }
  if ((await git(repository, ["status", "--porcelain"], signal)).length !== 0) {
    throw new Error("review worktree must be clean before review or integration");
  }
  return repository;
}

async function inspectExactFixCommit(
  worktree: string,
  base: string,
  head: string,
  writablePaths: string[],
  signal: AbortSignal,
): Promise<string[]> {
  if (!SHA.test(head) || head === base) throw new Error("fixer did not return one exact commit");
  if ((await git(worktree, ["status", "--porcelain"], signal)).length !== 0) {
    throw new Error("fixer worktree is dirty after its claimed commit");
  }
  const count = await git(worktree, ["rev-list", "--count", `${base}..${head}`], signal);
  if (count !== "1") throw new Error(`fixer must return exactly one commit, observed ${count}`);
  const changedPaths = (await git(worktree, ["diff", "--name-only", "--no-renames", base, head], signal))
    .split("\n")
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right));
  const allowed = new Set(writablePaths);
  const escaped = changedPaths.find((path) => !allowed.has(path));
  if (escaped !== undefined) throw new Error(`fixer changed path outside declared scope: ${escaped}`);
  return changedPaths;
}

async function gitCommonDir(cwd: string, signal: AbortSignal): Promise<string> {
  const common = await git(cwd, ["rev-parse", "--git-common-dir"], signal);
  return await realpath(isAbsolute(common) ? common : resolve(cwd, common));
}

async function isAncestor(
  repository: string,
  commit: string,
  head: string,
  signal: AbortSignal,
): Promise<boolean> {
  try {
    await git(repository, ["merge-base", "--is-ancestor", commit, head], signal);
    return true;
  } catch {
    return false;
  }
}

async function git(cwd: string, args: string[], signal: AbortSignal): Promise<string> {
  const result = await execFileAsync("git", ["-C", cwd, ...args], {
    encoding: "utf8",
    maxBuffer: MAX_GIT_OUTPUT,
    signal,
  });
  return result.stdout.trim();
}

function initialReviewPrompt(input: ReviewWorkflowInput): string {
  return [
    `Review commit ${input.reviewRevision} for this objective:`,
    input.objective,
    "Inspect only the committed changes and relevant repository context. Do not modify files.",
    "Report correctness, security, data_loss, scope, and acceptance failures as blocking.",
    "Use category style only for minor style findings. Orkastrator derives blocking status from category.",
    "For each finding, implicatedPaths names evidence locations and writablePaths names only files a fixer may change. Both use repository-relative paths such as src/count.js.",
    "Never widen writablePaths to tests or aggregate consequence paths unless those files themselves must change. Do not report an umbrella failing-suite finding when scoped root-cause findings already explain the failure.",
    "Never return an absolute path or a path containing . or .. segments.",
  ].join("\n\n");
}

function fixerPrompt(
  input: ReviewWorkflowInput,
  group: FixerGroup,
  round: number,
  rejectionEvidence: string[],
): string {
  return [
    `Fix review group ${group.groupId} for objective: ${input.objective}`,
    `This is fix round ${round} of 2.`,
    `Writable paths: ${JSON.stringify(group.writablePaths)}`,
    `Finding contracts: ${JSON.stringify(group.findings.map((finding) => ({
      id: finding.id,
      contract: finding.contract,
      evidence: finding.evidence,
    })))}`,
    rejectionEvidence.length === 0 ? "" : `Previous rejection evidence: ${JSON.stringify(rejectionEvidence)}`,
    "Change only the declared writable paths. Validate the fix, leave the worktree clean, and create exactly one commit.",
    "Do not ask the supervisor to widen scope. If scoped validation passes while the full suite fails only in another declared fixer group's paths, commit the scoped fix and record that out-of-scope failure.",
  ].filter(Boolean).join("\n\n");
}

function reReviewPrompt(
  input: ReviewWorkflowInput,
  packet: ReturnType<typeof buildReReviewPacket>,
  knownSiblingFindings: Array<{
    id: string;
    category: InitialReviewFinding["category"];
    writablePaths: string[];
  }>,
): string {
  return [
    `Re-review one fix for objective: ${input.objective}`,
    "Use only this bounded contract and the committed diff in the current worktree.",
    JSON.stringify(packet),
    `Known sibling findings handled by other fixer groups: ${JSON.stringify(knownSiblingFindings)}`,
    "Do not modify files. Reject any unmet contract or blocking regression introduced by this fix.",
    "Do not return a known sibling finding as new. If validation fails only because of a known sibling, mention it in the reason and keep introducedFindings empty.",
    "Return a non-fix-introduced finding only when it is genuinely novel. Novel deferred findings block final workflow completion pending reconciliation.",
  ].join("\n\n");
}

export function parseReReviewVerdict(value: unknown): ReReviewVerdict {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("re-review result must be an object");
  }
  const result = value as Record<string, unknown>;
  const keys = Object.keys(result).sort();
  if (keys.join("\0") !== ["introducedFindings", "reason", "verdict"].join("\0")) {
    throw new Error("re-review result has unknown or missing fields");
  }
  if (result.verdict !== "accept" && result.verdict !== "reject") {
    throw new Error("re-review verdict must be accept or reject");
  }
  if (typeof result.reason !== "string" || result.reason.trim().length === 0) {
    throw new Error("re-review reason is required");
  }
  const introducedFindings = result.introducedFindings;
  if (!Array.isArray(introducedFindings)) {
    throw new Error("re-review introducedFindings must be an array");
  }
  const introducedByFix = new Map<string, boolean>();
  const parsed = parseInitialReviewOutput(JSON.stringify({
    findings: introducedFindings.map((finding) => {
      if (finding === null || typeof finding !== "object" || Array.isArray(finding)) {
        throw new Error("introduced finding must be an object");
      }
      const record = finding as Record<string, unknown>;
      if (typeof record.introducedByFix !== "boolean") {
        throw new Error("introduced finding requires introducedByFix boolean");
      }
      if (typeof record.id === "string") introducedByFix.set(record.id, record.introducedByFix);
      const { introducedByFix: _, ...rest } = record;
      return { ...rest, blocking: rest.category !== "style" };
    }),
  }), 1).findings;
  return {
    verdict: result.verdict,
    reason: result.reason,
    introducedFindings: parsed.map((finding) => {
      const isIntroducedByFix = introducedByFix.get(finding.id);
      if (isIntroducedByFix === undefined) {
        throw new Error("introduced finding requires introducedByFix boolean");
      }
      return {
        ...finding,
        blocking: isIntroducedByFix && finding.category !== "style",
        introducedByFix: isIntroducedByFix,
      };
    }),
  };
}

export function parseInitialReviewResult(
  value: unknown,
  maxParallelFixers: number,
): FrozenReviewPlan {
  return parseInitialReviewOutput(
    JSON.stringify(withDerivedInitialBlocking(value)),
    maxParallelFixers,
  );
}

function withDerivedInitialBlocking(value: unknown): unknown {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("initial reviewer result must be an object");
  }
  const envelope = value as Record<string, unknown>;
  if (!Array.isArray(envelope.findings)) {
    throw new Error("initial reviewer findings must be an array");
  }
  return {
    ...envelope,
    findings: envelope.findings.map((finding) => {
      if (finding === null || typeof finding !== "object" || Array.isArray(finding)) {
        throw new Error("initial reviewer finding must be an object");
      }
      const record = finding as Record<string, unknown>;
      return { ...record, blocking: record.category !== "style" };
    }),
  };
}

function requireStructuredResult(
  response: Awaited<ReturnType<typeof delegateSubagent>>,
  label: string,
): unknown {
  requireCompleted(response, label);
  if (response.result?.kind !== "structured") {
    throw new Error(`${label} did not return structured output`);
  }
  return response.result.value;
}

function requireCompleted(
  response: Awaited<ReturnType<typeof delegateSubagent>>,
  label: string,
): void {
  if (response.status !== "completed") {
    throw new Error(`${label} ended ${response.status}: ${response.error ?? "no error detail"}`);
  }
}

function planForGroup(group: FixerGroup): FrozenReviewPlan {
  return {
    findings: group.findings,
    fixerGroups: [group],
    waves: [{ wave: 1, groupIds: [group.groupId] }],
  };
}

function boundedError(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).slice(0, 4_000);
}

function findingSchema() {
  return {
    type: "object",
    additionalProperties: false,
    required: ["id", "severity", "category", "contract", "evidence", "implicatedPaths", "writablePaths"],
    properties: {
      id: { type: "string", pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" },
      severity: { enum: ["critical", "high", "medium", "low", "info"] },
      category: { enum: ["correctness", "security", "data_loss", "scope", "acceptance", "style"] },
      contract: { type: "string", minLength: 1, maxLength: 4_000 },
      evidence: { type: "array", maxItems: 50, items: { type: "string", minLength: 1, maxLength: 4_000 } },
      implicatedPaths: pathArraySchema(),
      writablePaths: pathArraySchema(),
    },
  } as const;
}

function pathArraySchema() {
  return {
    type: "array",
    maxItems: 100,
    items: {
      type: "string",
      minLength: 1,
      maxLength: 512,
      pattern: "^(?!/)(?![A-Za-z]:)(?!.*\\\\)(?!.*//)(?!\\.\\.?$)(?!\\.\\.?/)(?!.*\\/\\.\\.?(?:\\/|$)).+$",
    },
  } as const;
}

function reviewEnvelopeSchema() {
  return {
    type: "object",
    additionalProperties: false,
    required: ["findings"],
    properties: {
      findings: { type: "array", maxItems: 1_000, items: findingSchema() },
    },
  } as const;
}
