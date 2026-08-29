import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import type { WorktreeIdentity } from "../ledger/types.ts";
import {
  createWorktrunkForTesting,
  type WorktreeObservation,
} from "../worktrunk.ts";

const RUN_ID = "00000000-0000-4000-8000-000000000743";
const BRANCH = `orkastrator/${RUN_ID}/worker`;
const BASE_SHA = "a".repeat(40);
const OTHER_SHA = "b".repeat(40);

type Exit = {
  type: "exit";
  exitCode: number | null;
  stdout: string;
  stderr: string;
  error?: string;
};
type Result = Exit | {
  type: "timeout";
  stdout: string;
  stderr: string;
} | {
  type: "output_limit";
  stream: "stdout" | "stderr";
  stdout: string;
  stderr: string;
};

interface Call {
  executable: string;
  argv: readonly string[];
  timeoutMs: number;
  maxStdoutBytes: number;
  maxStderrBytes: number;
}

function exited(stdout: string, exitCode = 0, stderr = ""): Result {
  return { type: "exit", exitCode, stdout, stderr };
}

function fixture(): {
  temporary: string;
  repository: string;
  worktree: string;
  cleanup: () => void;
} {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-worktrunk-unit-"));
  const repository = join(temporary, "repository");
  const worktree = join(temporary, "owned-worktree");
  mkdirSync(repository);
  mkdirSync(worktree);
  return {
    temporary,
    repository,
    worktree,
    cleanup: () => rmSync(temporary, { recursive: true, force: true }),
  };
}

function item(
  branch: string | null,
  path: string,
  options: {
    sha?: string;
    main?: boolean;
    detached?: boolean;
    branchMismatch?: boolean;
    duplicateBranch?: boolean;
    operation?: string;
    change?: string;
  } = {},
): Record<string, unknown> {
  const changes: Record<string, boolean> = {
    staged: false,
    modified: false,
    untracked: false,
    renamed: false,
    deleted: false,
    conflicted: false,
  };
  if (options.change !== undefined) changes[options.change] = true;
  return {
    branch,
    head: { sha: options.sha ?? BASE_SHA },
    worktree: {
      path,
      main: options.main ?? false,
      detached: options.detached ?? false,
      branch_mismatch: options.branchMismatch ?? false,
      duplicate_branch: options.duplicateBranch ?? false,
      changes,
      ...(options.operation === undefined ? {} : { operation: options.operation }),
    },
  };
}

function schema(
  repository: string,
  worktree: string,
  options: {
    includeFeature?: boolean;
    remote?: string | null;
    feature?: Parameters<typeof item>[2];
    extraItems?: Array<Record<string, unknown>>;
    baseSha?: string;
  } = {},
): string {
  const remote = options.remote === undefined ? "https://Example.COM/team/repository.git/" : options.remote;
  return JSON.stringify({
    schema: 2,
    repo: {
      default_branch: "main",
      ...(remote === null ? {} : { forge: { url: remote } }),
    },
    items: [
      item("main", repository, {
        main: true,
        ...(options.baseSha === undefined ? {} : { sha: options.baseSha }),
      }),
      ...(options.includeFeature === false ? [] : [item(BRANCH, worktree, options.feature)]),
      ...(options.extraItems ?? []),
    ],
  });
}

function harness(results: Result[]) {
  const calls: Call[] = [];
  const module = createWorktrunkForTesting(async (request) => {
    calls.push({ ...request });
    const result = results.shift();
    assert.ok(result, "unexpected Worktrunk command");
    return result;
  });
  return { module, calls, remaining: results };
}

function identity(repository: string, worktree: string, overrides: Partial<WorktreeIdentity> = {}): WorktreeIdentity {
  return {
    repositoryRoot: repository,
    remoteUrl: "https://example.com/team/repository",
    branch: BRANCH,
    path: worktree,
    baseSha: BASE_SHA,
    headSha: BASE_SHA,
    operation: null,
    clean: true,
    ...overrides,
  };
}

async function rejectedObservation(
  operation: Promise<unknown>,
): Promise<Exclude<WorktreeObservation, { status: "ready" }>> {
  try {
    await operation;
    assert.fail("expected owned worktree operation to fail closed");
  } catch (error) {
    assert.ok(error instanceof Error && "observation" in error);
    return (error as Error & { observation: Exclude<WorktreeObservation, { status: "ready" }> })
      .observation;
  }
}

