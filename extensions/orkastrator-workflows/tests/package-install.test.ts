import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const projectRoot = resolve(import.meta.dirname, "../../..");

test("the packed package applies its pi-workflows patch without dev dependencies", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-package-install-"));
  const packed = join(root, "packed");
  const consumer = join(root, "consumer");
  try {
    await mkdir(packed, { recursive: true });
    await mkdir(consumer, { recursive: true });
    const { stdout } = await execFileAsync(
      "npm",
      ["pack", "--json", "--pack-destination", packed],
      { cwd: projectRoot, encoding: "utf8", timeout: 60_000 },
    );
    const result = JSON.parse(stdout) as Array<{ filename: string }>;
    assert.equal(result.length, 1);
    const tarball = join(packed, result[0]!.filename);

    await writeFile(join(consumer, "package.json"), JSON.stringify({
      name: "orkastrator-package-consumer",
      version: "1.0.0",
      private: true,
      type: "module",
    }), "utf8");
    await execFileAsync(
      "npm",
      [
        "install",
        "--omit=dev",
        "--legacy-peer-deps",
        "--no-package-lock",
        "--no-audit",
        "--no-fund",
        tarball,
      ],
      { cwd: consumer, encoding: "utf8", timeout: 120_000 },
    );
    const imported = await execFileAsync(
      process.execPath,
      [
        "--input-type=module",
        "-e",
        "import { readWorkflowContinuationRunId } from '@osolmaz/pi-workflows'; console.log(typeof readWorkflowContinuationRunId)",
      ],
      { cwd: consumer, encoding: "utf8", timeout: 30_000 },
    );
    assert.equal(imported.stdout.trim(), "function");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
