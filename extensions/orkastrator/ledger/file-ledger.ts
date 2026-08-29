import { spawnSync } from "node:child_process";
import {
  closeSync,
  constants,
  existsSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
  writeSync,
} from "node:fs";
import { constants as osConstants, homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { isDeepStrictEqual } from "node:util";

import { parsePolicyV1, type PolicyV1 } from "../policy.ts";
import {
  reducePolicy,
  type PolicyAction,
  type PolicyCheckpoint,
  type PolicyEvent,
  type PolicyReduction,
} from "../reducer.ts";
import {
  type AwaitOwnerInput,
  type CreateRunInput,
  type JsonObject,
  type OwnerAnswerInput,
  type OwnedProcessExitEvidence,
  type OwnedProcessIdentity,
  type OwnershipInput,
  type PolicyActionDeliveryEvidence,
  RUN_STATES,
  type RunActor,
  type RunEvent,
  type RunLoadResult,
  type RunRecord,
  type RunState,
  TERMINAL_RUN_STATES,
  type TransitionInput,
  type WorkerEvent,
} from "./types.ts";

const MAX_POLICY_BYTES = 1_048_576;
const MAX_OBJECTIVE_CHARS = 8_000;
const MAX_WORKER_EVENT_BYTES = 32 * 1024;
const MAX_WORKER_EVENT_TEXT_CHARS = 4_000;
// A 4 MiB applied-event evidence bound comfortably covers MAX_FINDINGS (1,000).
const MAX_POLICY_EVENT_EVIDENCE_BYTES = 4 * 1024 * 1024;
const MAX_POLICY_DELIVERY_EVIDENCE_BYTES = 1024 * 1024;
const MAX_POLICY_DELIVERY_TEXT_CHARS = 512;
const EXIT_SIGNAL_SET = new Set<string>(Object.keys(osConstants.signals));
const STATELESS_EVENT_KEYS = [
  "schemaVersion",
  "eventId",
  "runId",
  "sequence",
  "timestamp",
  "type",
  "ruleId",
  "actor",
  "evidence",
  "projection",
];
const POLICY_DELIVERY_EVENT_KEYS = [
  ...STATELESS_EVENT_KEYS,
  "actionId",
  "policyRevision",
  "actionType",
  "delivery",
];
const RUN_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;
const RUN_STATE_SET = new Set<string>(RUN_STATES);
const RUN_ACTOR_SET = new Set<string>([
  "owner",
  "extension",
  "supervisor",
  "worker",
  "system",
]);

export class LedgerError extends Error {}
export class LedgerCorruptionError extends LedgerError {}
export class ActiveRunError extends LedgerError {}
export class RebindConflictError extends LedgerError {}

export function ownershipFingerprint(
  record: Pick<RunRecord, "ownedProcesses" | "worktrees">,
): string {
  return createHash("sha256")
    .update(JSON.stringify({
      ownedProcesses: record.ownedProcesses,
      worktrees: record.worktrees,
    }))
    .digest("hex");
}

export interface RunLedgerOptions {
  root?: string;
  now?: () => Date;
  randomId?: () => string;
}

interface EventInput {
  type: string;
  ruleId: string;
  actor: RunActor;
  evidence?: JsonObject;
  fromState?: RunState;
  toState?: RunState;
  actionId?: string;
  policyRevision?: number;
  actionType?: PolicyAction["type"];
  delivery?: "delivered";
}

export interface PolicyApplyResult {
  record: RunRecord;
  reduction: PolicyReduction;
  delivery: "pending" | "delivered";
  appended: boolean;
}

interface ParsedEvents {
  events: RunEvent[];
  droppedBytes: number;
  completeBytes: Buffer;
}

export class RunLedger {
  readonly root: string;
  readonly #now: () => Date;
  readonly #randomId: () => string;
  #lockDepth = 0;

  constructor(options: RunLedgerOptions = {}) {
    const requestedRoot = resolve(
      options.root ?? join(homedir(), ".local", "state", "orkastrator", "runs"),
    );
    this.#now = options.now ?? (() => new Date());
    this.#randomId = options.randomId ?? randomUUID;
    mkdirSync(requestedRoot, { recursive: true, mode: 0o700 });
    this.root = realpathSync(requestedRoot);
    this.#fsyncDirectory(this.root);
    this.#fsyncDirectory(dirname(this.root));
  }

  createRun(input: CreateRunInput): RunRecord {
    // Validate before even acquiring the filesystem-backed writer lock.
    parsePolicyV1(Buffer.from(input.policySnapshot, "utf8"));
    return this.#withWriterLock(() => this.#createRun(input));
  }

  #createRun(input: CreateRunInput): RunRecord {
    const objective = input.objective.trim();
    if (objective.length === 0 || objective.length > MAX_OBJECTIVE_CHARS) {
      throw new LedgerError(`objective must contain 1-${MAX_OBJECTIVE_CHARS} characters`);
    }
    if (input.supervisorSessionId.trim().length === 0) {
      throw new LedgerError("supervisor session ID is required");
    }
    if (!isAbsolute(input.repositoryRoot)) {
      throw new LedgerError("repository root must be absolute");
    }
    const policyBytes = Buffer.from(input.policySnapshot, "utf8");
    if (policyBytes.byteLength === 0 || policyBytes.byteLength > MAX_POLICY_BYTES) {
      throw new LedgerError(`policy snapshot must contain 1-${MAX_POLICY_BYTES} bytes`);
    }
    // The caller-supplied bytes are validated before allocating a run ID or touching a run directory.
    parsePolicyV1(policyBytes);
    const active = this.activeRunsForSession(input.supervisorSessionId);
    if (active.length > 0) {
      throw new ActiveRunError(
        `session ${input.supervisorSessionId} already owns active run ${active[0]?.runId}`,
      );
    }

    const runId = this.#randomId();
    const directory = this.runDirectory(runId);
    mkdirSync(directory, { mode: 0o700 });
    this.#assertSafeRunDirectory(directory);
    let eventAppendStarted = false;
    try {
      this.#fsyncDirectory(this.root);
      this.#writeDurable(join(directory, "policy.yaml"), input.policySnapshot);
      const timestamp = this.#timestamp();
      const record: RunRecord = {
        schemaVersion: 1,
        runId,
        objective,
        supervisorSessionId: input.supervisorSessionId,
        repositoryRoot: resolve(input.repositoryRoot),
        policyHash: createHash("sha256").update(input.policySnapshot).digest("hex"),
        policyFile: "policy.yaml",
        generation: 1,
        hostPid: input.hostPid ?? process.pid,
        sequence: 1,
        state: "submitted",
        reason: "run_created",
        createdAt: timestamp,
        updatedAt: timestamp,
        ownedProcesses: [],
        worktrees: [],
        policyCheckpoint: null,
      };
      const event: RunEvent = {
        schemaVersion: 1,
        eventId: this.#randomId(),
        runId,
        sequence: 1,
        timestamp,
        type: "run_created",
        toState: "submitted",
        ruleId: "run.create",
        actor: "owner",
        evidence: {
          repositoryRoot: record.repositoryRoot,
          policyHash: record.policyHash,
        },
        projection: record,
      };
      eventAppendStarted = true;
      this.#appendEvent(directory, event);
      this.#writeState(directory, record);
      return record;
    } catch (error) {
      if (!eventAppendStarted) {
        rmSync(directory, { recursive: true, force: true });
        this.#fsyncDirectory(this.root);
      }
      throw error;
    }
  }

  loadRun(runId: string): RunLoadResult {
    const directory = this.runDirectory(runId);
    this.#assertSafeRunDirectory(directory);
    const parsed = this.#readEvents(directory);
    if (parsed.droppedBytes > 0 && this.#lockDepth === 0) {
      return this.#withWriterLock(() => this.loadRun(runId));
    }
    const last = parsed.events.at(-1);
    if (last === undefined) throw new LedgerCorruptionError(`run ${runId} has no ledger events`);
    let record = last.projection;
    this.#assertRecord(record, runId);
    const policyBytes = this.#readHashVerifiedPolicy(directory, record);
    const checkpointCapable = Object.hasOwn(parsed.events[0]!.projection, "policyCheckpoint");
    if (checkpointCapable) {
      const policy = this.#parseFrozenPolicy(record, policyBytes);
      this.#validatePolicyEventChain(policy, parsed.events);
    } else {
      this.#validateLegacyPolicyFields(parsed.events);
    }

    const statePath = join(directory, "state.json");
    let stateNeedsRepair = true;
    if (existsSync(statePath)) {
      this.#assertRegularFile(statePath, "state projection");
      try {
        const projected = JSON.parse(readFileSync(statePath, "utf8")) as unknown;
        if (
          isObject(projected) &&
          typeof projected.sequence === "number" &&
          projected.sequence > record.sequence
        ) {
          throw new LedgerCorruptionError(
            `run ${runId} state sequence ${projected.sequence} exceeds event sequence ${record.sequence}`,
          );
        }
        this.#assertRecord(projected, runId);
        stateNeedsRepair = JSON.stringify(projected) !== JSON.stringify(record);
      } catch (error) {
        if (error instanceof LedgerCorruptionError && /exceeds event sequence/u.test(error.message)) {
          throw error;
        }
      }
    }
    if (parsed.droppedBytes === 0) {
      if (stateNeedsRepair && this.#lockDepth === 0) {
        return this.#withWriterLock(() => this.loadRun(runId));
      }
      if (stateNeedsRepair) this.#writeState(directory, record);
      return { record };
    }
    const previousSequence = record.sequence;
    const recovered = this.#nextEvent(record, record, {
      type: "ledger_tail_recovered",
      ruleId: "ledger.recover_truncated_tail",
      actor: "system",
      evidence: { droppedBytes: parsed.droppedBytes, previousSequence },
    });
    this.#replaceEventLog(directory, parsed.completeBytes, recovered.event);
    this.#writeState(directory, recovered.record);
    record = recovered.record;
    return {
      record,
      recovery: { droppedBytes: parsed.droppedBytes, previousSequence },
    };
  }

  events(runId: string): RunEvent[] {
    this.loadRun(runId);
    return this.#readEvents(this.runDirectory(runId)).events;
  }

  policySnapshot(runId: string): string {
    this.loadRun(runId);
    return readFileSync(join(this.runDirectory(runId), "policy.yaml"), "utf8");
  }

  applyPolicyEvent(runId: string, event: PolicyEvent): PolicyApplyResult {
    return this.#withWriterLock(() => {
      const current = this.loadRun(runId).record;
      if (!("policyCheckpoint" in current)) {
        throw new LedgerError(`legacy run ${runId} cannot apply policy events`);
      }
      if (TERMINAL_RUN_STATES.has(current.state)) {
        throw new LedgerError(`terminal run ${runId} cannot apply policy events`);
      }
      this.#assertBoundedPolicyEvent(event);

      const events = this.#readEvents(this.runDirectory(runId)).events;
      const revision = current.policyCheckpoint?.revision ?? 0;
      if (event.sequence <= revision) {
        if (event.sequence < revision) {
          throw new LedgerError(`policy event sequence ${event.sequence} is stale at revision ${revision}`);
        }
        const applied = [...events].reverse().find((candidate) =>
          candidate.type === "policy_event_applied"
          && isObject(candidate.evidence.policyEvent)
          && candidate.evidence.policyEvent.sequence === revision
        );
        if (applied === undefined || !isDeepStrictEqual(applied.evidence.policyEvent, event)) {
          throw new LedgerError(`policy event sequence ${event.sequence} conflicts with the persisted event`);
        }
        const reduction = this.#persistedReduction(applied);
        const delivery = current.pendingPolicyAction?.actionId === reduction.action.actionId
          ? "pending" as const
          : "delivered" as const;
        return { record: current, reduction, delivery, appended: false };
      }
      if (current.pendingPolicyAction !== undefined) {
        throw new LedgerError(
          `policy action ${current.pendingPolicyAction.actionId} must be delivered before another event`,
        );
      }

      const policy = this.#parseFrozenPolicy(
        current,
        this.#readHashVerifiedPolicy(this.runDirectory(runId), current),
      );
      let reduction: PolicyReduction;
      try {
        reduction = reducePolicy({ policy, checkpoint: current.policyCheckpoint ?? null, event });
      } catch (error) {
        throw new LedgerError(error instanceof Error ? error.message : String(error));
      }
      const evidence = {
        policyEvent: cloneJson(event),
        occurrenceId: reduction.occurrenceId,
        action: cloneJson(reduction.action),
        trace: cloneJson(reduction.trace),
      } as JsonObject;
      assertBoundedJson(evidence, MAX_POLICY_EVENT_EVIDENCE_BYTES, "policy event evidence");

      // Append+fsync is the outbox commit point. Dispatch must happen only after this returns.
      const record = this.#commit(
        current,
        {
          ...current,
          policyCheckpoint: structuredClone(reduction.checkpoint),
          pendingPolicyAction: structuredClone(reduction.action),
        },
        {
          type: "policy_event_applied",
          ruleId: reduction.ruleId,
          actor: "extension",
          evidence,
        },
      );
      return { record, reduction, delivery: "pending", appended: true };
    });
  }

  recordPolicyActionDelivered(
    runId: string,
    actionId: string,
    evidence: PolicyActionDeliveryEvidence,
  ): RunRecord {
    return this.#withWriterLock(() => {
      assertPolicyDeliveryEvidence(actionId, evidence);
      const current = this.loadRun(runId).record;
      const events = this.#readEvents(this.runDirectory(runId)).events;
      const priorDelivery = [...events].reverse().find((event) =>
        event.type === "policy_action_delivered" && event.actionId === actionId
      );
      const pending = current.pendingPolicyAction;
      if (pending === undefined) {
        if (
          priorDelivery !== undefined
          && priorDelivery.policyRevision === current.policyCheckpoint?.revision
        ) {
          if (!isDeepStrictEqual(priorDelivery.evidence, evidence)) {
            throw new LedgerError(`policy action ${actionId} delivery receipt conflicts`);
          }
          return current;
        }
        if (priorDelivery !== undefined) {
          throw new LedgerError(`policy action ${actionId} delivery is stale`);
        }
        throw new LedgerError(`policy action ${actionId} is unknown or is not pending`);
      }
      if (pending.actionId !== actionId) {
        if (priorDelivery !== undefined) throw new LedgerError(`policy action ${actionId} delivery is stale`);
        throw new LedgerError(`policy action ${actionId} is not the pending action`);
      }
      if (current.policyCheckpoint === undefined || current.policyCheckpoint === null) {
        throw new LedgerCorruptionError(`pending policy action ${actionId} has no checkpoint`);
      }

      const { pendingPolicyAction: _pending, ...withoutPending } = current;
      return this.#commit(current, withoutPending, {
        type: "policy_action_delivered",
        ruleId: "policy.action_delivered",
        actor: "extension",
        evidence: cloneJson(evidence) as unknown as JsonObject,
        actionId,
        policyRevision: current.policyCheckpoint.revision,
        actionType: pending.type,
        delivery: "delivered",
      });
    });
  }

  scanNonterminal(): RunRecord[] {
    const records: RunRecord[] = [];
    for (const entry of readdirSync(this.root, { withFileTypes: true }).sort((a, b) =>
      a.name.localeCompare(b.name),
    )) {
      if (!RUN_ID_PATTERN.test(entry.name)) continue;
      if (!entry.isDirectory()) {
        throw new LedgerCorruptionError(`run entry is not a real directory: ${entry.name}`);
      }
      const eventsPath = join(this.root, entry.name, "events.jsonl");
      if (!existsSync(eventsPath)) {
        throw new LedgerCorruptionError(`run ${entry.name} has no event ledger`);
      }
      const record = this.loadRun(entry.name).record;
      if (!TERMINAL_RUN_STATES.has(record.state)) records.push(record);
    }
    return records;
  }

  activeRunsForSession(sessionId: string): RunRecord[] {
    return this.scanNonterminal().filter((record) => record.supervisorSessionId === sessionId);
  }

  transition(runId: string, input: TransitionInput): RunRecord {
    return this.#withWriterLock(() => this.#transition(runId, input));
  }

  #transition(runId: string, input: TransitionInput): RunRecord {
    const current = this.loadRun(runId).record;
    if (TERMINAL_RUN_STATES.has(current.state)) {
      throw new LedgerError(`terminal run ${runId} cannot transition from ${current.state}`);
    }
    const { reload: _reload, ...withoutReload } = current;
    const next: RunRecord = TERMINAL_RUN_STATES.has(input.state)
      ? { ...withoutReload, state: input.state, reason: input.reason }
      : { ...current, state: input.state, reason: input.reason };
    return this.#commit(current, next, {
      type: "run_state_transitioned",
      fromState: current.state,
      toState: input.state,
      ruleId: input.ruleId,
      actor: input.actor,
      ...(input.evidence === undefined ? {} : { evidence: input.evidence }),
    });
  }

  recordOwnership(runId: string, input: OwnershipInput): RunRecord {
    return this.#withWriterLock(() => this.#recordOwnership(runId, input));
  }

  journalOwnedProcess(
    runId: string,
    attemptToken: string,
    identity?: OwnedProcessIdentity,
    exitEvidence?: OwnedProcessExitEvidence,
  ): RunRecord {
    return this.#withWriterLock(() => {
      const current = this.loadRun(runId).record;
      if (TERMINAL_RUN_STATES.has(current.state)) {
        throw new LedgerError(`terminal run ${runId} cannot change process ownership`);
      }
      const existing = current.ownedProcesses.find(
        (processIdentity) => processIdentity.attemptToken === attemptToken,
      );
      if (identity !== undefined) {
        if (exitEvidence !== undefined) {
          throw new LedgerError("exit evidence is valid only when clearing process ownership");
        }
        if (identity.attemptToken !== attemptToken) throw new LedgerError("attempt token mismatch");
        this.#assertOwnedProcess(identity);
        if (existing !== undefined) {
          if (!sameOwnedProcessIdentity(existing, identity)) {
            throw new LedgerError(`attempt ${attemptToken} already has different ownership`);
          }
          return current;
        }
      } else if (existing === undefined) {
        return current;
      }
      if (exitEvidence !== undefined) this.#assertExitEvidence(exitEvidence);
      const ownedProcesses = identity === undefined
        ? current.ownedProcesses.filter((item) => item.attemptToken !== attemptToken)
        : [...current.ownedProcesses, { ...identity }];
      return this.#commit(current, { ...current, ownedProcesses }, {
        type: identity === undefined ? "owned_process_cleared" : "owned_process_bound",
        ruleId: "worker.process_ownership",
        actor: "extension",
        evidence: identity === undefined
          ? {
              identity: { ...existing! },
              processGroupAbsent: true,
              ...(exitEvidence === undefined ? {} : exitEvidence),
            }
          : { identity: { ...identity } },
      });
    });
  }

  appendWorkerEvent(runId: string, event: WorkerEvent): RunRecord {
    return this.#withWriterLock(() => {
      const current = this.loadRun(runId).record;
      if (TERMINAL_RUN_STATES.has(current.state)) {
        throw new LedgerError(`terminal run ${runId} cannot record worker events`);
      }
      const evidence = this.#workerEventEvidence(event);
      return this.#commit(current, current, {
        type: "worker_event",
        ruleId: "worker.event_observed",
        actor: "worker",
        evidence,
      });
    });
  }

  #recordOwnership(runId: string, input: OwnershipInput): RunRecord {
    const current = this.loadRun(runId).record;
    if (TERMINAL_RUN_STATES.has(current.state)) {
      throw new LedgerError(`terminal run ${runId} cannot bind ownership`);
    }
    for (const processIdentity of input.ownedProcesses) {
      this.#assertOwnedProcess(processIdentity);
    }
    for (const worktree of input.worktrees) {
      if (
        !isAbsolute(worktree.repositoryRoot) ||
        !isAbsolute(worktree.path) ||
        worktree.branch.length === 0 ||
        !/^[0-9a-f]{40}$/u.test(worktree.headSha)
      ) {
        throw new LedgerError("worktree identity is incomplete");
      }
    }
    return this.#commit(
      current,
      {
        ...current,
        ownedProcesses: input.ownedProcesses.map((identity) => ({ ...identity })),
        worktrees: input.worktrees.map((identity) => ({ ...identity })),
      },
      {
        type: "run_ownership_recorded",
        ruleId: "lifecycle.record_ownership",
        actor: "extension",
        evidence: {
          ownedProcessCount: input.ownedProcesses.length,
          worktreeCount: input.worktrees.length,
        },
      },
    );
  }

  prepareReload(
    runId: string,
    sessionId: string,
    repositoryRoot: string,
    hostPid = process.pid,
  ): RunRecord {
    return this.#withWriterLock(() =>
      this.#prepareReload(runId, sessionId, repositoryRoot, hostPid),
    );
  }

  #prepareReload(
    runId: string,
    sessionId: string,
    repositoryRoot: string,
    hostPid: number,
  ): RunRecord {
    const current = this.loadRun(runId).record;
    this.#assertOwnedBy(current, sessionId);
    if (TERMINAL_RUN_STATES.has(current.state)) return current;
    if (resolve(repositoryRoot) !== current.repositoryRoot) {
      throw new LedgerError(`run ${runId} repository identity changed before reload`);
    }
    if (
      current.reload?.sessionId === sessionId &&
      current.reload.generation === current.generation &&
      current.reload.hostPid === hostPid &&
      current.reload.repositoryRoot === current.repositoryRoot
    ) {
      return current;
    }
    const marker = {
      sessionId,
      generation: current.generation,
      hostPid,
      repositoryRoot: current.repositoryRoot,
      requestedAt: this.#timestamp(),
    };
    return this.#commit(
      current,
      { ...current, reason: "reload_pending", reload: marker },
      {
        type: "session_reload_pending",
        ruleId: "lifecycle.reload_prepare",
        actor: "extension",
        evidence: {
          sessionId,
          generation: current.generation,
          hostPid,
          repositoryRoot: current.repositoryRoot,
        },
      },
    );
  }

  rebind(
    runId: string,
    sessionId: string,
    repositoryRoot: string,
    expectedSequence: number,
    expectedOwnershipFingerprint: string,
    hostPid = process.pid,
  ): RunRecord {
    return this.#withWriterLock(() =>
      this.#rebind(
        runId,
        sessionId,
        repositoryRoot,
        expectedSequence,
        expectedOwnershipFingerprint,
        hostPid,
      ),
    );
  }

  #rebind(
    runId: string,
    sessionId: string,
    repositoryRoot: string,
    expectedSequence: number,
    expectedOwnershipFingerprint: string,
    hostPid: number,
  ): RunRecord {
    const current = this.loadRun(runId).record;
    this.#assertOwnedBy(current, sessionId);
    const marker = current.reload;
    if (
      current.sequence !== expectedSequence ||
      ownershipFingerprint(current) !== expectedOwnershipFingerprint
    ) {
      throw new RebindConflictError(`run ${runId} identity changed after reload verification`);
    }
    if (
      marker === undefined ||
      marker.sessionId !== sessionId ||
      marker.generation !== current.generation ||
      marker.hostPid !== hostPid ||
      current.hostPid !== hostPid ||
      marker.repositoryRoot !== current.repositoryRoot ||
      resolve(repositoryRoot) !== current.repositoryRoot
    ) {
      throw new LedgerError(`run ${runId} does not have a continuous reload proof`);
    }
    const { reload: _reload, ...withoutReload } = current;
    const next: RunRecord = {
      ...withoutReload,
      generation: current.generation + 1,
      hostPid,
      reason: "reload_rebound",
    };
    return this.#commit(current, next, {
      type: "session_reload_rebound",
      ruleId: "lifecycle.reload_rebind",
      actor: "extension",
      evidence: {
        sessionId,
        previousGeneration: current.generation,
        generation: next.generation,
        hostPid,
        repositoryRoot: current.repositoryRoot,
      },
    });
  }

  beginAwaitingOwner(runId: string, input: AwaitOwnerInput): RunRecord {
    return this.#withWriterLock(() => this.#beginAwaitingOwner(runId, input));
  }

  #beginAwaitingOwner(runId: string, input: AwaitOwnerInput): RunRecord {
    const current = this.loadRun(runId).record;
    if (TERMINAL_RUN_STATES.has(current.state) || current.state === "awaiting_owner") {
      throw new LedgerError(`run ${runId} cannot wait for owner from ${current.state}`);
    }
    if (input.resumeState !== current.state) {
      throw new LedgerError("owner wait resume state must equal the current run state");
    }
    const allowedDecisions = [...new Set(input.allowedDecisions.map((item) => item.trim()))].filter(
      Boolean,
    );
    if (allowedDecisions.length === 0) throw new LedgerError("owner wait requires a decision set");
    const ownerWait = {
      ruleId: input.ruleId,
      evidence: input.evidence,
      allowedDecisions,
      startedAt: this.#timestamp(),
      resumeState: input.resumeState,
    };
    return this.#commit(
      current,
      {
        ...current,
        state: "awaiting_owner",
        reason: input.ruleId,
        ownerWait,
      },
      {
        type: "owner_wait_started",
        fromState: current.state,
        toState: "awaiting_owner",
        ruleId: input.ruleId,
        actor: "supervisor",
        evidence: {
          ...input.evidence,
          allowedDecisions,
          resumeState: input.resumeState,
        },
      },
    );
  }

  answerOwner(runId: string, input: OwnerAnswerInput): RunRecord {
    return this.#withWriterLock(() => this.#answerOwner(runId, input));
  }

  #answerOwner(runId: string, input: OwnerAnswerInput): RunRecord {
    if (input.rationale.trim().length === 0) {
      throw new LedgerError("owner answer rationale is required");
    }
    const current = this.loadRun(runId).record;
    const wait = current.ownerWait;
    if (current.state !== "awaiting_owner" || wait === undefined) {
      throw new LedgerError(`run ${runId} is not awaiting an owner answer`);
    }
    if (!wait.allowedDecisions.includes(input.decision)) {
      throw new LedgerError(`owner decision ${input.decision} is not allowed`);
    }
    const answeredAt = this.#timestamp();
    const ownerWait = {
      ...wait,
      response: {
        decision: input.decision,
        rationale: input.rationale,
        answeredAt,
      },
    };
    return this.#commit(
      current,
      {
        ...current,
        state: wait.resumeState,
        reason: "owner_answered",
        ownerWait,
      },
      {
        type: "owner_answered",
        fromState: "awaiting_owner",
        toState: wait.resumeState,
        ruleId: wait.ruleId,
        actor: "owner",
        evidence: {
          decision: input.decision,
          rationale: input.rationale,
          waitStartedAt: wait.startedAt,
          answeredAt,
        },
      },
    );
  }

  #assertOwnedProcess(processIdentity: OwnedProcessIdentity): void {
    if (
      !Number.isInteger(processIdentity.pid) ||
      processIdentity.pid < 2 ||
      !Number.isInteger(processIdentity.processGroupId) ||
      processIdentity.processGroupId < 2 ||
      !isAbsolute(processIdentity.sessionFile) ||
      processIdentity.attemptToken.length === 0
    ) {
      throw new LedgerError("owned process identity is incomplete");
    }
  }

  #assertExitEvidence(evidence: OwnedProcessExitEvidence): void {
    if (!isOwnedProcessExitEvidence(evidence)) {
      throw new LedgerError("owned process exit evidence is invalid");
    }
  }

  #workerEventEvidence(event: WorkerEvent): JsonObject {
    assertWorkerEvent(event);
    const serialized = JSON.stringify(event);
    if (Buffer.byteLength(serialized, "utf8") > MAX_WORKER_EVENT_BYTES) {
      throw new LedgerError(`worker event exceeds ${MAX_WORKER_EVENT_BYTES} bytes`);
    }
    return { event: JSON.parse(serialized) as JsonObject };
  }

  #readHashVerifiedPolicy(directory: string, record: RunRecord): Buffer {
    const policyPath = join(directory, record.policyFile);
    this.#assertRegularFile(policyPath, "policy snapshot");
    const policyBytes = readFileSync(policyPath);
    const policyHash = createHash("sha256").update(policyBytes).digest("hex");
    if (policyHash !== record.policyHash) {
      throw new LedgerCorruptionError(`run ${record.runId} policy snapshot hash does not match`);
    }
    return policyBytes;
  }

  #parseFrozenPolicy(record: RunRecord, policyBytes: Buffer): PolicyV1 {
    try {
      return parsePolicyV1(policyBytes);
    } catch (error) {
      throw new LedgerCorruptionError(
        `run ${record.runId} frozen policy snapshot is invalid: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  #validateLegacyPolicyFields(events: RunEvent[]): void {
    for (const [index, event] of events.entries()) {
      if (
        Object.hasOwn(event.projection, "policyCheckpoint")
        || Object.hasOwn(event.projection, "pendingPolicyAction")
        || event.type === "policy_event_applied"
        || event.type === "policy_action_delivered"
      ) {
        throw new LedgerCorruptionError(
          `legacy event ${index + 1} contains checkpoint-capable policy state`,
        );
      }
    }
  }

  #assertBoundedPolicyEvent(event: PolicyEvent): void {
    assertBoundedJson(event, MAX_POLICY_EVENT_EVIDENCE_BYTES, "policy event");
  }

  #persistedReduction(event: RunEvent): PolicyReduction {
    return {
      checkpoint: structuredClone(event.projection.policyCheckpoint) as PolicyCheckpoint,
      action: structuredClone(event.evidence.action) as PolicyAction,
      ruleId: event.ruleId,
      occurrenceId: String(event.evidence.occurrenceId),
      trace: structuredClone(event.evidence.trace) as string[],
    };
  }

  #validatePolicyEventChain(policy: PolicyV1, events: RunEvent[]): void {
    for (const [index, event] of events.entries()) {
      const preceding = events[index - 1]?.projection;
      if (preceding === undefined) {
        if (
          event.type !== "run_created"
          || event.projection.pendingPolicyAction !== undefined
          || (("policyCheckpoint" in event.projection) && event.projection.policyCheckpoint !== null)
        ) {
          throw new LedgerCorruptionError("initial run event has an invalid policy projection");
        }
        continue;
      }

      if (event.type === "policy_event_applied") {
        if (
          !("policyCheckpoint" in preceding)
          || preceding.policyCheckpoint === undefined
          || preceding.pendingPolicyAction !== undefined
          || TERMINAL_RUN_STATES.has(preceding.state)
          || event.actor !== "extension"
          || !hasExactKeys(event, STATELESS_EVENT_KEYS)
          || !hasExactKeys(event.evidence, ["policyEvent", "occurrenceId", "action", "trace"])
        ) {
          throw new LedgerCorruptionError(`event ${index + 1} has an invalid policy apply envelope`);
        }
        assertBoundedJson(
          event.evidence,
          MAX_POLICY_EVENT_EVIDENCE_BYTES,
          `event ${index + 1} policy evidence`,
          LedgerCorruptionError,
        );
        let reduction: PolicyReduction;
        try {
          reduction = reducePolicy({
            policy,
            checkpoint: preceding.policyCheckpoint,
            event: event.evidence.policyEvent as PolicyEvent,
          });
        } catch (error) {
          throw new LedgerCorruptionError(
            `event ${index + 1} policy reduction is invalid: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
        if (
          event.ruleId !== reduction.ruleId
          || !isDeepStrictEqual(event.evidence.occurrenceId, reduction.occurrenceId)
          || !isDeepStrictEqual(event.evidence.action, reduction.action)
          || !isDeepStrictEqual(event.evidence.trace, reduction.trace)
        ) {
          throw new LedgerCorruptionError(`event ${index + 1} persisted policy reduction differs from replay`);
        }
        const expected: RunRecord = {
          ...preceding,
          sequence: event.sequence,
          updatedAt: event.timestamp,
          policyCheckpoint: reduction.checkpoint,
          pendingPolicyAction: reduction.action,
        };
        if (!isDeepStrictEqual(event.projection, expected)) {
          throw new LedgerCorruptionError(`event ${index + 1} policy projection differs from replay`);
        }
        continue;
      }

      if (event.type === "policy_action_delivered") {
        const pending = preceding.pendingPolicyAction;
        if (
          pending === undefined
          || preceding.policyCheckpoint === undefined
          || preceding.policyCheckpoint === null
          || event.actor !== "extension"
          || event.ruleId !== "policy.action_delivered"
          || !hasExactKeys(event, POLICY_DELIVERY_EVENT_KEYS)
          || event.actionId !== pending.actionId
          || event.policyRevision !== preceding.policyCheckpoint.revision
          || event.actionType !== pending.type
          || event.delivery !== "delivered"
        ) {
          throw new LedgerCorruptionError(`event ${index + 1} has invalid policy delivery metadata`);
        }
        try {
          assertPolicyDeliveryEvidence(pending.actionId, event.evidence);
        } catch (error) {
          throw new LedgerCorruptionError(
            `event ${index + 1} has invalid policy delivery evidence: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
        const { pendingPolicyAction: _pending, ...withoutPending } = preceding;
        const expected: RunRecord = {
          ...withoutPending,
          sequence: event.sequence,
          updatedAt: event.timestamp,
        };
        if (!isDeepStrictEqual(event.projection, expected)) {
          throw new LedgerCorruptionError(`event ${index + 1} policy delivery changed unrelated state`);
        }
        continue;
      }

      if (!sameOptionalField(preceding, event.projection, "policyCheckpoint")
        || !sameOptionalField(preceding, event.projection, "pendingPolicyAction")) {
        throw new LedgerCorruptionError(`event ${index + 1} changed policy state outside a policy event`);
      }
    }
  }

  runDirectory(runId: string): string {
    if (!RUN_ID_PATTERN.test(runId)) {
      throw new LedgerError(`invalid run ID: ${runId}`);
    }
    const directory = resolve(this.root, runId);
    if (dirname(directory) !== this.root) throw new LedgerError(`run ID escapes ledger root: ${runId}`);
    if (existsSync(directory)) this.#assertSafeRunDirectory(directory);
    return directory;
  }

  #commit(current: RunRecord, candidate: RunRecord, input: EventInput): RunRecord {
    const next = this.#nextEvent(current, candidate, input);
    const directory = this.runDirectory(current.runId);
    this.#appendEvent(directory, next.event);
    this.#writeState(directory, next.record);
    return next.record;
  }

  #nextEvent(
    current: RunRecord,
    candidate: RunRecord,
    input: EventInput,
  ): { record: RunRecord; event: RunEvent } {
    const timestamp = this.#timestamp();
    const record: RunRecord = {
      ...candidate,
      sequence: current.sequence + 1,
      updatedAt: timestamp,
    };
    const event: RunEvent = {
      schemaVersion: 1,
      eventId: this.#randomId(),
      runId: current.runId,
      sequence: record.sequence,
      timestamp,
      type: input.type,
      ruleId: input.ruleId,
      actor: input.actor,
      evidence: input.evidence ?? {},
      projection: record,
      ...(input.fromState === undefined ? {} : { fromState: input.fromState }),
      ...(input.toState === undefined ? {} : { toState: input.toState }),
      ...(input.actionId === undefined ? {} : { actionId: input.actionId }),
      ...(input.policyRevision === undefined ? {} : { policyRevision: input.policyRevision }),
      ...(input.actionType === undefined ? {} : { actionType: input.actionType }),
      ...(input.delivery === undefined ? {} : { delivery: input.delivery }),
    };
    return { record, event };
  }

  #readEvents(directory: string): ParsedEvents {
    const path = join(directory, "events.jsonl");
    this.#assertRegularFile(path, "event ledger");
    const bytes = readFileSync(path);
    const finalLf = bytes.lastIndexOf(0x0a);
    const retainedBytes = finalLf + 1;
    const droppedBytes = bytes.length - retainedBytes;
    const completeBytes = bytes.subarray(0, retainedBytes);
    const complete = completeBytes.toString("utf8");
    const events: RunEvent[] = [];
    const expectedRunId = basename(directory);
    for (const [index, line] of complete.split("\n").entries()) {
      if (line.length === 0) continue;
      try {
        const event = JSON.parse(line) as unknown;
        this.#assertEvent(event, index + 1, events.at(-1)?.projection);
        if (event.runId !== expectedRunId) {
          throw new LedgerCorruptionError(`event ${index + 1} belongs to another run`);
        }
        if (event.sequence !== events.length + 1) {
          throw new LedgerCorruptionError(`event ${index + 1} breaks sequence continuity`);
        }
        events.push(event);
      } catch (error) {
        throw new LedgerCorruptionError(
          `invalid ledger event at line ${index + 1}: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
    return { events, droppedBytes, completeBytes };
  }

  #appendEvent(directory: string, event: RunEvent): void {
    const path = join(directory, "events.jsonl");
    if (existsSync(path)) this.#assertRegularFile(path, "event ledger");
    const descriptor = openSync(
      path,
      constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW,
      0o600,
    );
    try {
      this.#writeAll(descriptor, Buffer.from(`${JSON.stringify(event)}\n`, "utf8"));
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
  }

  #replaceEventLog(directory: string, completeBytes: Buffer, recovery: RunEvent): void {
    const path = join(directory, "events.jsonl");
    const temporary = `${path}.${this.#randomId()}.tmp`;
    const descriptor = openSync(
      temporary,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
      0o600,
    );
    let renamed = false;
    try {
      this.#writeAll(descriptor, completeBytes);
      this.#writeAll(descriptor, Buffer.from(`${JSON.stringify(recovery)}\n`, "utf8"));
      fsyncSync(descriptor);
      closeSync(descriptor);
      renameSync(temporary, path);
      renamed = true;
      this.#fsyncDirectory(directory);
    } finally {
      try {
        closeSync(descriptor);
      } catch {
        // The descriptor was already closed before the atomic rename.
      }
      if (!renamed) rmSync(temporary, { force: true });
    }
  }

  #writeAll(descriptor: number, bytes: Buffer): void {
    let offset = 0;
    while (offset < bytes.length) {
      const written = writeSync(descriptor, bytes, offset, bytes.length - offset, null);
      if (written <= 0) throw new LedgerError("filesystem made no progress writing ledger bytes");
      offset += written;
    }
  }

  #writeState(directory: string, record: RunRecord): void {
    this.#writeDurable(join(directory, "state.json"), `${JSON.stringify(record, null, 2)}\n`);
  }

  #writeDurable(path: string, contents: string): void {
    const temporary = `${path}.${this.#randomId()}.tmp`;
    const descriptor = openSync(
      temporary,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
      0o600,
    );
    let renamed = false;
    try {
      writeFileSync(descriptor, contents, "utf8");
      fsyncSync(descriptor);
      closeSync(descriptor);
      renameSync(temporary, path);
      renamed = true;
      this.#fsyncDirectory(dirname(path));
    } finally {
      try {
        closeSync(descriptor);
      } catch {
        // The descriptor was already closed before the atomic rename.
      }
      if (!renamed) rmSync(temporary, { force: true });
    }
  }

  #fsyncDirectory(path: string): void {
    const descriptor = openSync(path, constants.O_RDONLY);
    try {
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
  }

  #withWriterLock<T>(operation: () => T): T {
    if (this.#lockDepth > 0) return operation();

    const lockPath = join(this.root, ".writer-lock");
    const existed = existsSync(lockPath);
    const descriptor = openSync(
      lockPath,
      constants.O_RDWR | constants.O_CREAT | constants.O_NOFOLLOW,
      0o600,
    );
    if (!existed) this.#fsyncDirectory(this.root);
    const acquired = spawnSync("flock", ["--exclusive", "3"], {
      stdio: ["ignore", "pipe", "pipe", descriptor],
    });
    if (acquired.error !== undefined) {
      closeSync(descriptor);
      throw new LedgerError(`failed to invoke flock: ${acquired.error.message}`);
    }
    if (acquired.status !== 0) {
      closeSync(descriptor);
      throw new LedgerError(
        `failed to lock the Orkastrator ledger: ${acquired.stderr.toString("utf8").trim()}`,
      );
    }

    this.#lockDepth = 1;
    try {
      return operation();
    } finally {
      this.#lockDepth = 0;
      closeSync(descriptor);
    }
  }

  #timestamp(): string {
    return this.#now().toISOString();
  }

  #assertSafeRunDirectory(directory: string): void {
    const metadata = lstatSync(directory);
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
      throw new LedgerCorruptionError(`run path is not a real directory: ${directory}`);
    }
    const canonical = realpathSync(directory);
    if (dirname(canonical) !== this.root) {
      throw new LedgerCorruptionError(`run directory escapes ledger root: ${directory}`);
    }
  }

  #assertRegularFile(path: string, label: string): void {
    const metadata = lstatSync(path);
    if (!metadata.isFile() || metadata.isSymbolicLink()) {
      throw new LedgerCorruptionError(`${label} is not a regular file: ${path}`);
    }
    if (dirname(realpathSync(path)) !== dirname(path)) {
      throw new LedgerCorruptionError(`${label} escapes its run directory: ${path}`);
    }
  }

  #assertOwnedBy(record: RunRecord, sessionId: string): void {
    if (record.supervisorSessionId !== sessionId) {
      throw new LedgerError(`run ${record.runId} is owned by another supervisor session`);
    }
  }

  #assertEvent(
    value: unknown,
    line: number,
    precedingProjection?: RunRecord,
  ): asserts value is RunEvent {
    if (!isObject(value)) throw new LedgerCorruptionError(`event ${line} is not an object`);
    if (
      value.schemaVersion !== 1 ||
      typeof value.eventId !== "string" ||
      !RUN_ID_PATTERN.test(value.eventId) ||
      typeof value.runId !== "string" ||
      typeof value.timestamp !== "string" ||
      !isIsoTimestamp(value.timestamp) ||
      typeof value.type !== "string" ||
      value.type.length === 0 ||
      typeof value.ruleId !== "string" ||
      value.ruleId.length === 0 ||
      typeof value.actor !== "string" ||
      !RUN_ACTOR_SET.has(value.actor) ||
      !isObject(value.evidence) ||
      (value.fromState !== undefined &&
        (typeof value.fromState !== "string" || !RUN_STATE_SET.has(value.fromState))) ||
      (value.toState !== undefined &&
        (typeof value.toState !== "string" || !RUN_STATE_SET.has(value.toState)))
    ) {
      throw new LedgerCorruptionError(`event ${line} has an unsupported schema`);
    }
    if (!Number.isInteger(value.sequence) || !isObject(value.projection)) {
      throw new LedgerCorruptionError(`event ${line} has no valid projection`);
    }
    this.#assertRecord(value.projection, String(value.runId));
    if (
      value.projection.sequence !== value.sequence ||
      value.projection.updatedAt !== value.timestamp
    ) {
      throw new LedgerCorruptionError(`event ${line} projection metadata does not match`);
    }
    if (value.type === "worker_event") {
      if (
        value.ruleId !== "worker.event_observed" ||
        value.actor !== "worker" ||
        !hasExactKeys(value, STATELESS_EVENT_KEYS) ||
        !hasExactKeys(value.evidence, ["event"])
      ) {
        throw new LedgerCorruptionError(`event ${line} has an invalid worker event envelope`);
      }
      assertWorkerEvent(value.evidence.event);
      const serialized = JSON.stringify(value.evidence.event);
      if (Buffer.byteLength(serialized, "utf8") > MAX_WORKER_EVENT_BYTES) {
        throw new LedgerCorruptionError(`event ${line} worker event exceeds the byte limit`);
      }
      if (
        precedingProjection === undefined ||
        !isDeepStrictEqual(value.projection, {
          ...precedingProjection,
          sequence: value.sequence,
          updatedAt: value.timestamp,
        })
      ) {
        throw new LedgerCorruptionError(`event ${line} worker event changed its run projection`);
      }
    }
    if (value.type === "owned_process_cleared") {
      const hasBaseEvidence = hasExactKeys(value.evidence, [
        "identity",
        "processGroupAbsent",
      ]);
      const hasExitEvidence = hasExactKeys(value.evidence, [
        "identity",
        "processGroupAbsent",
        "exitCode",
        "exitSignal",
      ]);
      if (
        value.ruleId !== "worker.process_ownership" ||
        value.actor !== "extension" ||
        !hasExactKeys(value, STATELESS_EVENT_KEYS) ||
        (!hasBaseEvidence && !hasExitEvidence) ||
        !isExactOwnedProcessIdentity(value.evidence.identity) ||
        value.evidence.processGroupAbsent !== true ||
        (hasExitEvidence &&
          !isOwnedProcessExitEvidence({
            exitCode: value.evidence.exitCode,
            exitSignal: value.evidence.exitSignal,
          }))
      ) {
        throw new LedgerCorruptionError(`event ${line} has invalid process clear evidence`);
      }
      const identity = value.evidence.identity;
      const existed = precedingProjection?.ownedProcesses.some((candidate) =>
        sameOwnedProcessIdentity(candidate, identity)
      ) ?? false;
      const remains = value.projection.ownedProcesses.some(
        (candidate) => candidate.attemptToken === identity.attemptToken,
      );
      if (!existed || remains || precedingProjection === undefined) {
        throw new LedgerCorruptionError(`event ${line} process clear projection is inconsistent`);
      }
      if (!isDeepStrictEqual(value.projection, {
        ...precedingProjection,
        sequence: value.sequence,
        updatedAt: value.timestamp,
        ownedProcesses: precedingProjection.ownedProcesses.filter(
          (candidate) => candidate.attemptToken !== identity.attemptToken,
        ),
      })) {
        throw new LedgerCorruptionError(`event ${line} process clear changed unrelated run state`);
      }
    }
  }

  #assertRecord(value: unknown, expectedRunId: string): asserts value is RunRecord {
    if (!isObject(value)) throw new LedgerCorruptionError("run state is not an object");
    if (
      value.schemaVersion !== 1 ||
      value.runId !== expectedRunId ||
      typeof value.objective !== "string" ||
      value.objective.trim().length === 0 ||
      value.objective.length > MAX_OBJECTIVE_CHARS ||
      typeof value.supervisorSessionId !== "string" ||
      value.supervisorSessionId.trim().length === 0 ||
      typeof value.repositoryRoot !== "string" ||
      !isAbsolute(value.repositoryRoot) ||
      typeof value.policyHash !== "string" ||
      !/^[0-9a-f]{64}$/u.test(value.policyHash) ||
      value.policyFile !== "policy.yaml" ||
      typeof value.generation !== "number" ||
      !Number.isInteger(value.generation) ||
      value.generation < 1 ||
      typeof value.hostPid !== "number" ||
      !Number.isInteger(value.hostPid) ||
      value.hostPid < 2 ||
      typeof value.sequence !== "number" ||
      !Number.isInteger(value.sequence) ||
      value.sequence < 1 ||
      !Array.isArray(value.ownedProcesses) ||
      !Array.isArray(value.worktrees) ||
      typeof value.state !== "string" ||
      !RUN_STATE_SET.has(value.state) ||
      typeof value.reason !== "string" ||
      value.reason.length === 0 ||
      typeof value.createdAt !== "string" ||
      !isIsoTimestamp(value.createdAt) ||
      typeof value.updatedAt !== "string" ||
      !isIsoTimestamp(value.updatedAt)
    ) {
      throw new LedgerCorruptionError(`run ${expectedRunId} has invalid projected state`);
    }
    if (
      !value.ownedProcesses.every(isOwnedProcessIdentity) ||
      !value.worktrees.every(isWorktreeIdentity) ||
      (value.reload !== undefined &&
        !isReloadMarker(
          value.reload,
          value.supervisorSessionId,
          value.generation,
          value.hostPid,
          value.repositoryRoot,
        )) ||
      (value.ownerWait !== undefined && !isOwnerWait(value.ownerWait)) ||
      (value.policyCheckpoint !== undefined
        && value.policyCheckpoint !== null
        && !isObject(value.policyCheckpoint)) ||
      (value.pendingPolicyAction !== undefined && !isObject(value.pendingPolicyAction)) ||
      (value.state === "awaiting_owner" &&
        (value.ownerWait === undefined ||
          (isObject(value.ownerWait) && value.ownerWait.response !== undefined)))
    ) {
      throw new LedgerCorruptionError(`run ${expectedRunId} has invalid nested identity evidence`);
    }
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sameOwnedProcessIdentity(
  left: OwnedProcessIdentity,
  right: OwnedProcessIdentity,
): boolean {
  return (
    left.pid === right.pid &&
    left.processGroupId === right.processGroupId &&
    left.sessionFile === right.sessionFile &&
    left.attemptToken === right.attemptToken
  );
}

