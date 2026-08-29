import type { PolicyAction, PolicyCheckpoint } from "../reducer.ts";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export const RUN_STATES = [
  "submitted",
  "preparing",
  "worker_running",
  "initial_review",
  "fixing",
  "re_reviewing",
  "integrating",
  "validating",
  "supervisor_pending",
  "awaiting_owner",
  "validated",
  "failed",
  "interrupted",
  "stopped",
] as const;

export type RunState = (typeof RUN_STATES)[number];

export const TERMINAL_RUN_STATES = new Set<RunState>([
  "validated",
  "failed",
  "interrupted",
  "stopped",
]);

export interface OwnedProcessIdentity {
  pid: number;
  processGroupId: number;
  sessionFile: string;
  attemptToken: string;
}

export interface OwnedProcessExitEvidence {
  exitCode: number | null;
  exitSignal: NodeJS.Signals | null;
}

/** The closed set of normalized observations emitted by one owned Pi worker attempt. */
export type WorkerEvent =
  | { type: "started"; identity: OwnedProcessIdentity }
  | { type: "prompt_accepted" }
  | { type: "tool_activity"; toolName: string }
  | { type: "usage"; input: number; output: number; total: number; cost: number }
  | { type: "settled" }
  | { type: "blocked"; message: string }
  | { type: "error"; message: string }
  | { type: "abort" }
  | { type: "exit"; code: number | null; signal: NodeJS.Signals | null };

export interface WorktreeIdentity {
  /** Canonical real path of the repository whose Worktrunk list established ownership. */
  repositoryRoot: string;
  /** Canonical forge URL from schema-2 repo.forge, or null when the repository has no forge. */
  remoteUrl: string | null;
  branch: string;
  /** Canonical real path of the owned feature worktree. */
  path: string;
  baseSha: string;
  headSha: string;
  /** Ownership is valid only while Worktrunk reports no in-progress operation. */
  operation: null;
  /** Ownership is valid only while all schema-2 change bits remain false. */
  clean: true;
}

export interface ReloadMarker {
  sessionId: string;
  generation: number;
  hostPid: number;
  repositoryRoot: string;
  requestedAt: string;
}

export interface OwnerWait {
  ruleId: string;
  evidence: JsonObject;
  allowedDecisions: string[];
  startedAt: string;
  resumeState: Exclude<RunState, "awaiting_owner">;
  response?: {
    decision: string;
    rationale: string;
    answeredAt: string;
  };
}

export interface RunRecord {
  schemaVersion: 1;
  runId: string;
  objective: string;
  supervisorSessionId: string;
  repositoryRoot: string;
  policyHash: string;
  policyFile: "policy.yaml";
  generation: number;
  hostPid: number;
  sequence: number;
  state: RunState;
  reason: string;
  createdAt: string;
  updatedAt: string;
  ownedProcesses: OwnedProcessIdentity[];
  worktrees: WorktreeIdentity[];
  reload?: ReloadMarker;
  ownerWait?: OwnerWait;
  /** Null is the explicit start marker; absence identifies a legacy run. */
  policyCheckpoint?: PolicyCheckpoint | null;
  /** Append-first outbox slot. Dispatchers must acknowledge it before another reduction. */
  pendingPolicyAction?: PolicyAction;
}

export type RunActor = "owner" | "extension" | "supervisor" | "worker" | "system";

export interface RunEvent {
  schemaVersion: 1;
  eventId: string;
  runId: string;
  sequence: number;
  timestamp: string;
  type: string;
  fromState?: RunState;
  toState?: RunState;
  ruleId: string;
  actor: RunActor;
  evidence: JsonObject;
  projection: RunRecord;
  actionId?: string;
  policyRevision?: number;
  actionType?: PolicyAction["type"];
  delivery?: "delivered";
}

export interface PolicyActionDeliveryEvidence {
  adapter: string;
  idempotencyKey: string;
  receipt: JsonValue;
}

export interface CreateRunInput {
  objective: string;
  supervisorSessionId: string;
  repositoryRoot: string;
  policySnapshot: string;
  hostPid?: number;
}

export interface TransitionInput {
  state: RunState;
  reason: string;
  ruleId: string;
  actor: RunActor;
  evidence?: JsonObject;
}

export interface AwaitOwnerInput {
  ruleId: string;
  evidence: JsonObject;
  allowedDecisions: string[];
  resumeState: Exclude<RunState, "awaiting_owner">;
}

export interface OwnerAnswerInput {
  decision: string;
  rationale: string;
}

export interface OwnershipInput {
  ownedProcesses: OwnedProcessIdentity[];
  worktrees: WorktreeIdentity[];
}

export interface TailRecovery {
  droppedBytes: number;
  previousSequence: number;
}

export interface RunLoadResult {
  record: RunRecord;
  recovery?: TailRecovery;
}
