import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import {
  mkdir,
  readFile,
  readdir,
  readlink,
  realpath,
  rename,
  rm,
  rmdir,
  stat,
  writeFile,
} from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const SHA = /^[0-9a-f]{40}$/u;
const SAFE_PATH_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/u;
const MAX_GIT_OUTPUT = 1_048_576;

export interface WorktreeRetentionInput {
  repository: string;
  reviewRevision: string;
  worktreeRetentionDays: number;
  stateRoot?: string;
}

export type WorktreeRetentionOutcome =
  | {
      status: "integrated";
      groupId: string;
      worktree: string;
      commitSha: string;
      integratedHead: string;
    }
  | {
      status: "unresolved";
      groupId: string;
      worktree: string;
    };

interface WorktreeRetentionRecord {
  version: 1;
  repository: string;
  reviewRevision: string;
  runId: string;
  groupId: string;
  worktree: string;
  createdAt: string;
  status: "owned" | "integrated" | "unresolved";
  commitSha?: string;
  integratedHead?: string;
  deleteAfter?: string;
}

export interface RetentionSweepResult {
  removed: string[];
  preserved: string[];
  warnings: string[];
}

export async function ensureOwnedFixerWorktree(
  input: WorktreeRetentionInput,
  runId: string,
  groupId: string,
  signal: AbortSignal,
): Promise<string> {
  const paths = retentionPaths(input, runId, groupId);
  await mkdir(paths.runDirectory, { recursive: true });
  try {
    await stat(paths.worktree);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    await git(input.repository, ["worktree", "add", "--detach", paths.worktree, input.reviewRevision], signal);
  }
  const actual = await realpath(paths.worktree);
  if (await gitCommonDir(input.repository, signal) !== await gitCommonDir(actual, signal)) {
    throw new Error(`fixer worktree ${groupId} belongs to another repository`);
  }
  const head = await git(actual, ["rev-parse", "HEAD"], signal);
  const count = Number.parseInt(
    await git(actual, ["rev-list", "--count", `${input.reviewRevision}..${head}`], signal),
    10,
  );
  if (head !== input.reviewRevision && count !== 1) {
    throw new Error(`fixer worktree ${groupId} has unexpected commit history`);
  }
  await ensureRetentionRecord(input, runId, groupId, actual);
  if (!(await isWorktreeLocked(input.repository, actual, signal))) {
    await git(input.repository, ["worktree", "lock", "--reason", `orkastrator run ${runId}`, actual], signal);
  }
  return actual;
}

export async function recordFixerWorktreeOutcome(
  input: WorktreeRetentionInput,
  runId: string,
  outcome: WorktreeRetentionOutcome,
  signal: AbortSignal,
): Promise<void> {
  const paths = retentionPaths(input, runId, outcome.groupId);
  const record = await readRetentionRecord(paths.marker);
  if (
    record.repository !== await realpath(input.repository)
    || record.reviewRevision !== input.reviewRevision
    || record.runId !== runId
    || record.groupId !== outcome.groupId
    || resolve(record.worktree) !== resolve(outcome.worktree)
  ) {
    throw new Error(`retention record for ${outcome.groupId} changed during the run`);
  }
  if (outcome.status === "integrated") {
    if (!SHA.test(outcome.commitSha) || !SHA.test(outcome.integratedHead)) {
      throw new Error(`integrated retention state for ${outcome.groupId} requires exact commit identities`);
    }
    if (
      !Number.isSafeInteger(input.worktreeRetentionDays)
      || input.worktreeRetentionDays < 1
      || input.worktreeRetentionDays > 365
    ) {
      throw new Error("worktree retention days must be an integer from 1 to 365");
    }
    if (!(await isAncestor(input.repository, outcome.commitSha, outcome.integratedHead, signal))) {
      throw new Error(`fixer commit for ${outcome.groupId} is not integrated`);
    }
    await writeRetentionRecord(paths.marker, {
      ...record,
      status: "integrated",
      commitSha: outcome.commitSha,
      integratedHead: outcome.integratedHead,
      deleteAfter: new Date(Date.now() + input.worktreeRetentionDays * 86_400_000).toISOString(),
    });
    if (await isWorktreeLocked(input.repository, outcome.worktree, signal)) {
      await git(input.repository, ["worktree", "unlock", outcome.worktree], signal);
    }
    return;
  }
  const { commitSha: _, integratedHead: __, deleteAfter: ___, ...preserved } = record;
  await writeRetentionRecord(paths.marker, { ...preserved, status: "unresolved" });
  if (!(await isWorktreeLocked(input.repository, outcome.worktree, signal))) {
    await git(
      input.repository,
      ["worktree", "lock", "--reason", `unresolved orkastrator run ${runId}`, outcome.worktree],
      signal,
    );
  }
}