function isExactOwnedProcessIdentity(value: unknown): value is OwnedProcessIdentity {
  return isObject(value) &&
    hasExactKeys(value, ["pid", "processGroupId", "sessionFile", "attemptToken"]) &&
    isOwnedProcessIdentity(value);
}

function isOwnedProcessIdentity(value: unknown): value is OwnedProcessIdentity {
  return (
    isObject(value) &&
    typeof value.pid === "number" &&
    Number.isInteger(value.pid) &&
    value.pid >= 2 &&
    typeof value.processGroupId === "number" &&
    Number.isInteger(value.processGroupId) &&
    value.processGroupId >= 2 &&
    typeof value.sessionFile === "string" &&
    isAbsolute(value.sessionFile) &&
    typeof value.attemptToken === "string" &&
    value.attemptToken.length > 0
  );
}

function isWorktreeIdentity(value: unknown): boolean {
  return (
    isObject(value) &&
    typeof value.repositoryRoot === "string" &&
    isAbsolute(value.repositoryRoot) &&
    typeof value.path === "string" &&
    isAbsolute(value.path) &&
    typeof value.branch === "string" &&
    value.branch.length > 0 &&
    typeof value.headSha === "string" &&
    /^[0-9a-f]{40}$/u.test(value.headSha)
  );
}

