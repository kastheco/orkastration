import { spawn } from "node:child_process";
import { realpath, stat } from "node:fs/promises";
import { isAbsolute } from "node:path";

import type {
  CurrentWorktreeIdentity,
  FilesystemIdentity,
  LegacyWorktreeIdentity,
  WorktreeIdentity,
} from "./ledger/types.ts";
import {
  canonicalRemoteUrl,
  isCurrentWorktreeIdentity,
  isLegacyWorktreeIdentity,
  pathsOverlap,
} from "./ledger/worktree-identity.ts";

const DEFAULT_EXECUTABLE = "/home/kas/.local/bin/wt";
const COMMAND_TIMEOUT_MS = 15_000;
const MAX_STDOUT_BYTES = 4 * 1024 * 1024;
const MAX_STDERR_BYTES = 32 * 1024;
const SHA_PATTERN = /^[0-9a-f]{40}$/u;
const RUN_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;
const CURRENT_BRANCH_PATTERN = /^orkastrator\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\/worker$/iu;
const CHANGE_KEYS = ["staged", "modified", "untracked", "renamed", "deleted"] as const;

type DirtyChange = (typeof CHANGE_KEYS)[number];
type CommandKind = "list" | "create";
type StreamName = "stdout" | "stderr";
type IdentityTarget = "repository" | "worktree";

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

type WorktreeFailureObservation =
  | { status: "invalid_input"; reason: string }
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
  | { status: "path_overlap"; repositoryRoot: string; worktreePath: string }
  | { status: "remote_mismatch"; expected: string | null; actual: string | null }
  | {
      status: "identity_changed";
      target: IdentityTarget;
      expected: FilesystemIdentity | null;
      actual: FilesystemIdentity | null;
    }
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

export type WorktreeObservation =
  | { status: "ready"; identity: CurrentWorktreeIdentity }
  | { status: "legacy_ready"; identity: LegacyWorktreeIdentity; authorization: "inspect_only" }
  | WorktreeFailureObservation;

export interface EnsureOwnedWorktreeInput {
  repositoryRoot: string;
  runId: string;
}

export type EnsureOwnedWorktreeResult =
  | { status: "ready"; identity: CurrentWorktreeIdentity; recovered: boolean }
  | WorktreeFailureObservation;

export interface WorktrunkModule {
  ensureOwnedWorktree(input: EnsureOwnedWorktreeInput): Promise<EnsureOwnedWorktreeResult>;
  inspectOwnedWorktree(identity: WorktreeIdentity): Promise<WorktreeObservation>;
}

interface ParsedList {
  defaultBranch: string;
  remoteUrl: string | null;
  repositoryPath: string;
  repositoryFs: FilesystemIdentity;
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

interface PathProbe {
  path: string;
  fs: FilesystemIdentity;
}

interface ModuleOptions {
  commandRunner: CommandRunner;
  executable: string;
  timeoutMs: number;
  maxStdoutBytes: number;
  maxStderrBytes: number;
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
    detached: true,
    shell: false,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  let stdoutBytes = 0;
  let stderrBytes = 0;
  let settled = false;
  let forced: { type: "timeout" } | { type: "output_limit"; stream: StreamName } | undefined;
  let timer: NodeJS.Timeout | undefined;
  let settlementTimer: NodeJS.Timeout | undefined;

  const captured = (chunks: Buffer[]): string => Buffer.concat(chunks).toString("utf8");
  const finish = (result: CommandExecution): void => {
    if (settled) return;
    settled = true;
    if (timer !== undefined) clearTimeout(timer);
    if (settlementTimer !== undefined) clearTimeout(settlementTimer);
    complete(result);
  };
  const forcedResult = (): CommandExecution => {
    const output = { stdout: captured(stdout), stderr: captured(stderr) };
    return forced?.type === "output_limit"
      ? { type: "output_limit", stream: forced.stream, ...output }
      : { type: "timeout", ...output };
  };
  const killProcessGroup = (): void => {
    const pid = child.pid;
    if (pid !== undefined && pid >= 2) {
      try {
        process.kill(-pid, "SIGKILL");
      } catch {
        // The group may have exited between observation and signal delivery.
      }
    }
    try {
      child.kill("SIGKILL");
    } catch {
      // Spawn failure or an already-absent direct child is safe to ignore.
    }
    settlementTimer = setTimeout(() => {
      child.stdout.destroy();
      child.stderr.destroy();
      finish(forcedResult());
    }, 500);
    settlementTimer.unref();
  };
  const force = (reason: NonNullable<typeof forced>): void => {
    if (forced !== undefined || settled) return;
    forced = reason;
    killProcessGroup();
  };
  const capture = (stream: StreamName, chunk: Buffer): void => {
    const maximum = stream === "stdout" ? request.maxStdoutBytes : request.maxStderrBytes;
    const chunks = stream === "stdout" ? stdout : stderr;
    const used = stream === "stdout" ? stdoutBytes : stderrBytes;
    const available = Math.max(0, maximum - used);
    if (available > 0) chunks.push(chunk.subarray(0, available));
    if (stream === "stdout") stdoutBytes += Math.min(chunk.byteLength, available);
    else stderrBytes += Math.min(chunk.byteLength, available);
    if (chunk.byteLength > available) force({ type: "output_limit", stream });
  };

