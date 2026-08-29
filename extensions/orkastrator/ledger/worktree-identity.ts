import { isAbsolute, relative, resolve } from "node:path";

import type {
  CurrentWorktreeIdentity,
  FilesystemIdentity,
  LegacyWorktreeIdentity,
  WorktreeIdentity,
} from "./types.ts";

const SHA_PATTERN = /^[0-9a-f]{40}$/u;
const CURRENT_KEYS = [
  "version",
  "repositoryRoot",
  "repositoryFs",
  "remoteUrl",
  "branch",
  "path",
  "worktreeFs",
  "baseSha",
  "headSha",
  "operation",
  "clean",
] as const;
const LEGACY_KEYS = ["repositoryRoot", "path", "branch", "headSha"] as const;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length &&
    actual.every((key, index) => key === sortedExpected[index]);
}

export function canonicalRemoteUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    if (!new Set(["https:", "http:", "ssh:"]).has(parsed.protocol)) return null;
    if (parsed.username !== "" || parsed.password !== "" || parsed.search !== "" || parsed.hash !== "") {
      return null;
    }
    parsed.hostname = parsed.hostname.toLowerCase();
    let pathname = parsed.pathname.replace(/\/+$/u, "").replace(/\.git$/u, "");
    if (pathname === "") pathname = "/";
    parsed.pathname = pathname;
    return parsed.toString().replace(/\/$/u, "");
  } catch {
    return null;
  }
}

export function pathsOverlap(left: string, right: string): boolean {
  if (!isAbsolute(left) || !isAbsolute(right)) return true;
  const leftToRight = relative(left, right);
  const rightToLeft = relative(right, left);
  const isWithin = (value: string): boolean => value === "" || (!value.startsWith("..") && !isAbsolute(value));
  return isWithin(leftToRight) || isWithin(rightToLeft);
}

export function isFilesystemIdentity(value: unknown): value is FilesystemIdentity {
  return isObject(value) &&
    hasExactKeys(value, ["device", "inode"]) &&
    typeof value.device === "number" &&
    Number.isSafeInteger(value.device) &&
    value.device >= 0 &&
    typeof value.inode === "number" &&
    Number.isSafeInteger(value.inode) &&
    value.inode > 0;
}

export function isLegacyWorktreeIdentity(value: unknown): value is LegacyWorktreeIdentity {
  return isObject(value) &&
    hasExactKeys(value, LEGACY_KEYS) &&
    typeof value.repositoryRoot === "string" &&
    isAbsolute(value.repositoryRoot) &&
    typeof value.path === "string" &&
    isAbsolute(value.path) &&
    typeof value.branch === "string" &&
    value.branch.length > 0 &&
    typeof value.headSha === "string" &&
    SHA_PATTERN.test(value.headSha);
}

export function isCurrentWorktreeIdentity(
  value: unknown,
  expectedRunId?: string,
  expectedRepositoryRoot?: string,
): value is CurrentWorktreeIdentity {
  if (!isObject(value) || !hasExactKeys(value, CURRENT_KEYS) || value.version !== 2) return false;
  if (
    typeof value.repositoryRoot !== "string" ||
    !isAbsolute(value.repositoryRoot) ||
    resolve(value.repositoryRoot) !== value.repositoryRoot ||
    (expectedRepositoryRoot !== undefined && value.repositoryRoot !== expectedRepositoryRoot) ||
    !isFilesystemIdentity(value.repositoryFs) ||
    (value.remoteUrl !== null &&
      (typeof value.remoteUrl !== "string" || canonicalRemoteUrl(value.remoteUrl) !== value.remoteUrl)) ||
    typeof value.branch !== "string" ||
    (expectedRunId !== undefined && value.branch !== `orkastrator/${expectedRunId.toLowerCase()}/worker`) ||
    typeof value.path !== "string" ||
    !isAbsolute(value.path) ||
    resolve(value.path) !== value.path ||
    pathsOverlap(value.repositoryRoot, value.path) ||
    !isFilesystemIdentity(value.worktreeFs) ||
    typeof value.baseSha !== "string" ||
    !SHA_PATTERN.test(value.baseSha) ||
    value.headSha !== value.baseSha ||
    value.operation !== null ||
    value.clean !== true
  ) {
    return false;
  }
  return true;
}

export function worktreeIdentityKind(value: unknown): "current" | "legacy" | null {
  if (isCurrentWorktreeIdentity(value)) return "current";
  if (isLegacyWorktreeIdentity(value)) return "legacy";
  return null;
}

export function isPersistedWorktreeIdentity(
  value: unknown,
  runId: string,
  repositoryRoot: string,
): value is WorktreeIdentity {
  return isCurrentWorktreeIdentity(value, runId, repositoryRoot) || isLegacyWorktreeIdentity(value);
}