function isReloadMarker(
  value: unknown,
  sessionId: string,
  generation: number,
  hostPid: number,
  repositoryRoot: string,
): boolean {
  return (
    isObject(value) &&
    value.sessionId === sessionId &&
    value.generation === generation &&
    value.hostPid === hostPid &&
    value.repositoryRoot === repositoryRoot &&
    typeof value.requestedAt === "string" &&
    isIsoTimestamp(value.requestedAt)
  );
}

function isOwnerWait(value: unknown): boolean {
  if (
    !isObject(value) ||
    typeof value.ruleId !== "string" ||
    value.ruleId.length === 0 ||
    !isObject(value.evidence) ||
    !Array.isArray(value.allowedDecisions) ||
    value.allowedDecisions.length === 0 ||
    !value.allowedDecisions.every(
      (decision) => typeof decision === "string" && decision.trim().length > 0,
    ) ||
    new Set(value.allowedDecisions).size !== value.allowedDecisions.length ||
    typeof value.startedAt !== "string" ||
    !isIsoTimestamp(value.startedAt) ||
    typeof value.resumeState !== "string" ||
    !RUN_STATE_SET.has(value.resumeState) ||
    value.resumeState === "awaiting_owner"
  ) {
    return false;
  }
  if (value.response === undefined) return true;
  return (
    isObject(value.response) &&
    typeof value.response.decision === "string" &&
    value.allowedDecisions.includes(value.response.decision) &&
    typeof value.response.rationale === "string" &&
    value.response.rationale.trim().length > 0 &&
    typeof value.response.answeredAt === "string" &&
    isIsoTimestamp(value.response.answeredAt)
  );
}

