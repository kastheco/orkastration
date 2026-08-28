import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

interface CommandResult {
  status: number;
  stdout: string;
  stderr: string;
}

function command(executable: string, args: string[], cwd?: string): CommandResult {
  const result = spawnSync(executable, args, { cwd, encoding: "utf8" });
  if (result.error !== undefined) throw result.error;
  return {
    status: result.status ?? 1,
    stdout: result.stdout,
    stderr: result.stderr,
  };
}

function git(repository: string, ...args: string[]): CommandResult {
  return command("git", args, repository);
}

function requireSuccess(result: CommandResult): void {
  assert.equal(result.status, 0, result.stderr);
}

function initializeRepository(parent: string, name: string): string {
  const repository = join(parent, name);
  mkdirSync(repository);
  requireSuccess(git(repository, "init", "-b", "main"));
  requireSuccess(git(repository, "config", "user.name", "Contract Test"));
  requireSuccess(git(repository, "config", "user.email", "contract@example.invalid"));
  return repository;
}

function commitAll(repository: string, message: string): void {
  requireSuccess(git(repository, "add", "."));
  requireSuccess(git(repository, "commit", "-m", message));
}

function createWorktree(repository: string, branch: string): string {
  const result = command("wt", [
    "-C",
    repository,
    "switch",
    "--create",
    branch,
    "--base",
    "main",
    "--no-hooks",
    "--no-cd",
    "--format=json",
  ]);
  requireSuccess(result);
  const created = JSON.parse(result.stdout) as Record<string, unknown>;
  assert.equal(created.action, "created");
  assert.equal(created.branch, branch);
  assert.equal(created.created_branch, true);
  return String(created.path);
}

function listWorktrees(repository: string): Array<Record<string, unknown>> {
  const result = command("wt", [
    "-C",
    repository,
    "--config-set",
    "list.json-schema=2",
    "list",
    "--format=json",
  ]);
  requireSuccess(result);
  const list = JSON.parse(result.stdout) as Record<string, unknown>;
  assert.equal(list.schema, 2);
  return list.items as Array<Record<string, unknown>>;
}

function removeWorktree(repository: string, branch: string, ...options: string[]): CommandResult {
  return command("wt", [
    "-C",
    repository,
    "remove",
    branch,
    ...options,
    "--foreground",
    "--no-hooks",
    "--format=json",
  ]);
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitForExit(pid: number): Promise<boolean> {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (!isAlive(pid)) return true;
    await new Promise<void>((resolve) => setTimeout(resolve, 50));
  }
  return !isAlive(pid);
}

const live = process.env.ORKASTRATOR_LIVE_WORKTRUNK === "1";

