import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const projectRoot = resolve(import.meta.dirname, "../../..");

test("the packed package applies its workflow and questionnaire patches without dev dependencies", async () => {
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
      dependencies: {
        "@earendil-works/pi-coding-agent": "0.84.3",
        "@earendil-works/pi-tui": "0.84.3",
        "@juicesharp/rpiv-ask-user-question": "2.8.0",
      },
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
        "--import",
        "tsx",
        "--input-type=module",
        "-e",
        "import { createRequire } from 'node:module'; import { pathToFileURL } from 'node:url'; const rootRequire = createRequire(import.meta.url); const orkRequire = createRequire(rootRequire.resolve('orkastrator-pi/package.json')); const workflowPath = orkRequire.resolve('@osolmaz/pi-workflows'); const workflows = await import(pathToFileURL(workflowPath).href); const extension = await import(pathToFileURL(orkRequire.resolve('@osolmaz/pi-workflows/extension')).href); const questionnaire = await import(pathToFileURL(orkRequire.resolve('@juicesharp/rpiv-ask-user-question')).href); console.log([workflowPath === rootRequire.resolve('@osolmaz/pi-workflows'), workflows.readWorkflowContinuationRunId, extension.pendingDecisionForSession, extension.registerWorkflowHumanDecisionPresenter, questionnaire.presentQuestionnaire, extension.answerExactPendingDecision].map((value) => typeof value === 'boolean' ? String(value) : typeof value).join(','))",
      ],
      { cwd: consumer, encoding: "utf8", timeout: 30_000 },
    );
    assert.equal(imported.stdout.trim(), "true,function,function,function,function,undefined");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