function assertWorkerEvent(value: unknown): asserts value is WorkerEvent {
  if (!isObject(value) || typeof value.type !== "string") {
    throw new LedgerError("worker event is not an object with a type");
  }
  switch (value.type) {
    case "started":
      if (
        !hasExactKeys(value, ["type", "identity"]) ||
        !isOwnedProcessIdentity(value.identity) ||
        !isObject(value.identity) ||
        !hasExactKeys(value.identity, ["pid", "processGroupId", "sessionFile", "attemptToken"])
      ) {
        throw new LedgerError("worker started event has invalid identity evidence");
      }
      return;
    case "prompt_accepted":
    case "settled":
    case "abort":
      if (!hasExactKeys(value, ["type"])) {
        throw new LedgerError(`worker ${value.type} event has unsupported fields`);
      }
      return;
    case "tool_activity":
      if (
        !hasExactKeys(value, ["type", "toolName"]) ||
        typeof value.toolName !== "string" ||
        value.toolName.length > MAX_WORKER_EVENT_TEXT_CHARS
      ) {
        throw new LedgerError("worker tool activity event is invalid");
      }
      return;
    case "usage":
      if (
        !hasExactKeys(value, ["type", "input", "output", "total", "cost"]) ||
        ![value.input, value.output, value.total, value.cost].every(
          (amount) => typeof amount === "number" && Number.isFinite(amount) && amount >= 0,
        )
      ) {
        throw new LedgerError("worker usage event is invalid");
      }
      return;
    case "blocked":
    case "error":
      if (
        !hasExactKeys(value, ["type", "message"]) ||
        typeof value.message !== "string" ||
        value.message.length > MAX_WORKER_EVENT_TEXT_CHARS
      ) {
        throw new LedgerError(`worker ${value.type} event is invalid`);
      }
      return;
    case "exit":
      if (
        !hasExactKeys(value, ["type", "code", "signal"]) ||
        (value.code !== null &&
          (typeof value.code !== "number" || !Number.isInteger(value.code) || value.code < 0)) ||
        !isExitSignal(value.signal)
      ) {
        throw new LedgerError("worker exit event is invalid");
      }
      return;
    default:
      throw new LedgerError(`unsupported worker event type: ${value.type}`);
  }
}