export async function sweepExpiredFixerWorktrees(
  input: WorktreeRetentionInput,
  signal: AbortSignal,
  now = new Date(),
): Promise<RetentionSweepResult> {
  const result: RetentionSweepResult = { removed: [], preserved: [], warnings: [] };
  const repository = await realpath(input.repository);
  const { repositoryRoot } = retentionNamespace(input);
  let runEntries;
  try {
    runEntries = await readdir(repositoryRoot, { withFileTypes: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return result;
    result.warnings.push(`could not inspect retention root: ${boundedError(error)}`);
    return result;
  }
  for (const runEntry of runEntries) {
    if (!runEntry.isDirectory() || !SAFE_PATH_SEGMENT.test(runEntry.name)) continue;
    const markerDirectory = join(repositoryRoot, runEntry.name, ".retention");
    let markerEntries;
    try {
      markerEntries = await readdir(markerDirectory, { withFileTypes: true });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        result.warnings.push(`could not inspect ${markerDirectory}: ${boundedError(error)}`);
      }
      continue;
    }
    for (const markerEntry of markerEntries) {
      if (!markerEntry.isFile() || !markerEntry.name.endsWith(".json")) continue;
      await sweepRetentionRecord(
        input,
        repository,
        runEntry.name,
        join(markerDirectory, markerEntry.name),
        now,
        signal,
        result,
      );
    }
  }
  return result;
}

async function sweepRetentionRecord(
  input: WorktreeRetentionInput,
  repository: string,
  runId: string,
  markerPath: string,
  now: Date,
  signal: AbortSignal,
  result: RetentionSweepResult,
): Promise<void> {
  try {
    const record = await readRetentionRecord(markerPath);
    const expected = retentionPaths(input, record.runId, record.groupId);
    if (
      record.repository !== repository
      || record.runId !== runId
      || markerPath !== expected.marker
      || resolve(record.worktree) !== resolve(expected.worktree)
    ) {
      throw new Error("retention record identity does not match its namespace");
    }
    if (
      record.status !== "integrated"
      || record.deleteAfter === undefined
      || Date.parse(record.deleteAfter) > now.getTime()
    ) {
      result.preserved.push(record.worktree);
      return;
    }
    const actual = await realpath(record.worktree);
    if (actual !== await realpath(expected.worktree)) throw new Error("retained worktree path changed");
    if (await gitCommonDir(actual, signal) !== await gitCommonDir(repository, signal)) {
      throw new Error("retained worktree belongs to another repository");
    }
    if (await isWorktreeLocked(repository, actual, signal)) {
      result.preserved.push(actual);
      return;
    }
    if (process.platform !== "linux") {
      result.preserved.push(actual);
      result.warnings.push(`process ownership check unavailable for ${actual}`);
      return;
    }
    if (await hasProcessWorkingIn(actual)) {
      result.preserved.push(actual);
      return;
    }
    const head = await git(actual, ["rev-parse", "HEAD"], signal);
    if (record.commitSha === undefined || head !== record.commitSha) {
      throw new Error("retained worktree HEAD changed after integration");
    }
    if ((await git(actual, ["status", "--porcelain", "--untracked-files=all"], signal)).length !== 0) {
      result.preserved.push(actual);
      return;
    }
    const repositoryHead = await git(repository, ["rev-parse", "HEAD"], signal);
    if (!(await isAncestor(repository, record.commitSha, repositoryHead, signal))) {
      result.preserved.push(actual);
      return;
    }
    await git(repository, ["worktree", "remove", actual], signal);
    await rm(markerPath);
    await rmdir(dirname(markerPath)).catch(() => undefined);
    await rmdir(expected.runDirectory).catch(() => undefined);
    result.removed.push(actual);
  } catch (error) {
    result.warnings.push(`${markerPath}: ${boundedError(error)}`);
  }
}

async function ensureRetentionRecord(
  input: WorktreeRetentionInput,
  runId: string,
  groupId: string,
  worktree: string,
): Promise<void> {
  const paths = retentionPaths(input, runId, groupId);
  const repository = await realpath(input.repository);
  try {
    const existing = await readRetentionRecord(paths.marker);
    if (
      existing.repository !== repository
      || existing.reviewRevision !== input.reviewRevision
      || existing.runId !== runId
      || existing.groupId !== groupId
      || resolve(existing.worktree) !== resolve(worktree)
    ) {
      throw new Error(`retention record for ${groupId} does not match the fixer worktree`);
    }
    return;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  await writeRetentionRecord(paths.marker, {
    version: 1,
    repository,
    reviewRevision: input.reviewRevision,
    runId,
    groupId,
    worktree,
    createdAt: new Date().toISOString(),
    status: "owned",
  });
}

function retentionNamespace(input: WorktreeRetentionInput): { repositoryRoot: string } {
  const repositoryDigest = createHash("sha256").update(input.repository).digest("hex").slice(0, 12);
  const root = input.stateRoot ?? join(homedir(), ".pi", "agent", "orkastrator-worktrees");
  return { repositoryRoot: join(root, `${basename(input.repository)}-${repositoryDigest}`) };
}

function retentionPaths(
  input: WorktreeRetentionInput,
  runId: string,
  groupId: string,
): { runDirectory: string; worktree: string; marker: string } {
  requireSafePathSegment(runId, "run id");
  requireSafePathSegment(groupId, "fixer group id");
  const { repositoryRoot } = retentionNamespace(input);
  const runDirectory = join(repositoryRoot, runId);
  return {
    runDirectory,
    worktree: join(runDirectory, groupId),
    marker: join(runDirectory, ".retention", `${groupId}.json`),
  };
}

function requireSafePathSegment(value: string, label: string): void {
  if (!SAFE_PATH_SEGMENT.test(value)) throw new Error(`${label} is not a safe path segment`);
}

async function writeRetentionRecord(path: string, record: WorktreeRetentionRecord): Promise<void> {
  await mkdir(resolve(path, ".."), { recursive: true });
  const temporary = `${path}.tmp-${process.pid}-${Date.now()}`;
  await writeFile(temporary, `${JSON.stringify(record, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, path);
}

async function readRetentionRecord(path: string): Promise<WorktreeRetentionRecord> {
  const value = JSON.parse(await readFile(path, "utf8")) as unknown;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("retention record must be an object");
  }
  const record = value as Record<string, unknown>;
  const allowed = new Set([
    "commitSha",
    "createdAt",
    "deleteAfter",
    "groupId",
    "integratedHead",
    "repository",
    "reviewRevision",
    "runId",
    "status",
    "version",
    "worktree",
  ]);
  const unknown = Object.keys(record).find((key) => !allowed.has(key));
  if (unknown !== undefined) throw new Error(`retention record has unknown field ${unknown}`);
  if (
    record.version !== 1
    || typeof record.repository !== "string"
    || typeof record.reviewRevision !== "string" || !SHA.test(record.reviewRevision)
    || typeof record.runId !== "string" || !SAFE_PATH_SEGMENT.test(record.runId)
    || typeof record.groupId !== "string" || !SAFE_PATH_SEGMENT.test(record.groupId)
    || typeof record.worktree !== "string" || !isAbsolute(record.worktree)
    || typeof record.createdAt !== "string" || !Number.isFinite(Date.parse(record.createdAt))
    || (record.status !== "owned" && record.status !== "integrated" && record.status !== "unresolved")
  ) {
    throw new Error("retention record has invalid required fields");
  }
  if (record.commitSha !== undefined && (typeof record.commitSha !== "string" || !SHA.test(record.commitSha))) {
    throw new Error("retention record commitSha is invalid");
  }
  if (record.integratedHead !== undefined && (typeof record.integratedHead !== "string" || !SHA.test(record.integratedHead))) {
    throw new Error("retention record integratedHead is invalid");
  }
  if (record.deleteAfter !== undefined && (typeof record.deleteAfter !== "string" || !Number.isFinite(Date.parse(record.deleteAfter)))) {
    throw new Error("retention record deleteAfter is invalid");
  }
  if (
    record.status === "integrated"
    && (record.commitSha === undefined || record.integratedHead === undefined || record.deleteAfter === undefined)
  ) {
    throw new Error("integrated retention record is incomplete");
  }
  return record as unknown as WorktreeRetentionRecord;
}

async function isWorktreeLocked(
  repository: string,
  worktree: string,
  signal: AbortSignal,
): Promise<boolean> {
  const actual = await realpath(worktree);
  const blocks = (await git(repository, ["worktree", "list", "--porcelain"], signal)).split("\n\n");
  const block = blocks.find((entry) => entry.split("\n")[0] === `worktree ${actual}`);
  if (block === undefined) throw new Error(`worktree is not registered: ${actual}`);
  return block.split("\n").some((line) => line === "locked" || line.startsWith("locked "));
}

async function hasProcessWorkingIn(worktree: string): Promise<boolean> {
  const actual = await realpath(worktree);
  const entries = await readdir("/proc", { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory() || !/^\d+$/u.test(entry.name)) continue;
    try {
      const cwd = await readlink(join("/proc", entry.name, "cwd"));
      if (cwd === actual || cwd.startsWith(`${actual}/`)) return true;
    } catch {
      // Processes can exit or deny inspection between readdir and readlink.
    }
  }
  return false;
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

function boundedError(error: unknown): string {
  return (error instanceof Error ? error.message : String(error)).slice(0, 4_000);
}