test(
  "Worktrunk 0.75.0 exposes the pinned success and failure contracts",
  { skip: live ? false : "set ORKASTRATOR_LIVE_WORKTRUNK=1 to run destructive temp fixtures" },
  async () => {
    assert.match(command("wt", ["--version"]).stdout, /^wt v0\.75\.0\s*$/u);
    const temporary = mkdtempSync(join(tmpdir(), "orkastrator-worktrunk-"));
    let reapPid: number | undefined;
    try {
      const basic = initializeRepository(temporary, "basic");
      writeFileSync(join(basic, "README.md"), "base\n");
      commitAll(basic, "base");
      const createdPath = createWorktree(basic, "contract-create");

      const feature = listWorktrees(basic).find((item) => item.branch === "contract-create");
      assert.ok(feature);
      assert.equal(typeof (feature.head as Record<string, unknown>).sha, "string");
      const featureWorktree = feature.worktree as Record<string, unknown>;
      assert.equal(featureWorktree.path, createdPath);
      assert.equal(featureWorktree.main, false);
      assert.equal(featureWorktree.current, false);
      assert.equal(featureWorktree.detached, false);
      assert.equal(featureWorktree.branch_mismatch, false);
      assert.equal((featureWorktree.changes as Record<string, unknown>).conflicted, false);
      const featureDefault = feature.default_branch as Record<string, unknown>;
      assert.equal(featureDefault.ahead, 0);
      assert.equal(featureDefault.behind, 0);
      assert.equal(featureDefault.merge_conflicts, false);
      const removed = removeWorktree(basic, "contract-create");
      requireSuccess(removed);
      assert.equal(
        (JSON.parse(removed.stdout) as Array<Record<string, unknown>>)[0]?.branch_outcome,
        "deleted",
      );

      const hookRepository = initializeRepository(temporary, "hook");
      mkdirSync(join(hookRepository, ".config"));
      writeFileSync(
        join(hookRepository, ".config/wt.toml"),
        'pre-merge = "sh -c \'echo hook-out; echo hook-err >&2; exit 23\'"\n',
      );
      writeFileSync(join(hookRepository, "file.txt"), "base\n");
      commitAll(hookRepository, "base");
      const hookFeature = createWorktree(hookRepository, "hook-fail");
      writeFileSync(join(hookFeature, "feature.txt"), "feature\n");
      commitAll(hookFeature, "feature");
      const hookFailure = command("wt", [
        "-C",
        hookFeature,
        "--yes",
        "merge",
        "--no-remove",
        "--format=json",
      ]);
      assert.equal(hookFailure.status, 23);
      assert.equal(hookFailure.stdout, "");
      assert.match(hookFailure.stderr, /hook-out/u);
      assert.match(hookFailure.stderr, /hook-err/u);
      const hookIdentity = listWorktrees(hookFeature).find((item) => item.branch === "hook-fail");
      assert.ok(hookIdentity);
      assert.equal(typeof (hookIdentity.head as Record<string, unknown>).sha, "string");
      const hookWorktree = hookIdentity.worktree as Record<string, unknown>;
      assert.equal(hookWorktree.detached, false);
      assert.equal(hookWorktree.branch_mismatch, false);
      assert.equal(Object.hasOwn(hookWorktree, "operation"), false);
      assert.equal((hookWorktree.changes as Record<string, unknown>).conflicted, false);
      requireSuccess(removeWorktree(hookRepository, "hook-fail", "--force-delete"));

      const conflictRepository = initializeRepository(temporary, "conflict");
      writeFileSync(join(conflictRepository, "conflict.txt"), "base\n");
      commitAll(conflictRepository, "base");
      const conflictFeature = createWorktree(conflictRepository, "conflict-feature");
      writeFileSync(join(conflictFeature, "conflict.txt"), "feature\n");
      commitAll(conflictFeature, "feature");
      writeFileSync(join(conflictRepository, "conflict.txt"), "main\n");
      commitAll(conflictRepository, "main");
      const conflict = command("wt", [
        "-C",
        conflictFeature,
        "merge",
        "--no-remove",
        "--no-hooks",
        "--format=json",
      ]);
      assert.equal(conflict.status, 1);
      assert.equal(conflict.stdout, "");
      assert.match(conflict.stderr, /Rebase onto main incomplete/u);
      const conflictList = command("wt", [
        "-C",
        conflictFeature,
        "--config-set",
        "list.json-schema=2",
        "list",
        "--format=json",
      ]);
      requireSuccess(conflictList);
      const conflictItems = (JSON.parse(conflictList.stdout) as Record<string, unknown>)
        .items as Array<Record<string, unknown>>;
      const conflicted = conflictItems.find((item) => item.branch === "conflict-feature");
      assert.ok(conflicted);
      const conflictWorktree = conflicted.worktree as Record<string, unknown>;
      assert.equal(conflictWorktree.operation, "rebase");
      assert.equal((conflictWorktree.changes as Record<string, unknown>).conflicted, true);
      requireSuccess(git(conflictFeature, "rebase", "--abort"));
      requireSuccess(removeWorktree(conflictRepository, "conflict-feature", "--force-delete"));

      const reapRepository = initializeRepository(temporary, "reap");
      writeFileSync(join(reapRepository, "file.txt"), "base\n");
      commitAll(reapRepository, "base");
      const reapFeature = createWorktree(reapRepository, "reap-feature");
      const sleeper = spawn("sleep", ["300"], {
        cwd: reapFeature,
        detached: true,
        stdio: "ignore",
      });
      assert.ok(sleeper.pid);
      reapPid = sleeper.pid;
      sleeper.unref();
      assert.equal(isAlive(reapPid), true);
      const reap = removeWorktree(
        reapRepository,
        "reap-feature",
        "--reap",
        "--force-delete",
      );
      requireSuccess(reap);
      assert.equal(await waitForExit(reapPid), true);
      assert.equal(
        (JSON.parse(reap.stdout) as Array<Record<string, unknown>>)[0]?.branch_outcome,
        "deleted",
      );
    } finally {
      if (reapPid !== undefined && isAlive(reapPid)) process.kill(-reapPid, "SIGKILL");
      rmSync(temporary, { recursive: true, force: true });
    }
  },
);