function hasExactKeys(value: object, expected: string[]): boolean {
  const keys = Object.keys(value).sort();
  const expectedKeys = [...expected].sort();
  return keys.length === expectedKeys.length &&
    keys.every((key, index) => key === expectedKeys[index]);
}

function isOwnedProcessExitEvidence(value: unknown): value is OwnedProcessExitEvidence {
  return isObject(value) &&
    hasExactKeys(value, ["exitCode", "exitSignal"]) &&
    (value.exitCode === null ||
      (typeof value.exitCode === "number" &&
        Number.isInteger(value.exitCode) &&
        value.exitCode >= 0)) &&
    isExitSignal(value.exitSignal);
}

function isExitSignal(value: unknown): value is NodeJS.Signals | null {
  return value === null || (typeof value === "string" && EXIT_SIGNAL_SET.has(value));
}

function isIsoTimestamp(value: string): boolean {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) && new Date(parsed).toISOString() === value;
}

function sameOptionalField(
  left: RunRecord,
  right: RunRecord,
  key: "policyCheckpoint" | "pendingPolicyAction",
): boolean {
  return Object.hasOwn(left, key) === Object.hasOwn(right, key)
    && isDeepStrictEqual(left[key], right[key]);
}

function assertPolicyDeliveryEvidence(
  actionId: string,
  value: unknown,
): asserts value is PolicyActionDeliveryEvidence {
  if (
    !isObject(value)
    || !hasExactKeys(value, ["adapter", "idempotencyKey", "receipt"])
    || typeof value.adapter !== "string"
    || value.adapter.trim().length === 0
    || value.adapter.length > MAX_POLICY_DELIVERY_TEXT_CHARS
    || typeof value.idempotencyKey !== "string"
    || value.idempotencyKey !== actionId
    || value.idempotencyKey.length === 0
    || value.idempotencyKey.length > MAX_POLICY_DELIVERY_TEXT_CHARS
  ) {
    throw new LedgerError("policy delivery evidence or idempotency key is invalid");
  }
  assertBoundedJson(value, MAX_POLICY_DELIVERY_EVIDENCE_BYTES, "policy delivery evidence");
}

