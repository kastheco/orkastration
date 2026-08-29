import {
  type RunLedger,
  LedgerError,
  RebindConflictError,
  ownershipFingerprint,
} from "./ledger/file-ledger.ts";
import {
  type CreateRunInput,
  type CurrentWorktreeIdentity,
  type OwnedProcessIdentity,
  type RunRecord,
} from "./ledger/types.ts";
import { isCurrentWorktreeIdentity } from "./ledger/worktree-identity.ts";

export type SessionStartReason = "startup" | "reload" | "new" | "resume" | "fork";
export type SessionShutdownReason = "quit" | "reload" | "new" | "resume" | "fork";

export interface RebindIdentityVerifier {
  verifyProcess(identity: OwnedProcessIdentity): Promise<boolean> | boolean;
  verifyWorktree(identity: CurrentWorktreeIdentity): Promise<boolean> | boolean;
}

export interface SessionStartResult {
  rebound?: RunRecord;
  interrupted?: RunRecord;
  stale: RunRecord[];
}

export interface SessionShutdownResult {
  reloadPending?: RunRecord;
  interrupted?: RunRecord;
}

const FAIL_CLOSED_VERIFIER: RebindIdentityVerifier = {
  verifyProcess: () => false,
  verifyWorktree: () => false,
};

export class LifecycleCoordinator {
  readonly #ledger: RunLedger;
  readonly #verifier: RebindIdentityVerifier;

  constructor(ledger: RunLedger, verifier: RebindIdentityVerifier = FAIL_CLOSED_VERIFIER) {
    this.#ledger = ledger;
    this.#verifier = verifier;
  }

  startRun(input: CreateRunInput): RunRecord {
    return this.#ledger.createRun(input);
  }

  async sessionStart(
    reason: SessionStartReason,
    sessionId: string,
    hostPid = process.pid,
    repositoryRoot?: string,
  ): Promise<SessionStartResult> {
    if (reason !== "reload") return { stale: this.#ledger.scanNonterminal() };

    const owned = this.#ledger.activeRunsForSession(sessionId);
    if (owned.length === 0) return { stale: this.#ledger.scanNonterminal() };
    if (owned.length > 1) {
      throw new LedgerError(`session ${sessionId} owns more than one active run`);
    }
    const run = owned[0]!;
    if (await this.#canRebind(run, sessionId, hostPid, repositoryRoot)) {
      try {
        const rebound = this.#ledger.rebind(
          run.runId,
          sessionId,
          repositoryRoot!,
          run.sequence,
          ownershipFingerprint(run),
          hostPid,
        );
        return {
          rebound,
          stale: this.#ledger.scanNonterminal().filter((item) => item.runId !== rebound.runId),
        };
      } catch (error) {
        if (!(error instanceof RebindConflictError)) throw error;
      }
    }
    const interrupted = this.#ledger.transition(run.runId, {
      state: "interrupted",
      reason: "reload_continuity_unproven",
      ruleId: "lifecycle.reload_fail_closed",
      actor: "extension",
      evidence: {
        sessionId,
        hostPid,
        generation: run.generation,
        reloadMarkerPresent: run.reload !== undefined,
        ownedProcessCount: run.ownedProcesses.length,
        worktreeCount: run.worktrees.length,
        expectedRepositoryRoot: run.repositoryRoot,
        observedRepositoryRoot: repositoryRoot ?? null,
      },
    });
    return { interrupted, stale: this.#ledger.scanNonterminal() };
  }

  sessionShutdown(
    reason: SessionShutdownReason,
    sessionId: string,
    hostPid = process.pid,
    repositoryRoot?: string,
    expectedRunId?: string,
  ): SessionShutdownResult {
    const owned = this.#ledger
      .activeRunsForSession(sessionId)
      .filter((run) => expectedRunId === undefined || run.runId === expectedRunId);
    if (owned.length === 0) return {};
    if (owned.length > 1) {
      throw new LedgerError(`session ${sessionId} owns more than one active run`);
    }
    const run = owned[0]!;
    if (reason === "reload") {
      if (repositoryRoot === undefined || repositoryRoot !== run.repositoryRoot) {
        return {
          interrupted: this.#ledger.transition(run.runId, {
            state: "interrupted",
            reason: "reload_repository_identity_unproven",
            ruleId: "lifecycle.reload_repository_fail_closed",
            actor: "extension",
            evidence: {
              expectedRepositoryRoot: run.repositoryRoot,
              observedRepositoryRoot: repositoryRoot ?? null,
            },
          }),
        };
      }
      return {
        reloadPending: this.#ledger.prepareReload(
          run.runId,
          sessionId,
          repositoryRoot,
          hostPid,
        ),
      };
    }
    return {
      interrupted: this.#ledger.transition(run.runId, {
        state: "interrupted",
        reason: `session_shutdown:${reason}`,
        ruleId: "lifecycle.session_shutdown",
        actor: "extension",
        evidence: {
          sessionId,
          reason,
          hostPid,
          preservedWorktrees: run.worktrees.map((worktree) => worktree.path),
        },
      }),
    };
  }

  async #canRebind(
    run: RunRecord,
    sessionId: string,
    hostPid: number,
    repositoryRoot: string | undefined,
  ): Promise<boolean> {
    const marker = run.reload;
    if (
      marker === undefined ||
      marker.sessionId !== sessionId ||
      marker.generation !== run.generation ||
      marker.hostPid !== hostPid ||
      run.hostPid !== hostPid ||
      repositoryRoot === undefined ||
      repositoryRoot !== run.repositoryRoot ||
      marker.repositoryRoot !== repositoryRoot
    ) {
      return false;
    }
    try {
      const processProofs = await Promise.all(
        run.ownedProcesses.map((identity) => this.#verifier.verifyProcess(identity)),
      );
      if (processProofs.some((verified) => !verified)) return false;
      if (run.worktrees.some((identity) => !isCurrentWorktreeIdentity(identity))) return false;
      const worktreeProofs = await Promise.all(
        run.worktrees.map((identity) => this.#verifier.verifyWorktree(
          identity as CurrentWorktreeIdentity,
        )),
      );
      return worktreeProofs.every(Boolean);
    } catch {
      return false;
    }
  }
}
