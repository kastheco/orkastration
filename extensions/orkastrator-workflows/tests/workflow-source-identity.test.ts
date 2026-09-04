import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { promisify } from "node:util";

import planChangeWorkflow from "../../../.pi/workflows/orkastrator-plan-change.workflow.ts";

const execFileAsync = promisify(execFile);
const repository = resolve(import.meta.dirname, "../../..");
const localPackage = join(repository, "node_modules/@osolmaz/pi-workflows");
const fixture = join(import.meta.dirname, "fixtures/canonical-builtin.workflow.ts");

const resolverProgram = `
  import { pathToFileURL } from "node:url";
  const packageRoot = process.argv[1];
  const workflowPath = process.argv[2];
  const cwd = process.argv[3];
  const { resolveWorkflowRef, resolveWorkflowSource } = await import(pathToFileURL(packageRoot + "/dist/workflows/loader.js"));
  const { builtinWorkflowCatalog } = await import(pathToFileURL(packageRoot + "/dist/builtins/catalog.js"));
  if (process.argv[4]) {
    await resolveWorkflowSource(JSON.parse(process.argv[4]), builtinWorkflowCatalog, "source-change-test", { cwd });
    process.stdout.write("verified");
  } else {
    const resolved = await resolveWorkflowRef(workflowPath, { cwd }, builtinWorkflowCatalog);
    process.stdout.write(JSON.stringify({ source: resolved.source, sources: resolved.sources }));
  }
`;

test("separate package instances preserve registered built-in identities and file fingerprints", async () => {
  const root = await mkdtemp(join(repository, ".tmp-piw-contexts-"));
  const launcherPackage = join(root, "launcher/pi-workflows");
  const workerPackage = join(root, "worker/pi-workflows");
  try {
    await Promise.all([
      cp(localPackage, launcherPackage, { recursive: true }),
      cp(localPackage, workerPackage, { recursive: true }),
    ]);
    const launcher = await resolveFixture(launcherPackage);
    const worker = await resolveFixture(workerPackage);

    assert.deepEqual(worker.source, launcher.source);
    assert.deepEqual(canonicalSources(worker.sources), canonicalSources(launcher.sources));
    assert.deepEqual(fileSourceFingerprints(worker.sources), fileSourceFingerprints(launcher.sources));
    assert.ok(fileSourcePaths(launcher.sources).every((path) => path.startsWith(launcherPackage)));
    assert.ok(fileSourcePaths(worker.sources).every((path) => path.startsWith(workerPackage)));
    assert.deepEqual(launcher.sources[0], {
      mountPath: ["implementation"],
      workflowName: "autoimplement",
      source: { kind: "builtin", id: "autoimplement", revision: "11" },
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("production workflows use canonical package references", async () => {
  const expectations = [
    [join(repository, ".pi/workflows/orkastrator-cook.workflow.ts"), ["planning/design", "planning/documentation", "planning/approval", "implementation"]],
    [join(repository, ".pi/workflows/orkastrator-implement.workflow.ts"), ["implementation"]],
    [join(repository, ".pi/workflows/orkastrator-plan-change.workflow.ts"), ["design", "documentation", "approval"]],
  ] as const;

  for (const [workflowPath, builtinMounts] of expectations) {
    const resolved = await resolveFixture(localPackage, workflowPath);
    for (const mount of builtinMounts) {
      const source = resolved.sources.find((entry) =>
        (entry as { mountPath: string[] }).mountPath.join("/") === mount
      ) as { source?: { kind?: string } } | undefined;
      assert.equal(source?.source?.kind, "builtin", `${workflowPath} mount ${mount}`);
    }
  }
});

test("local plan-change preserves the pinned built-in input contract", () => {
  const preparedWorkspace = {
    schema: "pi-workflows.prepared-workspace.v1",
    mode: "worktree",
    repository,
    worktreePath: repository,
    baseBranch: "main",
    baseRevision: "a".repeat(40),
    workBranch: "test",
    directDefaultBranchAuthorized: false,
    preExistingChangedPaths: [],
    evidence: ["test fixture"],
    scope: "test",
  };
  const verificationChecks = [{ id: "test", command: "npm", args: ["test"] }];
  const parsed = planChangeWorkflow.input?.({
    task: "test",
    preparedWorkspace,
    directDefaultBranchAuthorized: true,
    verificationChecks,
  }) as Record<string, unknown>;
  assert.deepEqual(parsed.preparedWorkspace, preparedWorkspace);
  assert.equal(parsed.directDefaultBranchAuthorized, true);
  assert.deepEqual(parsed.verificationChecks, verificationChecks);
  assert.throws(
    () => planChangeWorkflow.input?.({
      task: "test",
      preparedWorkspace: { schema: "pi-workflows.prepared-workspace.v1" },
    }),
    /prepared workspace mode is invalid/u,
  );
});

test("canonical references do not weaken file source-change protection", async () => {
  const root = await mkdtemp(join(repository, ".tmp-workflow-source-"));
  const copied = join(root, "canonical-builtin.workflow.ts");
  try {
    await writeFile(copied, await readFile(fixture, "utf8"), "utf8");
    const before = await resolveFixture(localPackage, copied);
    await writeFile(copied, `${await readFile(copied, "utf8")}\n`, "utf8");
    const after = await resolveFixture(localPackage, copied);
    assert.notEqual(after.source.hash, before.source.hash);
    assert.deepEqual(after.sources, before.sources);
    await assert.rejects(
      execFileAsync(
        process.execPath,
        [
          "--input-type=module",
          "--eval",
          resolverProgram,
          localPackage,
          copied,
          repository,
          JSON.stringify(before.source),
        ],
        {
          cwd: repository,
          encoding: "utf8",
          env: { ...process.env, PIW_CONTRACT_PACKAGE_ROOT: localPackage },
        },
      ),
      /Workflow source changed since run source-change-test started/u,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

function canonicalSources(sources: unknown[]): unknown[] {
  return sources.filter((entry) =>
    (entry as { source?: { kind?: string } }).source?.kind === "builtin"
  );
}

function fileSourceFingerprints(sources: unknown[]): unknown[] {
  return sources.flatMap((entry) => {
    const mounted = entry as {
      mountPath?: string[];
      workflowName?: string;
      source?: { kind?: string; hash?: string };
    };
    if (mounted.source?.kind !== "file") return [];
    return [{
      mountPath: mounted.mountPath,
      workflowName: mounted.workflowName,
      hash: mounted.source.hash,
    }];
  });
}

function fileSourcePaths(sources: unknown[]): string[] {
  return sources.flatMap((entry) => {
    const source = (entry as { source?: { kind?: string; path?: string } }).source;
    return source?.kind === "file" && typeof source.path === "string" ? [source.path] : [];
  });
}

async function resolveFixture(
  contractPackage: string,
  workflowPath = fixture,
): Promise<{ source: { kind: string; hash?: string }; sources: unknown[] }> {
  const result = await execFileAsync(
    process.execPath,
    ["--input-type=module", "--eval", resolverProgram, contractPackage, workflowPath, repository],
    {
      cwd: repository,
      encoding: "utf8",
      env: { ...process.env, PIW_CONTRACT_PACKAGE_ROOT: contractPackage },
    },
  );
  return JSON.parse(result.stdout) as {
    source: { kind: string; hash?: string };
    sources: unknown[];
  };
}
