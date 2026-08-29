import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { RunLedger } from "../ledger/file-ledger.ts";
import type { RunRecord } from "../ledger/types.ts";
import {
  LifecycleCoordinator,
  type RebindIdentityVerifier,
  type SessionShutdownReason,
} from "../lifecycle.ts";

function ids(): () => string {
  let value = 100;
  return () => {
    value += 1;
    return `00000000-0000-4000-8000-${value.toString(16).padStart(12, "0")}`;
  };
}

function fixture(verifier?: RebindIdentityVerifier): {
  temporary: string;
  repository: string;
  ledger: RunLedger;
  coordinator: LifecycleCoordinator;
  cleanup: () => void;
} {
  const temporary = mkdtempSync(join(tmpdir(), "orkastrator-lifecycle-"));
  const repository = join(temporary, "repository");
  mkdirSync(repository);
  const ledger = new RunLedger({
    root: join(temporary, "state"),
    now: () => new Date("2026-08-28T22:00:00.000Z"),
    randomId: ids(),
  });
  return {
    temporary,
    repository,
    ledger,
    coordinator: new LifecycleCoordinator(ledger, verifier),
    cleanup: () => rmSync(temporary, { recursive: true, force: true }),
  };
}

function create(
  coordinator: LifecycleCoordinator,
  repository: string,
  sessionId = "supervisor-session",
  hostPid = 4242,
): RunRecord {
  return coordinator.startRun({
    objective: "Test lifecycle ownership.",
    supervisorSessionId: sessionId,
    repositoryRoot: repository,
    policySnapshot: "version: 1\n",
    hostPid,
  });
}

test("startup reports incomplete runs without claiming, resuming, or deleting them", async () => {
  const value = fixture();
  try {
    const created = create(value.coordinator, value.repository, "old-session");
    const result = await value.coordinator.sessionStart(
      "startup",
      "new-session",
      5000,
      value.repository,
    );

    assert.equal(result.rebound, undefined);
    assert.equal(result.interrupted, undefined);
    assert.deepEqual(result.stale.map((record) => record.runId), [created.runId]);
    assert.deepEqual(value.ledger.loadRun(created.runId).record, result.stale[0]);
    assert.equal(value.ledger.events(created.runId).length, 1);
  } finally {
    value.cleanup();
  }
});

