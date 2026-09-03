import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";
import { promisify } from "node:util";

import type {
  AutodocInput,
  AutoimplementCompleted,
} from "@osolmaz/pi-workflows/builtins";

import {
  parseOptionalHerdrLaunchBinding,
  type HerdrLaunchBinding,
} from "./herdr-launch.ts";

const execFileAsync = promisify(execFile);
const SHA = /^[0-9a-f]{40}$/u;

export type OrkastratorLifecycleInput = {
  task: string;
  repository: string;
  maxParallelFixers: number;
  worktreeRetentionDays: number;
  herdrLaunch?: HerdrLaunchBinding;
};

export type RepositoryResolution = {
  status: "resolved" | "blocked";
  repository?: string;
  reason: string;
  evidence: string[];
};

export type ImplementationWorktree = {
  repository: string;
  branch: string;
  baseRevision: string;
};

export type RepositoryBaseline = {
  repository: string;
  branch: string;
  headRevision: string;
  changedPaths: string[];
};

export type PreparedTaskWorkspace = NonNullable<AutodocInput["preparedWorkspace"]>;

export type ReviewTarget = {
  repository: string;
  reviewRevision: string;
};

export type WorktrunkRunner = (
  repository: string,
  args: string[],
  signal: AbortSignal,
) => Promise<string>;

export type OwnerResolvedStatus = "owner_accepted_partial" | "stopped";

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function optionalInteger(
  value: unknown,
  fallback: number,
  minimum: number,
  maximum: number,
  label: string,
): number {
  const resolved = value === undefined ? fallback : value;
  if (!Number.isInteger(resolved) || (resolved as number) < minimum || (resolved as number) > maximum) {
    throw new Error(`${label} must be an integer from ${minimum} to ${maximum}`);
  }
  return resolved as number;
}

export function parseLifecycleInput(value: unknown): OrkastratorLifecycleInput {
  const input = requireRecord(value, "Orkastrator lifecycle input");
  const allowed = new Set([
    "task",
    "repository",
    "maxParallelFixers",
    "worktreeRetentionDays",
    "herdrLaunch",
  ]);
  const unexpected = Object.keys(input).find((key) => !allowed.has(key));
  if (unexpected !== undefined) {
    throw new Error(`Orkastrator lifecycle input has unknown field ${unexpected}`);
  }
  if (typeof input.task !== "string" || input.task.trim().length === 0) {
    throw new Error("Orkastrator lifecycle task must be a non-empty string");
  }
  if (typeof input.repository !== "string" || !isAbsolute(input.repository)) {
    throw new Error("Orkastrator lifecycle repository must be an absolute path");
  }
  const herdrLaunch = parseOptionalHerdrLaunchBinding(
    input.herdrLaunch,
    "Orkastrator lifecycle Herdr launch binding",
  );
  return {
    task: input.task.trim(),
    repository: input.repository,
    maxParallelFixers: optionalInteger(
      input.maxParallelFixers,
      3,
      1,
      3,
      "Orkastrator lifecycle maxParallelFixers",
    ),
    worktreeRetentionDays: optionalInteger(
      input.worktreeRetentionDays,
      30,
      1,
      365,
      "Orkastrator lifecycle worktreeRetentionDays",
    ),
    ...(herdrLaunch === undefined ? {} : { herdrLaunch }),
  };
}

