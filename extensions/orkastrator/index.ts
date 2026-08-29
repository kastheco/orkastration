import { randomUUID } from "node:crypto";
import { realpathSync } from "node:fs";
import { join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { RunLedger } from "./ledger/file-ledger.ts";
import type { RunRecord } from "./ledger/types.ts";
import { LifecycleCoordinator } from "./lifecycle.ts";
import {
  type PiAttemptResult,
  runOwnedPiAttempt,
} from "./rpc/pi-attempt.ts";

export const STATUS_KEY = "orkastrator";
export const ENTRY_TYPE = "orkastrator-lifecycle";

export interface OrkastratorExtensionDependencies {
  ledger?: RunLedger;
  coordinator?: LifecycleCoordinator;
  stateRoot?: string;
  hostPid?: number;
  canonicalizeRepository?: (path: string) => string;
  piExecutable?: string;
  randomAttemptToken?: () => string;
  runAttempt?: typeof runOwnedPiAttempt;
}

type LifecycleContext = Pick<
  ExtensionContext,
  "cwd" | "isProjectTrusted" | "sessionManager" | "model" | "thinkingLevel"
> & {
  ui: Pick<ExtensionContext["ui"], "notify" | "setStatus">;
};

function sessionId(ctx: LifecycleContext): string {
  return ctx.sessionManager.getSessionId();
}

function statusText(records: RunRecord[]): string | undefined {
  if (records.length === 0) return undefined;
  const active = records[0]!;
  return `● ${active.runId.slice(0, 8)} ${active.state}`;
}

function staleSummary(records: RunRecord[]): string {
  return records
    .map((record) => `${record.runId.slice(0, 8)} ${record.state} session=${record.supervisorSessionId}`)
    .join("\n");
}

export function installOrkastrator(
  pi: ExtensionAPI,
  dependencies: OrkastratorExtensionDependencies = {},
): void {
  const configuredStateRoot = dependencies.stateRoot ?? process.env.ORKASTRATOR_STATE_DIR;
  const ledger =
    dependencies.ledger ??
    new RunLedger(configuredStateRoot === undefined ? {} : { root: configuredStateRoot });
  const coordinator = dependencies.coordinator ?? new LifecycleCoordinator(ledger);
  const hostPid = dependencies.hostPid ?? process.pid;
  const canonicalizeRepository = dependencies.canonicalizeRepository ?? realpathSync;
  const piExecutable =
    dependencies.piExecutable ?? resolve(import.meta.dirname, "../../node_modules/.bin/pi");
  const randomAttemptToken = dependencies.randomAttemptToken ?? randomUUID;
  const runAttempt = dependencies.runAttempt ?? runOwnedPiAttempt;
  let claimedRunId: string | undefined;
  let activeAttempt:
    | {
        runId: string;
        controller: AbortController;
        promise: Promise<PiAttemptResult>;
        cleanup: { reapAndOwnershipClearProven: boolean };
      }
    | undefined;

  const requireTrust = (ctx: LifecycleContext): void => {
    if (!ctx.isProjectTrusted()) throw new Error("Orkastrator requires project trust");
  };

  const reloadRepositoryIdentity = (ctx: LifecycleContext): string | undefined => {
    try {
      return canonicalizeRepository(ctx.cwd);
    } catch {
      return undefined;
    }
  };

  const refreshStatus = (ctx: LifecycleContext): RunRecord[] => {
    if (claimedRunId === undefined) {
      ctx.ui.setStatus(STATUS_KEY, undefined);
      return [];
    }
    const claimed = ledger
      .scanNonterminal()
      .find(
        (record) =>
          record.runId === claimedRunId && record.supervisorSessionId === sessionId(ctx),
      );
    if (claimed === undefined) {
      claimedRunId = undefined;
      ctx.ui.setStatus(STATUS_KEY, undefined);
      return [];
    }
    ctx.ui.setStatus(STATUS_KEY, statusText([claimed]));
    return [claimed];
  };

  pi.on("session_start", async (event, ctx) => {
    if (!ctx.isProjectTrusted()) {
      ctx.ui.setStatus(STATUS_KEY, undefined);
      return;
    }
    claimedRunId = undefined;
    const result = await coordinator.sessionStart(
      event.reason,
      sessionId(ctx),
      hostPid,
      event.reason === "reload" ? reloadRepositoryIdentity(ctx) : undefined,
    );
    if (result.rebound !== undefined) {
      claimedRunId = result.rebound.runId;
      pi.appendEntry(ENTRY_TYPE, {
        action: "reload_rebound",
        runId: result.rebound.runId,
        generation: result.rebound.generation,
      });
      ctx.ui.notify(`Rebound Orkastrator run ${result.rebound.runId.slice(0, 8)}.`, "info");
    }
    if (result.interrupted !== undefined) {
      pi.appendEntry(ENTRY_TYPE, {
        action: "reload_interrupted",
        runId: result.interrupted.runId,
        reason: result.interrupted.reason,
      });
      ctx.ui.notify(
        `Interrupted Orkastrator run ${result.interrupted.runId.slice(0, 8)} because reload continuity was not proven.`,
        "warning",
      );
    }
    if (result.stale.length > 0) {
      pi.appendEntry(ENTRY_TYPE, {
        action: "stale_runs_reported",
        runs: result.stale.map((record) => ({
          runId: record.runId,
          sessionId: record.supervisorSessionId,
          state: record.state,
          reason: record.reason,
        })),
      });
      ctx.ui.notify(
        `Incomplete Orkastrator runs were preserved without being claimed:\n${staleSummary(result.stale)}`,
        "warning",
      );
    }
    refreshStatus(ctx);
  });

  pi.on("session_shutdown", async (event, ctx) => {
    if (!ctx.isProjectTrusted()) return;
    let postCleanupAttemptError: unknown;
    if (activeAttempt !== undefined) {
      const attempt = activeAttempt;
      attempt.controller.abort();
      try {
        await attempt.promise;
      } catch (error) {
        if (!attempt.cleanup.reapAndOwnershipClearProven) throw error;
        postCleanupAttemptError = error;
      } finally {
        if (activeAttempt === attempt) activeAttempt = undefined;
      }
    }
    if (claimedRunId === undefined) {
      ctx.ui.setStatus(STATUS_KEY, undefined);
      if (postCleanupAttemptError !== undefined) throw postCleanupAttemptError;
      return;
    }
    const result = coordinator.sessionShutdown(
      event.reason,
      sessionId(ctx),
      hostPid,
      event.reason === "reload" ? reloadRepositoryIdentity(ctx) : undefined,
      claimedRunId,
    );
    claimedRunId = undefined;
    if (result.reloadPending !== undefined) {
      pi.appendEntry(ENTRY_TYPE, {
        action: "reload_pending",
        runId: result.reloadPending.runId,
        generation: result.reloadPending.generation,
      });
    }
    if (result.interrupted !== undefined) {
      pi.appendEntry(ENTRY_TYPE, {
        action: "run_interrupted",
        runId: result.interrupted.runId,
        reason: result.interrupted.reason,
      });
    }
    ctx.ui.setStatus(STATUS_KEY, undefined);
    if (postCleanupAttemptError !== undefined) throw postCleanupAttemptError;
  });

  pi.registerCommand("kas", {
    description: "Start and supervise an Orkastrator run",
    handler: async (args, ctx) => {
      try {
        requireTrust(ctx);
        const objective = args.trim();
        pi.sendUserMessage(
          objective.length === 0
            ? "Start a Pi-native Orkastrator v1 worker attempt. Ask me for the objective before calling orkastrator_run_create. Do not use the legacy orkas CLI or Orca."
            : `Start a Pi-native Orkastrator v1 worker attempt for this objective: ${objective}\n\nUse orkastrator_run_create directly. Do not use the legacy orkas CLI or Orca.`,

        );
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("kas-runs", {
    description: "Report active and preserved Orkastrator runs",
    handler: async (_args, ctx) => {
      try {
        requireTrust(ctx);
        const owned = refreshStatus(ctx);
        const stale = ledger
          .scanNonterminal()
          .filter((record) => record.runId !== claimedRunId);
        const lines = [
          owned.length === 0
            ? "This session owns no active Orkastrator run."
            : `Active: ${staleSummary(owned)}`,
          stale.length === 0
            ? "No preserved runs."
            : `Preserved, not claimed:\n${staleSummary(stale)}`,
        ];
        ctx.ui.notify(lines.join("\n"), "info");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerTool({
    name: "orkastrator_run_create",
    label: "Create Orkastrator Run",
    description:
      "Create one Orkastrator v1 run, snapshot its effective policy, and execute one fresh owned Pi RPC worker attempt. KAS-742 will replace the caller-supplied policy seam with repository profile resolution.",
    parameters: Type.Object(
      {
        objective: Type.String({ minLength: 1, maxLength: 8_000 }),
        policySnapshot: Type.String({ minLength: 1, maxLength: 1_048_576 }),
      },
      { additionalProperties: false },
    ),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      requireTrust(ctx);
      if (ctx.model === undefined) throw new Error("Orkastrator requires an active Pi model");
      const record = coordinator.startRun({
        objective: params.objective,
        policySnapshot: params.policySnapshot,
        supervisorSessionId: sessionId(ctx),
        repositoryRoot: canonicalizeRepository(ctx.cwd),
        hostPid,
      });
      claimedRunId = record.runId;
      pi.appendEntry(ENTRY_TYPE, {
        action: "run_created",
        runId: record.runId,
        policyHash: record.policyHash,
      });
      refreshStatus(ctx);
      const attemptToken = randomAttemptToken();
      const controller = new AbortController();
      const onToolAbort = (): void => controller.abort();
      signal?.addEventListener("abort", onToolAbort, { once: true });
      if (signal?.aborted) controller.abort();
      const cleanup = { reapAndOwnershipClearProven: false };
      const promise = runAttempt(
        {
          executable: piExecutable,
          cwd: record.repositoryRoot,
          sessionFile: join(ledger.runDirectory(record.runId), `worker-${attemptToken}.jsonl`),
          attemptToken,
          prompt: record.objective,
          model: `${ctx.model.provider}/${ctx.model.id}`,
          thinking: ctx.thinkingLevel ?? "off",
          journalOwnership: (identity) => {
            ledger.journalOwnedProcess(record.runId, attemptToken, identity ?? undefined);
            // runOwnedPiAttempt clears only after proving process-group absence.
            if (identity === null) cleanup.reapAndOwnershipClearProven = true;
          },
          recordEvent: (event) => {
            pi.appendEntry(ENTRY_TYPE, { action: "worker_event", runId: record.runId, event });
          },
        },
        controller.signal,
      );
      activeAttempt = { runId: record.runId, controller, promise, cleanup };
      try {
        const attempt = await promise;
        return {
          content: [
            {
              type: "text",
              text: `Created Orkastrator run ${record.runId}; worker attempt ${attempt.status}.`,
            },
          ],
          details: { run: ledger.loadRun(record.runId).record, attempt },
        };
      } finally {
        signal?.removeEventListener("abort", onToolAbort);
        if (activeAttempt?.runId === record.runId) activeAttempt = undefined;
      }
    },
  });

  pi.registerTool({
    name: "orkastrator_owner_answer",
    label: "Answer Orkastrator Run",
    description:
      "Record an owner decision for a run in awaiting_owner and resume the same run. The decision must be one of the ledger's recorded allowed decisions.",
    parameters: Type.Object(
      {
        runId: Type.String({ minLength: 36, maxLength: 36 }),
        decision: Type.String({ minLength: 1, maxLength: 200 }),
        rationale: Type.String({ minLength: 1, maxLength: 4_000 }),
      },
      { additionalProperties: false },
    ),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      requireTrust(ctx);
      if (claimedRunId !== params.runId) {
        throw new Error(`run ${params.runId} is preserved but not claimed by this extension instance`);
      }
      const current = ledger.loadRun(params.runId).record;
      if (current.supervisorSessionId !== sessionId(ctx)) {
        claimedRunId = undefined;
        throw new Error(`run ${params.runId} is owned by another supervisor session`);
      }
      const record = ledger.answerOwner(params.runId, {
        decision: params.decision,
        rationale: params.rationale,
      });
      pi.appendEntry(ENTRY_TYPE, {
        action: "owner_answered",
        runId: record.runId,
        decision: params.decision,
      });
      refreshStatus(ctx);
      return {
        content: [
          {
            type: "text",
            text: `Recorded owner decision ${params.decision} and resumed run ${record.runId}.`,
          },
        ],
        details: { run: record },
      };
    },
  });
}

export default function orkastrator(pi: ExtensionAPI): void {
  installOrkastrator(pi);
}
