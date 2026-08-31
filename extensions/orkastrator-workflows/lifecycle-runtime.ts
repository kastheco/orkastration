import { execFile } from "node:child_process";
import { realpath } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { promisify } from "node:util";

import type { AutoimplementCompleted } from "@osolmaz/pi-workflows/builtins";

const execFileAsync = promisify(execFile);
const SHA = /^[0-9a-f]{40}$/u;

export type OrkastratorLifecycleInput = {
  task: string;
  repository: string;
  maxParallelFixers: number;
  worktreeRetentionDays: number;
};

export type ReviewTarget = {
  repository: string;
  reviewRevision: string;
};

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