export function parseRepositoryResolution(
  value: unknown,
  coordinationRepository: string,
): RepositoryResolution {
  const result = requireRecord(value, "repository resolution");
  const unexpected = Object.keys(result).find(
    (key) => !["status", "repository", "reason", "evidence"].includes(key),
  );
  if (unexpected !== undefined) {
    throw new Error(`repository resolution has unknown field ${unexpected}`);
  }
  if (result.status !== "resolved" && result.status !== "blocked") {
    throw new Error("repository resolution status must be resolved or blocked");
  }
  if (typeof result.reason !== "string" || result.reason.trim().length === 0) {
    throw new Error("repository resolution reason must be non-empty text");
  }
  if (
    !Array.isArray(result.evidence)
    || result.evidence.length === 0
    || result.evidence.length > 8
    || result.evidence.some((item) => typeof item !== "string" || item.trim().length === 0 || item.length > 1_000)
  ) {
    throw new Error("repository resolution evidence must contain 1 to 8 bounded text items");
  }
  if (result.status === "blocked") {
    if (result.repository !== undefined) {
      throw new Error("blocked repository resolution must not select a repository");
    }
    return {
      status: "blocked",
      reason: result.reason.trim(),
      evidence: result.evidence.map((item) => item.trim()),
    };
  }
  if (typeof result.repository !== "string" || !isAbsolute(result.repository)) {
    throw new Error("resolved repository must be an absolute path");
  }
  const root = resolve(coordinationRepository);
  const repository = resolve(result.repository);
  const child = relative(root, repository);
  if (child.startsWith("..") || isAbsolute(child)) {
    throw new Error("resolved repository must be the launch repository or one of its descendants");
  }
  return {
    status: "resolved",
    repository,
    reason: result.reason.trim(),
    evidence: result.evidence.map((item) => item.trim()),
  };
}

export async function verifyImplementationRepository(
  repository: string,
  coordinationRepository: string,
  signal: AbortSignal,
): Promise<{ repository: string }> {
  const coordinationRoot = await realpath(coordinationRepository);
  const candidate = await realpath(repository);
  const child = relative(coordinationRoot, candidate);
  if (child.startsWith("..") || isAbsolute(child)) {
    throw new Error("resolved repository escapes the launch repository");
  }
  const topLevel = await realpath(await git(candidate, ["rev-parse", "--show-toplevel"], signal));
  if (topLevel !== candidate) {
    throw new Error("resolved repository must name a Git worktree root");
  }
  return { repository: candidate };
}

export function parseOwnerResolvedStatus(value: unknown): OwnerResolvedStatus {
  const result = requireRecord(value, "owner-resolved review result");
  if (result.status !== "owner_accepted_partial" && result.status !== "stopped") {
    throw new Error("owner-resolved review status must be owner_accepted_partial or stopped");
  }
  return result.status;
}

function repositoryCandidates(result: AutoimplementCompleted): string[] {
  const candidates: string[] = [];
  const implementation = requireRecord(result.implementation, "autoimplementation result");
  if (Array.isArray(implementation.repositories)) {
    for (const repository of implementation.repositories) {
      if (typeof repository === "string") candidates.push(repository);
    }
  }
  const delivery = requireRecord(result.delivery, "autoimplementation delivery");
  if (Array.isArray(delivery.repositories)) {
    for (const item of delivery.repositories) {
      if (item !== null && typeof item === "object" && !Array.isArray(item)) {
        const repository = (item as Record<string, unknown>).repository;
        if (typeof repository === "string") candidates.push(repository);
      }
    }
  }
  return [...new Set(candidates)];
}

async function git(repository: string, args: string[], signal: AbortSignal): Promise<string> {
  const result = await execFileAsync("git", ["-C", repository, ...args], {
    encoding: "utf8",
    signal,
  });
  return result.stdout.trim();
}

const runWorktrunk: WorktrunkRunner = async (repository, args, signal) => {
  const result = await execFileAsync("wt", ["-C", repository, ...args], {
    encoding: "utf8",
    signal,
  });
  return result.stdout.trim();
};

function parseWorktrunkPath(stdout: string, branch: string): string {
  let value: unknown;
  try {
    value = JSON.parse(stdout);
  } catch {
    throw new Error("Worktrunk create output must be JSON");
  }
  const output = requireRecord(value, "Worktrunk create output");
  if (output.branch !== branch) {
    throw new Error("Worktrunk create output returned an unexpected branch");
  }
  if (typeof output.path !== "string" || !isAbsolute(output.path)) {
    throw new Error("Worktrunk create output must include an absolute path");
  }
  return output.path;
}

