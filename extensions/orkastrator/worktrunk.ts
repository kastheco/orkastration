import { spawn } from "node:child_process";
import { realpath } from "node:fs/promises";
import { isAbsolute, resolve } from "node:path";

import type { WorktreeIdentity } from "./ledger/types.ts";

const DEFAULT_EXECUTABLE = "/home/kas/.local/bin/wt";
const COMMAND_TIMEOUT_MS = 15_000;
const MAX_STDOUT_BYTES = 4 * 1024 * 1024;
const MAX_STDERR_BYTES = 32 * 1024;
const SHA_PATTERN = /^[0-9a-f]{40}$/u;
const RUN_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;
const CHANGE_KEYS = ["staged", "modified", "untracked", "renamed", "deleted"] as const;

type DirtyChange = (typeof CHANGE_KEYS)[number];
type CommandKind = "list" | "create";
type StreamName = "stdout" | "stderr";

interface CommandRequest {
  executable: string;
  argv: readonly string[];
  timeoutMs: number;
  maxStdoutBytes: number;
  maxStderrBytes: number;
}

type CommandExecution =
  | { type: "exit"; exitCode: number | null; stdout: string; stderr: string; error?: string }
  | { type: "timeout"; stdout: string; stderr: string }
  | { type: "output_limit"; stream: StreamName; stdout: string; stderr: string };

type CommandRunner = (request: Readonly<CommandRequest>) => Promise<CommandExecution>;

export type WorktreeObservation =
  | { status: "ready"; identity: WorktreeIdentity }
  | { status: "missing"; target: "worktree" | "default_branch" }
  | { status: "duplicate"; count: number }
  | { status: "detached" }
  | { status: "branch_mismatch" }
  | { status: "duplicate_branch" }
  | { status: "not_feature_worktree" }
  | { status: "dirty"; change: DirtyChange }
  | { status: "conflicted" }
  | { status: "active_operation"; operation: string }
  | { status: "head_mismatch"; expected: string; actual: string }
  | { status: "base_mismatch"; expected: string; actual: string }
  | { status: "path_mismatch"; expected: string; actual: string | null }
  | { status: "remote_mismatch"; expected: string | null; actual: string | null }
  | { status: "malformed_output"; reason: string }
  | {
      status: "command_failed";
      command: CommandKind;
      exitCode: number | null;
      stdout: string;
      stderr: string;
      error?: string;
    }
  | { status: "timeout"; command: CommandKind; stdout: string; stderr: string }
  | {
      status: "output_limit";
      command: CommandKind;
      stream: StreamName;
      stdout: string;
      stderr: string;
    };

export interface EnsureOwnedWorktreeInput {
  repositoryRoot: string;
  runId: string;
}

export interface EnsureOwnedWorktreeResult {
  identity: WorktreeIdentity;
  recovered: boolean;
}

export interface WorktrunkModule {
  ensureOwnedWorktree(input: EnsureOwnedWorktreeInput): Promise<EnsureOwnedWorktreeResult>;
  inspectOwnedWorktree(identity: WorktreeIdentity): Promise<WorktreeObservation>;
}

export interface WorktrunkFailure extends Error {
  readonly observation: Exclude<WorktreeObservation, { status: "ready" }>;
}

interface ParsedList {
  defaultBranch: string;
  remoteUrl: string | null;
  repositoryPath: string;
  baseSha: string;
  items: ParsedItem[];
}

