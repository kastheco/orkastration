import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { discoverMonitorTasks } from "../discovery.ts";

const RUN_ID = "bb9a9b29-bd4e-4a34-91a0-d471eb4b0a28";

function fixture(id: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id,
    name: "KAS-706 monitor",
    command: `orkas monitor ${RUN_ID} --watch`,
    status: "running",
    outputPath: `.pi/tasks/session-current/${id}.output`,
    cwd: "/repo",
    startTime: 1_000,
    pid: 4242,
    bytesWritten: 0,
    isAgent: false,
    notified: false,
    notifyOnCompletion: true,
    triggerOnCompletion: true,
    ...overrides,
  };
}

test("discovers direct session task JSON defensively", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-monitors-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const current = join(root, ".pi", "tasks", "session-current");
  const ignored = join(root, ".pi", "tasks", "other");
  await mkdir(current, { recursive: true });
  await mkdir(ignored, { recursive: true });

  await Promise.all([
    writeFile(join(current, "live.json"), JSON.stringify(fixture("live"))),
    writeFile(join(current, "completed.json"), JSON.stringify(fixture("completed", { status: "completed", endTime: 2_000 }))),
    writeFile(join(current, "failed.json"), JSON.stringify(fixture("failed", { status: "failed", endTime: 2_000 }))),
    writeFile(join(current, "killed.json"), JSON.stringify(fixture("killed", { status: "killed", endTime: 2_000 }))),
    writeFile(join(current, "stale.json"), JSON.stringify(fixture("stale", { pid: 9999 }))),
    writeFile(join(current, "unrelated.json"), JSON.stringify(fixture("unrelated", { command: "sleep 5" }))),
    writeFile(join(current, "malformed.json"), "{not json"),
    writeFile(join(current, "not-json.txt"), "ignored"),
    writeFile(join(ignored, "hidden.json"), JSON.stringify(fixture("hidden"))),
  ]);
  await symlink(join(current, "live.json"), join(current, "link.json"));

  const tasks = await discoverMonitorTasks(root, (pid) => pid === 4242);
  assert.deepEqual(
    tasks.map((task) => [task.id, task.status, task.stale]).sort(),
    [
      ["completed", "completed", false],
      ["failed", "failed", false],
      ["killed", "killed", false],
      ["live", "running", false],
      ["stale", "running", true],
    ],
  );
});

test("missing task root is an empty discovery result", async () => {
  assert.deepEqual(await discoverMonitorTasks("/definitely/missing/orkastrator-project"), []);
});