async function repositoryChangedPaths(
  repository: string,
  signal: AbortSignal,
): Promise<string[]> {
  const result = await execFileAsync(
    "git",
    ["-C", repository, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    { encoding: "utf8", maxBuffer: 2_000_000, signal },
  );
  const entries = result.stdout.split("\0").filter(Boolean);
  const paths: string[] = [];
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index]!;
    const status = entry.slice(0, 2);
    paths.push(entry.slice(3));
    if ((status.includes("R") || status.includes("C")) && entries[index + 1] !== undefined) {
      paths.push(entries[index + 1]!);
      index += 1;
    }
  }
  return [...new Set(paths)].sort();
}

export async function captureRepositoryBaseline(
  repository: string,
  signal: AbortSignal,
): Promise<RepositoryBaseline> {
  const source = await realpath(repository);
  const topLevel = await realpath(await git(source, ["rev-parse", "--show-toplevel"], signal));
  if (topLevel !== source) {
    throw new Error("Repository baseline must name a Git worktree root");
  }
  const branch = await git(source, ["symbolic-ref", "--quiet", "--short", "HEAD"], signal);
  const headRevision = await git(source, ["rev-parse", "HEAD"], signal);
  if (!SHA.test(headRevision)) {
    throw new Error("Repository baseline HEAD is not a full Git revision");
  }
  return {
    repository: source,
    branch,
    headRevision,
    changedPaths: await repositoryChangedPaths(source, signal),
  };
}

async function createOrAdoptWorktrunk(
  source: string,
  branch: string,
  signal: AbortSignal,
  runner: WorktrunkRunner,
): Promise<string> {
  try {
    return await runner(
      source,
      ["switch", "--create", branch, "--base", "@", "--no-hooks", "--no-cd", "--format=json"],
      signal,
    );
  } catch (error) {
    const exists = await git(source, ["show-ref", "--verify", "--quiet", `refs/heads/${branch}`], signal)
      .then(() => true, () => false);
    if (!exists) throw error;
    return await runner(
      source,
      ["switch", branch, "--no-hooks", "--no-cd", "--format=json"],
      signal,
    );
  }
}

export async function createPreparedTaskWorktree(
  repository: string,
  runId: string,
  signal: AbortSignal,
  runner: WorktrunkRunner = runWorktrunk,
): Promise<PreparedTaskWorkspace> {
  const baseline = await captureRepositoryBaseline(repository, signal);
  const runDigest = createHash("sha256").update(runId).digest("hex").slice(0, 16);
  const workBranch = `orkastrator/${runDigest}/task`;
  const stdout = await createOrAdoptWorktrunk(
    baseline.repository,
    workBranch,
    signal,
    runner,
  );
  const worktree = await realpath(parseWorktrunkPath(stdout, workBranch));
  const worktreeRoot = await realpath(await git(worktree, ["rev-parse", "--show-toplevel"], signal));
  if (worktreeRoot !== worktree) {
    throw new Error("Prepared task worktree must name the worktree root");
  }
  const sourceCommonDir = resolve(
    baseline.repository,
    await git(baseline.repository, ["rev-parse", "--git-common-dir"], signal),
  );
  const worktreeCommonDir = resolve(
    worktree,
    await git(worktree, ["rev-parse", "--git-common-dir"], signal),
  );
  if (sourceCommonDir !== worktreeCommonDir) {
    throw new Error("Prepared task worktree must belong to the resolved repository");
  }
  if ((await git(worktree, ["branch", "--show-current"], signal)) !== workBranch) {
    throw new Error("Prepared task worktree is on an unexpected branch");
  }
  if ((await git(worktree, ["rev-parse", "HEAD"], signal)) !== baseline.headRevision) {
    throw new Error("Prepared task worktree does not start at the recorded launch revision");
  }
  if ((await repositoryChangedPaths(worktree, signal)).length !== 0) {
    throw new Error("Prepared task worktree must start clean");
  }
  const launchAfter = await captureRepositoryBaseline(baseline.repository, signal);
  if (
    launchAfter.branch !== baseline.branch
    || launchAfter.headRevision !== baseline.headRevision
    || JSON.stringify(launchAfter.changedPaths) !== JSON.stringify(baseline.changedPaths)
  ) {
    throw new Error("Preparing the task worktree changed the launch repository");
  }
  return {
    schema: "pi-workflows.prepared-workspace.v1",
    mode: "worktree",
    repository: baseline.repository,
    worktreePath: worktree,
    baseBranch: baseline.branch,
    baseRevision: baseline.headRevision,
    workBranch,
    directDefaultBranchAuthorized: false,
    preExistingChangedPaths: baseline.changedPaths,
    evidence: [
      `Recorded launch repository at ${baseline.headRevision}`,
      `Prepared dedicated task worktree ${worktree}`,
    ],
    scope: `Only ${baseline.repository}`,
  };
}