test("ensure lists, creates with exact argv, ignores create JSON, and lists freshly for identity", async () => {
  const value = fixture();
  try {
    const input = { repositoryRoot: value.repository, runId: RUN_ID };
    const snapshot = structuredClone(input);
    const testModule = harness([
      exited(schema(value.repository, value.worktree, { includeFeature: false })),
      exited("this is deliberately not create JSON"),
      exited(schema(value.repository, value.worktree)),
    ]);

    const result = await testModule.module.ensureOwnedWorktree(input);

    assert.deepEqual(input, snapshot);
    assert.equal(result.recovered, false);
    assert.deepEqual(result.identity, identity(value.repository, value.worktree));
    assert.deepEqual(testModule.calls.map((call) => call.argv), [
      ["-C", value.repository, "--config-set", "list.json-schema=2", "list", "--format=json"],
      [
        "-C", value.repository, "switch", "--create", BRANCH, "--base", "main",
        "--no-hooks", "--no-cd", "--format=json",
      ],
      ["-C", value.repository, "--config-set", "list.json-schema=2", "list", "--format=json"],
    ]);
    assert.equal(testModule.calls.every((call) => call.executable === "/home/kas/.local/bin/wt"), true);
    assert.equal(testModule.calls.every((call) => call.timeoutMs === 15_000), true);
    assert.equal(testModule.calls.every((call) => call.maxStdoutBytes === 4 * 1024 * 1024), true);
    assert.equal(testModule.calls.every((call) => call.maxStderrBytes === 32 * 1024), true);
  } finally {
    value.cleanup();
  }
});

test("an existing deterministic branch is verified as create-before-record recovery without create", async () => {
  const value = fixture();
  try {
    const testModule = harness([exited(schema(value.repository, value.worktree))]);
    const result = await testModule.module.ensureOwnedWorktree({
      repositoryRoot: value.repository,
      runId: RUN_ID.toUpperCase(),
    });
    assert.equal(result.recovered, true);
    assert.equal(result.identity.branch, BRANCH);
    assert.equal(testModule.calls.length, 1);
  } finally {
    value.cleanup();
  }
});

test("ensure canonicalizes repository and worktree real paths without mutating input", async () => {
  const value = fixture();
  try {
    const repositoryAlias = join(value.temporary, "repository-alias");
    const worktreeAlias = join(value.temporary, "worktree-alias");
    symlinkSync(value.repository, repositoryAlias, "dir");
    symlinkSync(value.worktree, worktreeAlias, "dir");
    const input = { repositoryRoot: repositoryAlias, runId: RUN_ID };
    const testModule = harness([
      exited(schema(value.repository, worktreeAlias, { includeFeature: false })),
      exited("{}"),
      exited(schema(value.repository, worktreeAlias)),
    ]);
    const result = await testModule.module.ensureOwnedWorktree(input);
    assert.equal(input.repositoryRoot, repositoryAlias);
    assert.equal(result.identity.repositoryRoot, value.repository);
    assert.equal(result.identity.path, value.worktree);
    assert.equal(testModule.calls[0]?.argv[1], value.repository);
  } finally {
    value.cleanup();
  }
});

test("inspect performs exactly one fresh list, never mutates, and returns ready identity", async () => {
  const value = fixture();
  try {
    const expected = identity(value.repository, value.worktree);
    const snapshot = structuredClone(expected);
    const testModule = harness([exited(schema(value.repository, value.worktree))]);
    assert.deepEqual(await testModule.module.inspectOwnedWorktree(expected), {
      status: "ready",
      identity: expected,
    });
    assert.deepEqual(expected, snapshot);
    assert.equal(testModule.calls.length, 1);
    assert.equal(testModule.calls[0]?.argv.includes("switch"), false);
  } finally {
    value.cleanup();
  }
});

for (const [name, alter, expected] of [
  ["missing", () => ({ includeFeature: false }), { status: "missing", target: "worktree" }],
  [
    "duplicate",
    (value: ReturnType<typeof fixture>) => ({ extraItems: [item(BRANCH, value.worktree)] }),
    { status: "duplicate", count: 2 },
  ],
  ["detached", () => ({ feature: { detached: true } }), { status: "detached" }],
  ["branch mismatch", () => ({ feature: { branchMismatch: true } }), { status: "branch_mismatch" }],
  ["duplicate flag", () => ({ feature: { duplicateBranch: true } }), { status: "duplicate_branch" }],
  ["conflict", () => ({ feature: { change: "conflicted" } }), { status: "conflicted" }],
  [
    "active operation",
    () => ({ feature: { operation: "rebase" } }),
    { status: "active_operation", operation: "rebase" },
  ],
] as const) {
  test(`inspect fails closed for ${name}`, async () => {
    const value = fixture();
    try {
      const options = alter(value) as Parameters<typeof schema>[2];
      const testModule = harness([exited(schema(value.repository, value.worktree, options))]);
      assert.deepEqual(await testModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree)), expected);
    } finally {
      value.cleanup();
    }
  });
}