test("same-session same-process reload rebinds when every recorded identity is proven", async () => {
  const verifier: RebindIdentityVerifier = {
    verifyProcess: (identity) => identity.attemptToken === "attempt-1",
    verifyWorktree: (identity) => identity.branch === "feature",
  };
  const value = fixture(verifier);
  try {
    const created = create(value.coordinator, value.repository);
    value.ledger.recordOwnership(created.runId, {
      ownedProcesses: [
        {
          pid: 6001,
          processGroupId: 6001,
          sessionFile: join(value.temporary, "worker.jsonl"),
          attemptToken: "attempt-1",
        },
      ],
      worktrees: [
        {
          repositoryRoot: value.repository,
          path: join(value.temporary, "worktree"),
          branch: "feature",
          headSha: "a".repeat(40),
        },
      ],
    });

    const firstShutdown = value.coordinator.sessionShutdown(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    const repeatedShutdown = value.coordinator.sessionShutdown(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    assert.equal(firstShutdown.reloadPending?.sequence, repeatedShutdown.reloadPending?.sequence);

    const result = await value.coordinator.sessionStart(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    assert.equal(result.rebound?.runId, created.runId);
    assert.equal(result.rebound?.generation, 2);
    assert.equal(result.rebound?.reload, undefined);
    assert.equal(result.interrupted, undefined);
    assert.deepEqual(result.stale, []);
  } finally {
    value.cleanup();
  }
});

test("reload fails closed when process, session, generation, or resource proof is absent", async () => {
  const value = fixture();
  try {
    const created = create(value.coordinator, value.repository);
    value.ledger.recordOwnership(created.runId, {
      ownedProcesses: [
        {
          pid: 6001,
          processGroupId: 6001,
          sessionFile: join(value.temporary, "worker.jsonl"),
          attemptToken: "attempt-1",
        },
      ],
      worktrees: [],
    });
    value.coordinator.sessionShutdown(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );

    const result = await value.coordinator.sessionStart(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    assert.equal(result.rebound, undefined);
    assert.equal(result.interrupted?.state, "interrupted");
    assert.equal(result.interrupted?.reason, "reload_continuity_unproven");
    assert.equal(value.ledger.scanNonterminal().length, 0);
  } finally {
    value.cleanup();
  }
});

test("reload verifier errors fail closed instead of leaving the run reload-pending", async () => {
  const value = fixture({
    verifyProcess: () => {
      throw new Error("probe failed");
    },
    verifyWorktree: () => true,
  });
  try {
    const created = create(value.coordinator, value.repository);
    value.ledger.recordOwnership(created.runId, {
      ownedProcesses: [
        {
          pid: 6001,
          processGroupId: 6001,
          sessionFile: join(value.temporary, "worker.jsonl"),
          attemptToken: "attempt-1",
        },
      ],
      worktrees: [],
    });
    value.coordinator.sessionShutdown(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );

    const result = await value.coordinator.sessionStart(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    assert.equal(result.interrupted?.state, "interrupted");
    assert.equal(result.interrupted?.reason, "reload_continuity_unproven");
  } finally {
    value.cleanup();
  }
});

test("reload compare-and-swap rejects ownership changed after identity verification", async () => {
  let mutateOwnership = () => {};
  const value = fixture({
    verifyProcess: () => {
      mutateOwnership();
      return true;
    },
    verifyWorktree: () => true,
  });
  try {
    const created = create(value.coordinator, value.repository);
    value.ledger.recordOwnership(created.runId, {
      ownedProcesses: [
        {
          pid: 6001,
          processGroupId: 6001,
          sessionFile: join(value.temporary, "worker-1.jsonl"),
          attemptToken: "attempt-1",
        },
      ],
      worktrees: [],
    });
    value.coordinator.sessionShutdown(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    let mutated = false;
    mutateOwnership = () => {
      if (mutated) return;
      mutated = true;
      value.ledger.recordOwnership(created.runId, {
        ownedProcesses: [
          {
            pid: 6002,
            processGroupId: 6002,
            sessionFile: join(value.temporary, "worker-2.jsonl"),
            attemptToken: "attempt-2",
          },
        ],
        worktrees: [],
      });
    };

    const result = await value.coordinator.sessionStart(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    assert.equal(result.rebound, undefined);
    assert.equal(result.interrupted?.state, "interrupted");
    assert.equal(result.interrupted?.ownedProcesses[0]?.attemptToken, "attempt-2");
  } finally {
    value.cleanup();
  }
});

test("reload with a changed host PID cannot claim a marker from the previous process", async () => {
  const value = fixture();
  try {
    const created = create(value.coordinator, value.repository);
    value.coordinator.sessionShutdown(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );

    const result = await value.coordinator.sessionStart(
      "reload",
      "supervisor-session",
      9999,
      value.repository,
    );
    assert.equal(result.interrupted?.runId, created.runId);
    assert.equal(result.interrupted?.reason, "reload_continuity_unproven");
  } finally {
    value.cleanup();
  }
});

test("reload cannot rebind from a different repository with matching session, PID, and generation", async () => {
  const value = fixture();
  try {
    const created = create(value.coordinator, value.repository);
    value.coordinator.sessionShutdown(
      "reload",
      "supervisor-session",
      4242,
      value.repository,
    );
    const otherRepository = join(value.temporary, "other-repository");
    mkdirSync(otherRepository);

    const result = await value.coordinator.sessionStart(
      "reload",
      "supervisor-session",
      4242,
      otherRepository,
    );
    assert.equal(result.rebound, undefined);
    assert.equal(result.interrupted?.runId, created.runId);
    assert.equal(result.interrupted?.reason, "reload_continuity_unproven");
  } finally {
    value.cleanup();
  }
});

for (const reason of ["new", "resume", "fork", "quit"] satisfies SessionShutdownReason[]) {
  test(`session shutdown reason ${reason} interrupts active work and preserves identity evidence`, () => {
    const value = fixture();
    try {
      const created = create(value.coordinator, value.repository);
      value.ledger.recordOwnership(created.runId, {
        ownedProcesses: [],
        worktrees: [
          {
            repositoryRoot: value.repository,
            path: join(value.temporary, "preserved-worktree"),
            branch: "feature",
            headSha: "b".repeat(40),
          },
        ],
      });

      const result = value.coordinator.sessionShutdown(
        reason,
        "supervisor-session",
        4242,
        value.repository,
      );
      assert.equal(result.interrupted?.state, "interrupted");
      assert.equal(result.interrupted?.reason, `session_shutdown:${reason}`);
      assert.deepEqual(result.interrupted?.worktrees.map((worktree) => worktree.path), [
        join(value.temporary, "preserved-worktree"),
      ]);
      assert.equal(value.ledger.scanNonterminal().length, 0);
      assert.deepEqual(
        value.coordinator.sessionShutdown(reason, "supervisor-session", 4242, value.repository),
        {},
      );
    } finally {
      value.cleanup();
    }
  });
}

test("quit shutdown records the lifecycle interruption used by Pi signals", () => {
  const value = fixture();
  try {
    const created = create(value.coordinator, value.repository);
    const result = value.coordinator.sessionShutdown(
      "quit",
      "supervisor-session",
      4242,
      value.repository,
    );
    assert.equal(result.interrupted?.runId, created.runId);
    assert.equal(result.interrupted?.reason, "session_shutdown:quit");
    assert.equal(value.ledger.events(created.runId).at(-1)?.ruleId, "lifecycle.session_shutdown");
  } finally {
    value.cleanup();
  }
});
