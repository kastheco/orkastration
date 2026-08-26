import assert from "node:assert/strict";
import { test } from "node:test";

import {
  type MonitorTask,
  parseMonitorCommand,
  parseMonitorTask,
  renderMonitorDetails,
  renderMonitorFooter,
} from "../core.ts";

const RUN_ID = "bb9a9b29-bd4e-4a34-91a0-d471eb4b0a28";

function fixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "b1234abcd",
    name: "KAS-706 monitor",
    command: `uv run --project /repo orkas monitor ${RUN_ID} --watch --interval 5 --json`,
    description: "watch the run",
    status: "running",
    outputPath: ".pi/tasks/session-abc-123/b1234abcd.output",
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

function task(overrides: Partial<MonitorTask> = {}): MonitorTask {
  return {
    id: "b1234abcd",
    name: "KAS-706 monitor",
    command: `orkas monitor ${RUN_ID} --watch`,
    status: "running",
    outputPath: ".pi/tasks/session-x/b1234abcd.output",
    startTime: 1_000,
    pid: 4242,
    runId: RUN_ID,
    stale: false,
    sourcePath: "/repo/.pi/tasks/session-x/b1234abcd.json",
    ...overrides,
  };
}

test("parses direct and uv-wrapped monitor commands narrowly", () => {
  assert.deepEqual(parseMonitorCommand(`orkas monitor ${RUN_ID} --watch`), {
    runId: RUN_ID,
  });
  assert.deepEqual(
    parseMonitorCommand(
      `uv run --project=/repo --frozen /venv/bin/orkastrator monitor ${RUN_ID} --json --watch --interval 2.5`,
    ),
    { runId: RUN_ID },
  );

  for (const command of [
    `orkas monitor ${RUN_ID}`,
    `orkas show ${RUN_ID} --watch`,
    `echo orkas monitor ${RUN_ID} --watch`,
    `uv run echo orkas monitor ${RUN_ID} --watch`,
    `orkas monitor not-a-uuid --watch`,
    `orkas monitor ${RUN_ID} --watch && echo done`,
    `orkas monitor ${RUN_ID} --watch --unknown`,
    `orkas monitor ${RUN_ID} --watch $(touch nope)`,
  ]) {
    assert.equal(parseMonitorCommand(command), undefined, command);
  }
});

test("parses a current bg_run JSON fixture and marks dead running tasks stale", () => {
  const live = parseMonitorTask(fixture(), "/fixture.json", (pid) => pid === 4242);
  assert.deepEqual(
    live,
    task({
      command: `uv run --project /repo orkas monitor ${RUN_ID} --watch --interval 5 --json`,
      outputPath: ".pi/tasks/session-abc-123/b1234abcd.output",
      sourcePath: "/fixture.json",
    }),
  );

  const stale = parseMonitorTask(fixture(), "/fixture.json", () => false);
  assert.equal(stale?.stale, true);
});

test("completed, failed, and killed records stay truthful and never become stale", () => {
  for (const status of ["completed", "failed", "killed"] as const) {
    const parsed = parseMonitorTask(
      fixture({ status, endTime: 9_000 }),
      `/${status}.json`,
      () => false,
    );
    assert.equal(parsed?.status, status);
    assert.equal(parsed?.stale, false);
  }
});

test("malformed and unrelated task records are ignored", () => {
  for (const value of [
    null,
    {},
    fixture({ id: "../escape" }),
    fixture({ command: "sleep 10" }),
    fixture({ status: "queued" }),
    fixture({ startTime: "yesterday" }),
    fixture({ outputPath: "" }),
  ]) {
    assert.equal(parseMonitorTask(value, "/fixture.json", () => true), undefined);
  }
});

test("running monitor renders a persistent compact footer", () => {
  assert.equal(renderMonitorFooter([task()]), "● KAS-706 monitor bb9a9b29");
  assert.equal(renderMonitorFooter([task({ status: "completed", endTime: 2_000 })]), undefined);
  assert.equal(renderMonitorFooter([task({ status: "failed", endTime: 2_000 })]), undefined);
  assert.equal(renderMonitorFooter([task({ status: "killed", endTime: 2_000 })]), undefined);
  assert.equal(renderMonitorFooter([task({ stale: true })]), undefined);
  assert.equal(renderMonitorFooter([]), undefined);
});

test("multiple monitors are bounded and summarized", () => {
  const tasks = Array.from({ length: 5 }, (_, index) =>
    task({
      id: `b${index}`,
      name: `KAS-${706 + index} monitor with a deliberately long label`,
      runId: `${String(index).repeat(8)}-bd4e-4a34-91a0-d471eb4b0a28`,
      startTime: index,
    }),
  );
  const footer = renderMonitorFooter(tasks, 48);
  assert.ok(footer);
  assert.ok(footer.length <= 48);
  assert.match(footer, /\+4$/u);
});

test("detail command reports only recorded task facts and computed elapsed time", () => {
  const details = renderMonitorDetails(
    [task({ status: "failed", endTime: 66_000, pid: 4242 })],
    100_000,
  );
  assert.equal(
    details,
    [
      "task id: b1234abcd",
      `run id: ${RUN_ID}`,
      "PID: 4242",
      "status: failed",
      "elapsed: 1m 05s",
      "output: .pi/tasks/session-x/b1234abcd.output",
    ].join("\n"),
  );
  assert.doesNotMatch(details, /review|worker phase/iu);
});