for (const change of ["staged", "modified", "untracked", "renamed", "deleted"] as const) {
  test(`inspect reports the ${change} dirty bit`, async () => {
    const value = fixture();
    try {
      const testModule = harness([
        exited(schema(value.repository, value.worktree, { feature: { change } })),
      ]);
      assert.deepEqual(
        await testModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree)),
        { status: "dirty", change },
      );
    } finally {
      value.cleanup();
    }
  });
}

for (const [name, expectedIdentity, options, expected] of [
  [
    "remote mismatch",
    { remoteUrl: "https://example.com/other/repository" },
    {},
    { status: "remote_mismatch", expected: "https://example.com/other/repository", actual: "https://example.com/team/repository" },
  ],
  [
    "base mismatch",
    {},
    { baseSha: OTHER_SHA },
    { status: "base_mismatch", expected: BASE_SHA, actual: OTHER_SHA },
  ],
  [
    "head mismatch",
    {},
    { feature: { sha: OTHER_SHA } },
    { status: "head_mismatch", expected: BASE_SHA, actual: OTHER_SHA },
  ],
] as const) {
  test(`inspect reports ${name}`, async () => {
    const value = fixture();
    try {
      const testModule = harness([
        exited(schema(value.repository, value.worktree, options as Parameters<typeof schema>[2])),
      ]);
      assert.deepEqual(
        await testModule.module.inspectOwnedWorktree(identity(
          value.repository,
          value.worktree,
          expectedIdentity as Partial<WorktreeIdentity>,
        )),
        expected,
      );
    } finally {
      value.cleanup();
    }
  });
}

test("inspect distinguishes no forge from a persisted remote and accepts matching null", async () => {
  const value = fixture();
  try {
    const absent = schema(value.repository, value.worktree, { remote: null });
    const mismatchModule = harness([exited(absent)]);
    assert.deepEqual(
      await mismatchModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree)),
      { status: "remote_mismatch", expected: "https://example.com/team/repository", actual: null },
    );
    const cleanModule = harness([exited(absent)]);
    assert.equal(
      (await cleanModule.module.inspectOwnedWorktree(identity(
        value.repository,
        value.worktree,
        { remoteUrl: null },
      ))).status,
      "ready",
    );
  } finally {
    value.cleanup();
  }
});

test("inspect reports wrong feature/default paths and a repository-root alias", async () => {
  const value = fixture();
  try {
    const wrongPath = join(value.temporary, "wrong-worktree");
    mkdirSync(wrongPath);
    const listedMismatch = harness([exited(schema(value.repository, wrongPath))]);
    assert.deepEqual(
      await listedMismatch.module.inspectOwnedWorktree(identity(value.repository, value.worktree)),
      { status: "path_mismatch", expected: value.worktree, actual: wrongPath },
    );
    const wrongRepository = join(value.temporary, "wrong-repository");
    mkdirSync(wrongRepository);
    const wrongRootModule = harness([exited(schema(wrongRepository, value.worktree))]);
    assert.deepEqual(
      await wrongRootModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree)),
      { status: "path_mismatch", expected: value.repository, actual: wrongRepository },
    );
    const alias = join(value.temporary, "repository-alias");
    symlinkSync(value.repository, alias, "dir");
    const aliasModule = harness([]);
    assert.deepEqual(
      await aliasModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree, { repositoryRoot: alias })),
      { status: "path_mismatch", expected: alias, actual: value.repository },
    );
    assert.equal(aliasModule.calls.length, 0);
  } finally {
    value.cleanup();
  }
});

test("missing default branch and malformed schema/JSON fail closed before create", async () => {
  const value = fixture();
  try {
    const noMain = JSON.stringify({
      schema: 2,
      repo: { default_branch: "main" },
      items: [item(BRANCH, value.worktree)],
    });
    for (const [output, expected] of [
      [noMain, { status: "missing", target: "default_branch" }],
      ["not json", { status: "malformed_output", reason: "list output is not JSON" }],
      [JSON.stringify({ schema: 1 }), { status: "malformed_output", reason: "list output is not schema 2" }],
    ] as const) {
      const testModule = harness([exited(output)]);
      assert.deepEqual(
        await rejectedObservation(testModule.module.ensureOwnedWorktree({
          repositoryRoot: value.repository,
          runId: RUN_ID,
        })),
        expected,
      );
      assert.equal(testModule.calls.length, 1);
    }
  } finally {
    value.cleanup();
  }
});