function assertBoundedJson(
  value: unknown,
  maximumBytes: number,
  label: string,
  ErrorType: new (message: string) => Error = LedgerError,
): void {
  assertJsonSafe(value, label, new Set(), 0, ErrorType);
  let serialized: string;
  try {
    serialized = JSON.stringify(value);
  } catch {
    throw new ErrorType(`${label} is not JSON-safe`);
  }
  if (Buffer.byteLength(serialized, "utf8") > maximumBytes) {
    throw new ErrorType(`${label} exceeds ${maximumBytes} bytes`);
  }
}

function assertJsonSafe(
  value: unknown,
  label: string,
  ancestors: Set<object>,
  depth: number,
  ErrorType: new (message: string) => Error,
): void {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (Number.isFinite(value)) return;
    throw new ErrorType(`${label} contains a non-finite number`);
  }
  if (typeof value !== "object" || depth > 100) {
    throw new ErrorType(`${label} is not a bounded JSON value`);
  }
  if (ancestors.has(value)) throw new ErrorType(`${label} contains a cycle`);
  ancestors.add(value);
  try {
    if (Array.isArray(value)) {
      for (const item of value) assertJsonSafe(item, label, ancestors, depth + 1, ErrorType);
      return;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new ErrorType(`${label} contains a non-JSON object`);
    }
    for (const [key, child] of Object.entries(value)) {
      if (key.length > MAX_POLICY_DELIVERY_TEXT_CHARS) {
        throw new ErrorType(`${label} contains an oversized key`);
      }
      assertJsonSafe(child, label, ancestors, depth + 1, ErrorType);
    }
  } finally {
    ancestors.delete(value);
  }
}

function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}
