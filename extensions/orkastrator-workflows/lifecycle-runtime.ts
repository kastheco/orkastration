import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { promisify } from "node:util";

import type { AutoimplementCompleted } from "@osolmaz/pi-workflows/builtins";

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

export type ImplementationWorktree = {
  repository: string;
  branch: string;
  baseRevision: string;
};

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
  let stdout: string;
  try {
    stdout = await runner(
      source,
      ["switch", "--create", branch, "--base", "@", "--no-hooks", "--no-cd", "--format=json"],
      signal,
    );
  } catch (error) {
    const exists = await git(source, ["show-ref", "--verify", "--quiet", `refs/heads/${branch}`], signal)
      .then(() => true, () => false);
    if (!exists) throw error;
    stdout = await runner(
      source,
      ["switch", branch, "--no-hooks", "--no-cd", "--format=json"],
      signal,
    );
  }

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