test("unknown or malformed necessary schema fields fail closed", async () => {
  const value = fixture();
  try {
    const malformed = JSON.parse(schema(value.repository, value.worktree)) as {
      items: Array<{ worktree: { changes: Record<string, unknown> } }>;
    };
    delete malformed.items[1]!.worktree.changes.untracked;
    const testModule = harness([exited(JSON.stringify(malformed))]);
    assert.deepEqual(
      await testModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree)),
      { status: "malformed_output", reason: "items[1].worktree.changes.untracked is invalid" },
    );
  } finally {
    value.cleanup();
  }
});

test("invalid run IDs fail before any Worktrunk command and naming uses only the validated ID", async () => {
  const value = fixture();
  try {
    for (const runId of ["../escape", `${RUN_ID}/extra`, "KAS-743"]) {
      const testModule = harness([]);
      assert.equal(
        (await rejectedObservation(testModule.module.ensureOwnedWorktree({
          repositoryRoot: value.repository,
          runId,
        }))).status,
        "malformed_output",
      );
      assert.equal(testModule.calls.length, 0);
    }
  } finally {
    value.cleanup();
  }
});

for (const [command, results] of [
  ["list", [exited("untrusted stdout", 7, "untrusted stderr")]],
  [
    "create",
    [
      exited(""),
      exited("untrusted create stdout", 9, "untrusted create stderr"),
    ],
  ],
] as const) {
  test(`${command} nonzero exit is typed and preserves bounded streams`, async () => {
    const value = fixture();
    try {
      const queued = [...results] as Result[];
      if (command === "create") queued[0] = exited(schema(value.repository, value.worktree, { includeFeature: false }));
      const testModule = harness(queued);
      const observation = await rejectedObservation(testModule.module.ensureOwnedWorktree({
        repositoryRoot: value.repository,
        runId: RUN_ID,
      }));
      assert.equal(observation.status, "command_failed");
      if (observation.status === "command_failed") {
        assert.equal(observation.command, command);
        assert.equal(observation.exitCode, command === "list" ? 7 : 9);
        assert.match(observation.stderr, /untrusted stderr|untrusted create stderr/u);
      }
    } finally {
      value.cleanup();
    }
  });
}

for (const execution of [
  { type: "timeout", stdout: "partial", stderr: "late" },
  { type: "output_limit", stream: "stderr", stdout: "partial", stderr: "large" },
] satisfies Result[]) {
  test(`runner ${execution.type} result is preserved as a typed observation`, async () => {
    const value = fixture();
    try {
      const testModule = harness([execution]);
      const observation = await testModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree));
      assert.equal(observation.status, execution.type);
      if (observation.status === execution.type) assert.equal(observation.command, "list");
    } finally {
      value.cleanup();
    }
  });
}

test("oversized injected stdout and stderr are bounded even if a runner violates its contract", async () => {
  const value = fixture();
  try {
    for (const [stream, result] of [
      ["stdout", exited("x".repeat(4 * 1024 * 1024 + 1))],
      ["stderr", exited("", 1, "x".repeat(32 * 1024 + 1))],
    ] as const) {
      const testModule = harness([result]);
      const observation = await testModule.module.inspectOwnedWorktree(identity(value.repository, value.worktree));
      assert.equal(observation.status, "output_limit");
      if (observation.status === "output_limit") {
        assert.equal(observation.stream, stream);
        assert.ok(Buffer.byteLength(observation.stdout) <= 4 * 1024 * 1024);
        assert.ok(Buffer.byteLength(observation.stderr) <= 32 * 1024);
      }
    }
  } finally {
    value.cleanup();
  }
});

test("inspect rejects tampered inactive/clean identity fields without mutation", async () => {
  const value = fixture();
  try {
    for (const corrupted of [
      { ...identity(value.repository, value.worktree), operation: "merge" },
      { ...identity(value.repository, value.worktree), clean: false },
    ]) {
      const testModule = harness([]);
      assert.equal(
        (await testModule.module.inspectOwnedWorktree(corrupted as unknown as WorktreeIdentity)).status,
        "malformed_output",
      );
      assert.equal(testModule.calls.length, 0);
    }
  } finally {
    value.cleanup();
  }
});

test("the stage-one module intentionally exposes no remove, force-remove, or reap operation", () => {
  const testModule = harness([]).module as unknown as Record<string, unknown>;
  assert.equal("remove" in testModule, false);
  assert.equal("reap" in testModule, false);
  assert.equal("forceRemove" in testModule, false);
});