export async function createImplementationWorktree(
  repository: string,
  runId: string,
  signal: AbortSignal,
  runner: WorktrunkRunner = runWorktrunk,
): Promise<ImplementationWorktree> {
  const source = await realpath(repository);
  const topLevel = await realpath(await git(source, ["rev-parse", "--show-toplevel"], signal));
  if (topLevel !== source) {
    throw new Error("Worktrunk source repository must name the worktree root");
  }
  if ((await git(source, ["status", "--porcelain"], signal)).length !== 0) {
    throw new Error("Worktrunk source repository must be clean");
  }
  const baseRevision = await git(source, ["rev-parse", "HEAD"], signal);
  if (!SHA.test(baseRevision)) {
    throw new Error("Worktrunk source HEAD is not a full Git revision");
  }

  const runDigest = createHash("sha256").update(runId).digest("hex").slice(0, 16);
  const branch = `orkastrator/${runDigest}/worker`;
  const stdout = await createOrAdoptWorktrunk(source, branch, signal, runner);

  const worktree = await realpath(parseWorktrunkPath(stdout, branch));
  const worktreeRoot = await realpath(await git(worktree, ["rev-parse", "--show-toplevel"], signal));
  if (worktreeRoot !== worktree) {
    throw new Error("Worktrunk result must name the worktree root");
  }
  if ((await git(worktree, ["branch", "--show-current"], signal)) !== branch) {
    throw new Error("Worktrunk result is on an unexpected branch");
  }
  if ((await git(worktree, ["rev-parse", "HEAD"], signal)) !== baseRevision) {
    throw new Error("Worktrunk result does not start at the invoking HEAD");
  }
  if ((await git(worktree, ["status", "--porcelain"], signal)).length !== 0) {
    throw new Error("Worktrunk result must be clean");
  }
  return { repository: worktree, branch, baseRevision };
}

export async function resolveReviewTarget(
  result: AutoimplementCompleted,
  signal: AbortSignal,
): Promise<ReviewTarget> {
  const candidates = repositoryCandidates(result);
  if (candidates.length !== 1) {
    throw new Error(
      `Orkastrator review requires one reported implementation repository, observed ${candidates.length}`,
    );
  }

  const repository = await realpath(candidates[0]!);
  const topLevel = await realpath(await git(repository, ["rev-parse", "--show-toplevel"], signal));
  if (topLevel !== repository) {
    throw new Error("implemented repository must name the worktree root");
  }
  const dirty = await git(repository, ["status", "--porcelain"], signal);
  if (dirty.length !== 0) {
    throw new Error("implemented repository must be clean before Orkastrator review");
  }
  const reviewRevision = await git(repository, ["rev-parse", "HEAD"], signal);
  if (!SHA.test(reviewRevision)) {
    throw new Error("implemented repository HEAD is not a full Git revision");
  }
  return { repository, reviewRevision };
}
