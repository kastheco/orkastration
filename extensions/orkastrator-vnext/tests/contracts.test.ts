import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "../contracts/fixtures");

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf8")) as Record<string, unknown>;
}

function record(value: unknown): Record<string, unknown> {
  assert.equal(typeof value, "object");
  assert.notEqual(value, null);
  assert.equal(Array.isArray(value), false);
  return value as Record<string, unknown>;
}

test("Worktrunk fixture pins the installed binary and machine-readable identity contract", () => {
  const data = fixture("worktrunk-v0.75.0.json");
  const tool = record(data.tool);
  const observations = record(data.observations);
  const create = record(observations.create);
  const list = record(observations.list_schema_2);
  const listStdout = record(list.stdout);

  assert.equal(tool.version, "0.75.0");
  assert.match(String(tool.executable_sha256), /^[0-9a-f]{64}$/u);
  assert.equal(create.exit_code, 0);
  assert.equal(record(create.stdout).action, "created");
  assert.equal(listStdout.schema, 2);
  assert.deepEqual(record(listStdout.repo), { default_branch: "main" });
  const item = record(listStdout.sample_item);
  assert.deepEqual(record(item.head), {
    sha: "<sha>",
    short_sha: "<short-sha>",
    subject: "base",
    committed_at: "<timestamp>",
  });
  assert.deepEqual(record(item.worktree), {
    path: "<repo>.contract-create",
    main: false,
    current: false,
    previous: false,
    detached: false,
    branch_mismatch: false,
    duplicate_branch: false,
    changes: {
      staged: false,
      modified: false,
      untracked: false,
      renamed: false,
      deleted: false,
      conflicted: false,
      diff: { added: 0, deleted: 0 },
    },
  });
  assert.deepEqual(record(item.default_branch), {
    ahead: 0,
    behind: 0,
    diff: { added: 0, deleted: 0 },
    orphan: false,
    integration: { reason: "same_commit" },
    merge_conflicts: false,
  });
  assert.deepEqual(record(item.display), { state: "empty", symbols: "_" });
});

test("Worktrunk failures are typed from exit status and streams, not assumed JSON", () => {
  const observations = record(fixture("worktrunk-v0.75.0.json").observations);
  const hook = record(observations.blocking_hook_failure);
  const conflict = record(observations.merge_conflict);
  const conflictIdentity = record(conflict.list_schema_2);

  assert.equal(hook.exit_code, 23);
  assert.equal(hook.stdout, "");
  assert.equal(hook.stdout_is_json, false);
  assert.ok((hook.stderr_contains as string[]).includes("hook-out"));
  assert.deepEqual(record(hook.post_failure_identity), {
    branch: "hook-fail",
    head_sha_present: true,
    worktree: {
      detached: false,
      branch_mismatch: false,
      operation_present: false,
      changes: { conflicted: false },
    },
  });
  assert.equal(conflict.exit_code, 1);
  assert.equal(conflict.stdout_is_json, false);
  assert.deepEqual(record(record(conflictIdentity.worktree).changes), { conflicted: true });
  assert.equal(record(conflictIdentity.worktree).operation, "rebase");
  assert.equal(record(conflictIdentity.default_branch).merge_conflicts, true);
});

test("Worktrunk removal and process reaping fixtures prove the destructive result", () => {
  const observations = record(fixture("worktrunk-v0.75.0.json").observations);
  const removal = record(observations.foreground_remove);
  const reap = record(observations.process_reap);

  assert.equal(removal.exit_code, 0);
  assert.equal(record((removal.stdout as unknown[])[0]).branch_outcome, "deleted");
  assert.equal(reap.exit_code, 0);
  assert.equal(reap.process_alive_before, true);
  assert.equal(reap.process_alive_after, false);
  assert.equal(reap.process_exit_signal, "SIGTERM");
  assert.match(String(reap.reporting_gap), /stderr reported it as ignored/u);
});

test("Pi fixture proves correlated RPC lifecycle boundaries on the pinned package", () => {
  const data = fixture("pi-v0.84.3.json");
  const tool = record(data.tool);
  const observations = record(data.observations);
  const prompt = record(observations.prompt);
  const stats = record(observations.session_stats);
  const abort = record(observations.abort);
  const framing = record(observations.stdout_framing);
  const shutdown = record(observations.shutdown);

  assert.equal(tool.version, "0.84.3");
  assert.equal(prompt.response_success, true);
  assert.equal(prompt.response_preceded_agent_settled, true);
  assert.equal(prompt.last_assistant_text, "CONTRACT_OK");
  assert.equal(prompt.agent_settled_observed, true);
  assert.equal(stats.user_messages, 1);
  assert.equal(stats.assistant_messages, 1);
  assert.equal(stats.total_messages, 2);
  assert.equal(stats.context_usage_present, true);
  assert.equal(abort.response_success, true);
  assert.equal(abort.agent_start_observed, true);
  assert.equal(abort.agent_end_observed, true);
  assert.equal(abort.agent_settled_observed, true);
  assert.equal(abort.agent_end_preceded_agent_settled, true);
  assert.equal(framing.crlf_records_observed, false);
  assert.equal(framing.final_record_terminated_by_lf, true);
  assert.equal(shutdown.signal_sent, "SIGTERM");
  assert.equal(shutdown.process_exit_code, 143);
  assert.deepEqual(shutdown.extension_event, { type: "session_shutdown", reason: "quit" });
});