interface ParsedItem {
  branch: string | null;
  headSha: string;
  path: string;
  main: boolean;
  detached: boolean;
  branchMismatch: boolean;
  duplicateBranch: boolean;
  operation: string | null;
  changes: Record<DirtyChange | "conflicted", boolean>;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function boundedText(value: string, maximumBytes: number): string {
  const bytes = Buffer.from(value, "utf8");
  if (bytes.byteLength <= maximumBytes) return value;
  let bounded = bytes.subarray(0, maximumBytes).toString("utf8");
  while (Buffer.byteLength(bounded, "utf8") > maximumBytes) bounded = bounded.slice(0, -1);
  return bounded;
}

const runCommand: CommandRunner = (request) => new Promise((complete) => {
  const child = spawn(request.executable, [...request.argv], {
    shell: false,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  let stdoutBytes = 0;
  let stderrBytes = 0;
  let settled = false;
  let forced: { type: "timeout" } | { type: "output_limit"; stream: StreamName } | undefined;

  const captured = (chunks: Buffer[]): string => Buffer.concat(chunks).toString("utf8");
  const finish = (result: CommandExecution): void => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    complete(result);
  };
  const capture = (stream: StreamName, chunk: Buffer): void => {
    const maximum = stream === "stdout" ? request.maxStdoutBytes : request.maxStderrBytes;
    const chunks = stream === "stdout" ? stdout : stderr;
    const used = stream === "stdout" ? stdoutBytes : stderrBytes;
    const available = Math.max(0, maximum - used);
    if (available > 0) chunks.push(chunk.subarray(0, available));
    if (stream === "stdout") stdoutBytes += Math.min(chunk.byteLength, available);
    else stderrBytes += Math.min(chunk.byteLength, available);
    if (chunk.byteLength > available && forced === undefined) {
      forced = { type: "output_limit", stream };
      child.kill("SIGKILL");
    }
  };

  child.stdout.on("data", (chunk: Buffer) => capture("stdout", chunk));
  child.stderr.on("data", (chunk: Buffer) => capture("stderr", chunk));
  child.on("error", (error) => {
    finish({
      type: "exit",
      exitCode: null,
      stdout: captured(stdout),
      stderr: captured(stderr),
      error: error.message,
    });
  });
  child.on("close", (code) => {
    const output = { stdout: captured(stdout), stderr: captured(stderr) };
    if (forced?.type === "timeout") finish({ type: "timeout", ...output });
    else if (forced?.type === "output_limit") {
      finish({ type: "output_limit", stream: forced.stream, ...output });
    } else finish({ type: "exit", exitCode: code, ...output });
  });
  const timer = setTimeout(() => {
    if (settled || forced !== undefined) return;
    forced = { type: "timeout" };
    child.kill("SIGKILL");
  }, request.timeoutMs);
  timer.unref();
});

function canonicalRemoteUrl(value: string): string | null {
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

function parseItem(value: unknown, index: number): ParsedItem | string {
  if (!isObject(value)) return `items[${index}] is not an object`;
  if (value.branch !== null && typeof value.branch !== "string") {
    return `items[${index}].branch is invalid`;
  }
  if (!isObject(value.head) || typeof value.head.sha !== "string" || !SHA_PATTERN.test(value.head.sha)) {
    return `items[${index}].head.sha is invalid`;
  }
  if (!isObject(value.worktree)) return `items[${index}].worktree is invalid`;
  const worktree = value.worktree;
  if (typeof worktree.path !== "string" || !isAbsolute(worktree.path)) {
    return `items[${index}].worktree.path is invalid`;
  }
  for (const key of ["main", "detached", "branch_mismatch", "duplicate_branch"] as const) {
    if (typeof worktree[key] !== "boolean") return `items[${index}].worktree.${key} is invalid`;
  }
  if (!isObject(worktree.changes)) return `items[${index}].worktree.changes is invalid`;
  for (const key of [...CHANGE_KEYS, "conflicted"] as const) {
    if (typeof worktree.changes[key] !== "boolean") {
      return `items[${index}].worktree.changes.${key} is invalid`;
    }
  }
  if (
    Object.hasOwn(worktree, "operation") &&
    (typeof worktree.operation !== "string" || worktree.operation.length === 0)
  ) {
    return `items[${index}].worktree.operation is invalid`;
  }
  return {
    branch: value.branch,
    headSha: value.head.sha,
    path: worktree.path,
    main: worktree.main as boolean,
    detached: worktree.detached as boolean,
    branchMismatch: worktree.branch_mismatch as boolean,
    duplicateBranch: worktree.duplicate_branch as boolean,
    operation: typeof worktree.operation === "string" ? worktree.operation : null,
    changes: {
      staged: worktree.changes.staged as boolean,
      modified: worktree.changes.modified as boolean,
      untracked: worktree.changes.untracked as boolean,
      renamed: worktree.changes.renamed as boolean,
      deleted: worktree.changes.deleted as boolean,
      conflicted: worktree.changes.conflicted as boolean,
    },
  };
}

function parseList(
  stdout: string,
): ParsedList | Exclude<WorktreeObservation, { status: "ready" }> {
  let value: unknown;
  try {
    value = JSON.parse(stdout);
  } catch {
    return { status: "malformed_output", reason: "list output is not JSON" };
  }
  if (!isObject(value) || value.schema !== 2 || !isObject(value.repo) || !Array.isArray(value.items)) {
    return { status: "malformed_output", reason: "list output is not schema 2" };
  }
  const defaultBranch = value.repo.default_branch;
  if (typeof defaultBranch !== "string" || defaultBranch.length === 0) {
    return { status: "malformed_output", reason: "repo.default_branch is invalid" };
  }
  let remoteUrl: string | null = null;
  if (value.repo.forge !== undefined && value.repo.forge !== null) {
    if (!isObject(value.repo.forge) || typeof value.repo.forge.url !== "string") {
      return { status: "malformed_output", reason: "repo.forge.url is invalid" };
    }
    remoteUrl = canonicalRemoteUrl(value.repo.forge.url);
    if (remoteUrl === null) {
      return { status: "malformed_output", reason: "repo.forge.url is not canonicalizable" };
    }
  }
  const items: ParsedItem[] = [];
  for (const [index, item] of value.items.entries()) {
    const parsed = parseItem(item, index);
    if (typeof parsed === "string") return { status: "malformed_output", reason: parsed };
    items.push(parsed);
  }
  const defaultItems = items.filter((item) => item.branch === defaultBranch && item.main);
  if (defaultItems.length === 0) return { status: "missing", target: "default_branch" };
  if (defaultItems.length !== 1) {
    return { status: "malformed_output", reason: "default branch worktree is not unique" };
  }
  const branchItems = items.filter((item) => item.branch === defaultBranch);
  if (branchItems.length !== 1) {
    return { status: "malformed_output", reason: "default branch item is not exact" };
  }
  return {
    defaultBranch,
    remoteUrl,
    repositoryPath: defaultItems[0]!.path,
    baseSha: defaultItems[0]!.headSha,
    items,
  };
}

function failure(observation: Exclude<WorktreeObservation, { status: "ready" }>): WorktrunkFailure {
  return Object.assign(new Error(`owned worktree is not ready: ${observation.status}`), { observation });
}

function commandObservation(
  command: CommandKind,
  execution: CommandExecution,
): Exclude<WorktreeObservation, { status: "ready" }> | undefined {
  const stdout = boundedText(execution.stdout, MAX_STDOUT_BYTES);
  const stderr = boundedText(execution.stderr, MAX_STDERR_BYTES);
  if (Buffer.byteLength(execution.stdout, "utf8") > MAX_STDOUT_BYTES) {
    return { status: "output_limit", command, stream: "stdout", stdout, stderr };
  }
  if (Buffer.byteLength(execution.stderr, "utf8") > MAX_STDERR_BYTES) {
    return { status: "output_limit", command, stream: "stderr", stdout, stderr };
  }
  if (execution.type === "timeout") return { status: "timeout", command, stdout, stderr };
  if (execution.type === "output_limit") {
    return { status: "output_limit", command, stream: execution.stream, stdout, stderr };
  }
  if (execution.exitCode !== 0) {
    return {
      status: "command_failed",
      command,
      exitCode: execution.exitCode,
      stdout,
      stderr,
      ...(execution.error === undefined ? {} : { error: execution.error }),
    };
  }
  return undefined;
}

async function canonicalPath(path: string): Promise<string | null> {
  if (!isAbsolute(path)) return null;
  try {
    return await realpath(path);
  } catch {
    return null;
  }
}

async function observeItem(
  list: ParsedList,
  branch: string,
  repositoryRoot: string,
  expected?: WorktreeIdentity,
): Promise<WorktreeObservation> {
  if (expected !== undefined && list.remoteUrl !== expected.remoteUrl) {
    return { status: "remote_mismatch", expected: expected.remoteUrl, actual: list.remoteUrl };
  }
  if (expected !== undefined && list.baseSha !== expected.baseSha) {
    return { status: "base_mismatch", expected: expected.baseSha, actual: list.baseSha };
  }
  const matching = list.items.filter((item) => item.branch === branch);
  if (matching.length === 0) return { status: "missing", target: "worktree" };
  if (matching.length !== 1) return { status: "duplicate", count: matching.length };
  const item = matching[0]!;
  if (item.detached) return { status: "detached" };
  if (item.branchMismatch) return { status: "branch_mismatch" };
  if (item.duplicateBranch) return { status: "duplicate_branch" };
  if (item.main) return { status: "not_feature_worktree" };
  if (item.operation !== null) return { status: "active_operation", operation: item.operation };
  if (item.changes.conflicted) return { status: "conflicted" };
  for (const change of CHANGE_KEYS) {
    if (item.changes[change]) return { status: "dirty", change };
  }
  const path = await canonicalPath(item.path);
  if (path === null || path === repositoryRoot) {
    return {
      status: "path_mismatch",
      expected: expected?.path ?? "an existing path distinct from the repository root",
      actual: path,
    };
  }
  if (expected !== undefined && path !== expected.path) {
    return { status: "path_mismatch", expected: expected.path, actual: path };
  }
  const headSha = expected?.headSha ?? list.baseSha;
  if (item.headSha !== headSha) {
    return { status: "head_mismatch", expected: headSha, actual: item.headSha };
  }
  const identity: WorktreeIdentity = expected ?? {
    repositoryRoot,
    remoteUrl: list.remoteUrl,
    branch,
    path,
    baseSha: list.baseSha,
    headSha: item.headSha,
    operation: null,
    clean: true,
  };
  return { status: "ready", identity };
}

function moduleWithRunner(commandRunner: CommandRunner): WorktrunkModule {
  const execute = async (
    command: CommandKind,
    repositoryRoot: string,
    argv: readonly string[],
  ): Promise<CommandExecution> => commandRunner({
    executable: DEFAULT_EXECUTABLE,
    argv: ["-C", repositoryRoot, ...argv],
    timeoutMs: COMMAND_TIMEOUT_MS,
    maxStdoutBytes: MAX_STDOUT_BYTES,
    maxStderrBytes: MAX_STDERR_BYTES,
  });

  const list = async (
    repositoryRoot: string,
  ): Promise<ParsedList | Exclude<WorktreeObservation, { status: "ready" }>> => {
    const execution = await execute("list", repositoryRoot, [
      "--config-set",
      "list.json-schema=2",
      "list",
      "--format=json",
    ]);
    const commandFailure = commandObservation("list", execution);
    if (commandFailure !== undefined) return commandFailure;
    if (execution.type !== "exit") return { status: "malformed_output", reason: "unreachable list result" };
    const parsed = parseList(execution.stdout);
    if (!("items" in parsed)) return parsed;
    const listedRepositoryRoot = await canonicalPath(parsed.repositoryPath);
    if (listedRepositoryRoot !== repositoryRoot) {
      return {
        status: "path_mismatch",
        expected: repositoryRoot,
        actual: listedRepositoryRoot,
      };
    }
    return parsed;
  };

  return {
    async ensureOwnedWorktree(input) {
      if (!RUN_ID_PATTERN.test(input.runId)) {
        throw failure({ status: "malformed_output", reason: "runId is invalid" });
      }
      const repositoryRoot = await canonicalPath(input.repositoryRoot);
      if (repositoryRoot === null) {
        throw failure({
          status: "path_mismatch",
          expected: "an existing absolute repository root",
          actual: null,
        });
      }
      const branch = `orkastrator/${input.runId.toLowerCase()}/worker`;
      const before = await list(repositoryRoot);
      if (!("items" in before)) throw failure(before);
      const existing = before.items.filter((item) => item.branch === branch);
      if (existing.length > 0) {
        const observation = await observeItem(before, branch, repositoryRoot);
        if (observation.status !== "ready") throw failure(observation);
        return { identity: observation.identity, recovered: true };
      }

      const creation = await execute("create", repositoryRoot, [
        "switch",
        "--create",
        branch,
        "--base",
        before.defaultBranch,
        "--no-hooks",
        "--no-cd",
        "--format=json",
      ]);
      const createFailure = commandObservation("create", creation);
      if (createFailure !== undefined) throw failure(createFailure);
      // Create JSON is intentionally not identity evidence. Only a fresh schema-2 list is trusted.
      const after = await list(repositoryRoot);
      if (!("items" in after)) throw failure(after);
      if (after.remoteUrl !== before.remoteUrl) {
        throw failure({
          status: "remote_mismatch",
          expected: before.remoteUrl,
          actual: after.remoteUrl,
        });
      }
      if (after.baseSha !== before.baseSha) {
        throw failure({ status: "base_mismatch", expected: before.baseSha, actual: after.baseSha });
      }
      const observation = await observeItem(after, branch, repositoryRoot);
      if (observation.status !== "ready") throw failure(observation);
      return { identity: observation.identity, recovered: false };
    },

    async inspectOwnedWorktree(identity) {
      if (
        !isObject(identity) ||
        typeof identity.repositoryRoot !== "string" ||
        (identity.remoteUrl !== null &&
          (typeof identity.remoteUrl !== "string" || canonicalRemoteUrl(identity.remoteUrl) !== identity.remoteUrl)) ||
        typeof identity.branch !== "string" ||
        !/^orkastrator\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\/worker$/iu.test(identity.branch) ||
        typeof identity.path !== "string" ||
        !isAbsolute(identity.path) ||
        typeof identity.baseSha !== "string" ||
        !SHA_PATTERN.test(identity.baseSha) ||
        typeof identity.headSha !== "string" ||
        !SHA_PATTERN.test(identity.headSha) ||
        identity.headSha !== identity.baseSha ||
        identity.operation !== null ||
        identity.clean !== true
      ) {
        return { status: "malformed_output", reason: "persisted worktree identity is invalid" };
      }
      const repositoryRoot = await canonicalPath(identity.repositoryRoot);
      if (repositoryRoot === null || repositoryRoot !== identity.repositoryRoot) {
        return {
          status: "path_mismatch",
          expected: identity.repositoryRoot,
          actual: repositoryRoot,
        };
      }
      const listed = await list(repositoryRoot);
      if (!("items" in listed)) return listed;
      return observeItem(listed, identity.branch, repositoryRoot, identity);
    },
  };
}

const worktrunk = moduleWithRunner(runCommand);

export const ensureOwnedWorktree = worktrunk.ensureOwnedWorktree;
export const inspectOwnedWorktree = worktrunk.inspectOwnedWorktree;

/** @internal Test seam; production callers use the two functions above. */
export function createWorktrunkForTesting(commandRunner: CommandRunner): WorktrunkModule {
  return moduleWithRunner(commandRunner);
}

// Deliberately no remove/force-remove/reap API: KAS-741 owns positively identified process-group reap.