  child.stdout.on("data", (chunk: Buffer) => capture("stdout", chunk));
  child.stderr.on("data", (chunk: Buffer) => capture("stderr", chunk));
  child.on("error", (error) => {
    if (forced !== undefined) finish(forcedResult());
    else finish({
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
  timer = setTimeout(() => force({ type: "timeout" }), request.timeoutMs);
  timer.unref();
});

async function probePath(path: string): Promise<PathProbe | null> {
  if (!isAbsolute(path)) return null;
  try {
    const canonical = await realpath(path);
    const facts = await stat(canonical, { bigint: true });
    if (
      facts.dev < 0n || facts.dev > BigInt(Number.MAX_SAFE_INTEGER) ||
      facts.ino <= 0n || facts.ino > BigInt(Number.MAX_SAFE_INTEGER)
    ) {
      return null;
    }
    return {
      path: canonical,
      fs: { device: Number(facts.dev), inode: Number(facts.ino) },
    };
  } catch {
    return null;
  }
}

function sameProbe(left: PathProbe | null, right: PathProbe | null): boolean {
  return left !== null && right !== null && left.path === right.path &&
    left.fs.device === right.fs.device && left.fs.inode === right.fs.inode;
}

function changed(
  target: IdentityTarget,
  expected: PathProbe | FilesystemIdentity | null,
  actual: PathProbe | FilesystemIdentity | null,
): WorktreeFailureObservation {
  const fs = (value: PathProbe | FilesystemIdentity | null): FilesystemIdentity | null =>
    value === null ? null : ("fs" in value ? value.fs : value);
  return { status: "identity_changed", target, expected: fs(expected), actual: fs(actual) };
}

function parseItem(value: unknown, index: number): ParsedItem | string {
  if (!isObject(value)) return `items[${index}] is not an object`;
  if (value.branch !== null && typeof value.branch !== "string") return `items[${index}].branch is invalid`;
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
    if (typeof worktree.changes[key] !== "boolean") return `items[${index}].worktree.changes.${key} is invalid`;
  }
  if (Object.hasOwn(worktree, "operation") &&
    (typeof worktree.operation !== "string" || worktree.operation.length === 0)) {
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

function parseList(stdout: string): Omit<ParsedList, "repositoryFs"> | WorktreeFailureObservation {
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
    if (remoteUrl === null) return { status: "malformed_output", reason: "repo.forge.url is not canonicalizable" };
  }
  const items: ParsedItem[] = [];
  for (const [index, item] of value.items.entries()) {
    const parsed = parseItem(item, index);
    if (typeof parsed === "string") return { status: "malformed_output", reason: parsed };
    items.push(parsed);
  }
  const mainItems = items.filter((item) => item.main);
  const defaultItems = items.filter((item) => item.branch === defaultBranch && item.main);
  if (defaultItems.length === 0) return { status: "missing", target: "default_branch" };
  if (mainItems.length !== 1 || defaultItems.length !== 1) {
    return { status: "malformed_output", reason: "main/default branch worktree is not unique" };
  }
  if (items.filter((item) => item.branch === defaultBranch).length !== 1) {
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

function commandObservation(
  command: CommandKind,
  execution: CommandExecution,
  limits: Pick<ModuleOptions, "maxStdoutBytes" | "maxStderrBytes">,
): WorktreeFailureObservation | undefined {
  const stdout = boundedText(execution.stdout, limits.maxStdoutBytes);
  const stderr = boundedText(execution.stderr, limits.maxStderrBytes);
  if (Buffer.byteLength(execution.stdout, "utf8") > limits.maxStdoutBytes) {
    return { status: "output_limit", command, stream: "stdout", stdout, stderr };
  }
  if (Buffer.byteLength(execution.stderr, "utf8") > limits.maxStderrBytes) {
    return { status: "output_limit", command, stream: "stderr", stdout, stderr };
  }
  if (execution.type === "timeout") return { status: "timeout", command, stdout, stderr };
  if (execution.type === "output_limit") {
    return { status: "output_limit", command, stream: execution.stream, stdout, stderr };
  }
  if (execution.exitCode !== 0) return {
    status: "command_failed",
    command,
    exitCode: execution.exitCode,
    stdout,
    stderr,
    ...(execution.error === undefined ? {} : { error: execution.error }),
  };
  return undefined;
}

async function observeItem(
  list: ParsedList,
  branch: string,
  repositoryRoot: string,
  expected?: CurrentWorktreeIdentity | LegacyWorktreeIdentity,
): Promise<WorktreeObservation> {
  if (isCurrentWorktreeIdentity(expected) && list.remoteUrl !== expected.remoteUrl) {
    return { status: "remote_mismatch", expected: expected.remoteUrl, actual: list.remoteUrl };
  }
  if (isCurrentWorktreeIdentity(expected) && list.baseSha !== expected.baseSha) {
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
  // Worktrunk's real conflicted-rebase shape has both fields; conflict is the stronger fact.
  if (item.changes.conflicted) return { status: "conflicted" };
  if (item.operation !== null) return { status: "active_operation", operation: item.operation };
  for (const change of CHANGE_KEYS) if (item.changes[change]) return { status: "dirty", change };

  const beforePath = await probePath(item.path);
  if (beforePath === null) {
    return { status: "path_mismatch", expected: expected?.path ?? "an existing worktree path", actual: null };
  }
  if (pathsOverlap(repositoryRoot, beforePath.path)) {
    return { status: "path_overlap", repositoryRoot, worktreePath: beforePath.path };
  }
  if (expected !== undefined && beforePath.path !== expected.path) {
    return { status: "path_mismatch", expected: expected.path, actual: beforePath.path };
  }
  if (isCurrentWorktreeIdentity(expected) &&
    (beforePath.fs.device !== expected.worktreeFs.device || beforePath.fs.inode !== expected.worktreeFs.inode)) {
    return changed("worktree", expected.worktreeFs, beforePath);
  }
  const headSha = expected?.headSha ?? list.baseSha;
  if (item.headSha !== headSha) return { status: "head_mismatch", expected: headSha, actual: item.headSha };

  const afterPath = await probePath(item.path);
  if (!sameProbe(beforePath, afterPath)) return changed("worktree", beforePath, afterPath);
  if (isLegacyWorktreeIdentity(expected)) {
    return { status: "legacy_ready", identity: expected, authorization: "inspect_only" };
  }
  const identity: CurrentWorktreeIdentity = expected ?? {
    version: 2,
    repositoryRoot,
    repositoryFs: list.repositoryFs,
    remoteUrl: list.remoteUrl,
    branch,
    path: beforePath.path,
    worktreeFs: beforePath.fs,
    baseSha: list.baseSha,
    headSha: item.headSha,
    operation: null,
    clean: true,
  };
  return { status: "ready", identity };
}

function strongerCreateMismatch(observation: WorktreeFailureObservation): boolean {
  return new Set([
    "duplicate", "detached", "branch_mismatch", "duplicate_branch", "not_feature_worktree",
    "dirty", "conflicted", "active_operation", "head_mismatch", "base_mismatch",
    "path_mismatch", "path_overlap", "remote_mismatch", "identity_changed",
  ]).has(observation.status);
}

function moduleWithOptions(options: ModuleOptions): WorktrunkModule {
  const execute = async (
    command: CommandKind,
    repositoryRoot: string,
    argv: readonly string[],
  ): Promise<CommandExecution> => {
    try {
      return await options.commandRunner({
        executable: options.executable,
        argv: ["-C", repositoryRoot, ...argv],
        timeoutMs: options.timeoutMs,
        maxStdoutBytes: options.maxStdoutBytes,
        maxStderrBytes: options.maxStderrBytes,
      });
    } catch (error) {
      return {
        type: "exit",
        exitCode: null,
        stdout: "",
        stderr: "",
        error: error instanceof Error ? error.message : String(error),
      };
    }
  };

  const list = async (
    repositoryRoot: string,
    watchedWorktree?: string,
    attemptEvenIfRootMissing = false,
  ): Promise<ParsedList | WorktreeFailureObservation> => {
    const beforeRoot = await probePath(repositoryRoot);
    if (beforeRoot === null && !attemptEvenIfRootMissing) return changed("repository", null, null);
    const beforeWorktree = watchedWorktree === undefined ? undefined : await probePath(watchedWorktree);
    if (watchedWorktree !== undefined && beforeWorktree === null) return changed("worktree", null, null);
    const execution = await execute("list", repositoryRoot, [
      "--config-set", "list.json-schema=2", "list", "--format=json",
    ]);
    const afterRoot = await probePath(repositoryRoot);
    const afterWorktree = watchedWorktree === undefined ? undefined : await probePath(watchedWorktree);
    if (!sameProbe(beforeRoot, afterRoot)) return changed("repository", beforeRoot, afterRoot);
    if (watchedWorktree !== undefined && !sameProbe(beforeWorktree ?? null, afterWorktree ?? null)) {
      return changed("worktree", beforeWorktree ?? null, afterWorktree ?? null);
    }
    const commandFailure = commandObservation("list", execution, options);
    if (commandFailure !== undefined) return commandFailure;
    if (execution.type !== "exit") return { status: "malformed_output", reason: "unreachable list result" };
    const parsed = parseList(execution.stdout);
    if (!("items" in parsed)) return parsed;
    const listedRoot = await probePath(parsed.repositoryPath);
    if (listedRoot === null || listedRoot.path !== repositoryRoot) {
      return { status: "path_mismatch", expected: repositoryRoot, actual: listedRoot?.path ?? null };
    }
    if (!sameProbe(afterRoot, listedRoot)) return changed("repository", afterRoot, listedRoot);
    return { ...parsed, repositoryFs: listedRoot.fs };
  };

  return {
    async ensureOwnedWorktree(input) {
      if (!isObject(input) || typeof input.runId !== "string" || !RUN_ID_PATTERN.test(input.runId) ||
        typeof input.repositoryRoot !== "string" || !isAbsolute(input.repositoryRoot)) {
        return { status: "invalid_input", reason: "repositoryRoot and runId are invalid" };
      }
      const initialRoot = await probePath(input.repositoryRoot);
      if (initialRoot === null) return { status: "invalid_input", reason: "repository root does not exist" };
      const repositoryRoot = initialRoot.path;
      const branch = `orkastrator/${input.runId.toLowerCase()}/worker`;
      const before = await list(repositoryRoot);
      if (!("items" in before)) return before;
      if (before.items.some((item) => item.branch === branch)) {
        const observation = await observeItem(before, branch, repositoryRoot);
        if (observation.status === "ready") {
          return { status: "ready", identity: observation.identity, recovered: true };
        }
        if (observation.status === "legacy_ready") {
          return { status: "invalid_input", reason: "unexpected legacy recovery observation" };
        }
        return observation;
      }

      const rootBeforeCreate = await probePath(repositoryRoot);
      if (rootBeforeCreate === null ||
        rootBeforeCreate.fs.device !== before.repositoryFs.device ||
        rootBeforeCreate.fs.inode !== before.repositoryFs.inode) {
        return changed("repository", before.repositoryFs, rootBeforeCreate);
      }
      const creation = await execute("create", repositoryRoot, [
        "switch", "--create", branch, "--base", before.defaultBranch,
        "--no-hooks", "--no-cd", "--format=json",
      ]);
      const createFailure = commandObservation("create", creation, options);
      const rootAfterCreate = await probePath(repositoryRoot);
      const createIdentityChange = sameProbe(rootBeforeCreate, rootAfterCreate)
        ? undefined
        : changed("repository", rootBeforeCreate, rootAfterCreate);

      // Every create return is uncertain until exactly one fresh schema-2 list re-observes identity.
      const after = await list(repositoryRoot, undefined, true);
      if (createIdentityChange !== undefined) return createIdentityChange;
      if (!("items" in after)) {
        if (createFailure !== undefined && strongerCreateMismatch(after)) return after;
        return createFailure ?? after;
      }
      if (after.repositoryFs.device !== before.repositoryFs.device ||
        after.repositoryFs.inode !== before.repositoryFs.inode) {
        return changed("repository", before.repositoryFs, after.repositoryFs);
      }
      let observation: WorktreeObservation;
      if (after.remoteUrl !== before.remoteUrl) {
        observation = { status: "remote_mismatch", expected: before.remoteUrl, actual: after.remoteUrl };
      } else if (after.baseSha !== before.baseSha) {
        observation = { status: "base_mismatch", expected: before.baseSha, actual: after.baseSha };
      } else {
        observation = await observeItem(after, branch, repositoryRoot);
      }
      if (observation.status === "ready") {
        return {
          status: "ready",
          identity: observation.identity,
          recovered: createFailure !== undefined,
        };
      }
      if (observation.status === "legacy_ready") {
        return { status: "invalid_input", reason: "unexpected legacy create observation" };
      }
      if (createFailure !== undefined && !strongerCreateMismatch(observation)) return createFailure;
      return observation;
    },

    async inspectOwnedWorktree(identity) {
      if (isObject(identity) && identity.version === 2 &&
        typeof identity.repositoryRoot === "string" && typeof identity.path === "string" &&
        isAbsolute(identity.repositoryRoot) && isAbsolute(identity.path) &&
        pathsOverlap(identity.repositoryRoot, identity.path)) {
        return {
          status: "path_overlap",
          repositoryRoot: identity.repositoryRoot,
          worktreePath: identity.path,
        };
      }
      if (!isCurrentWorktreeIdentity(identity) && !isLegacyWorktreeIdentity(identity)) {
        return { status: "invalid_input", reason: "persisted worktree identity is invalid" };
      }
      if (isCurrentWorktreeIdentity(identity) && !CURRENT_BRANCH_PATTERN.test(identity.branch)) {
        return { status: "invalid_input", reason: "current worktree branch is invalid" };
      }
      const root = await probePath(identity.repositoryRoot);
      if (root === null || root.path !== identity.repositoryRoot) {
        return { status: "path_mismatch", expected: identity.repositoryRoot, actual: root?.path ?? null };
      }
      if (isCurrentWorktreeIdentity(identity) &&
        (root.fs.device !== identity.repositoryFs.device || root.fs.inode !== identity.repositoryFs.inode)) {
        return changed("repository", identity.repositoryFs, root);
      }
      if (isCurrentWorktreeIdentity(identity)) {
        const worktree = await probePath(identity.path);
        if (worktree === null ||
          worktree.fs.device !== identity.worktreeFs.device ||
          worktree.fs.inode !== identity.worktreeFs.inode) {
          return changed("worktree", identity.worktreeFs, worktree);
        }
      }
      const watchedPath = isCurrentWorktreeIdentity(identity) ? identity.path : undefined;
      const listed = await list(root.path, watchedPath);
      if (!("items" in listed)) return listed;
      return observeItem(listed, identity.branch, root.path, identity);
    },
  };
}

const worktrunk = moduleWithOptions({
  commandRunner: runCommand,
  executable: DEFAULT_EXECUTABLE,
  timeoutMs: COMMAND_TIMEOUT_MS,
  maxStdoutBytes: MAX_STDOUT_BYTES,
  maxStderrBytes: MAX_STDERR_BYTES,
});

export const ensureOwnedWorktree = worktrunk.ensureOwnedWorktree;
export const inspectOwnedWorktree = worktrunk.inspectOwnedWorktree;

/** @internal Narrow test seam. Omitted runner exercises the real shell-free production runner. */
export function createWorktrunkForTesting(options: {
  commandRunner?: CommandRunner;
  executable?: string;
  timeoutMs?: number;
  maxStdoutBytes?: number;
  maxStderrBytes?: number;
}): WorktrunkModule {
  return moduleWithOptions({
    commandRunner: options.commandRunner ?? runCommand,
    executable: options.executable ?? DEFAULT_EXECUTABLE,
    timeoutMs: options.timeoutMs ?? COMMAND_TIMEOUT_MS,
    maxStdoutBytes: options.maxStdoutBytes ?? MAX_STDOUT_BYTES,
    maxStderrBytes: options.maxStderrBytes ?? MAX_STDERR_BYTES,
  });
}

// Deliberately no remove/force-remove/reap API: KAS-741 owns positively identified process-group reap.
// Any future destructive authorization must immediately re-inspect dev/inode and all schema-2 facts.
