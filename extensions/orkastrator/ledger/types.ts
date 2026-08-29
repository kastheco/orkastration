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

export interface WorktreeIdentity {
  repositoryRoot: string;
  path: string;
  branch: string;
  headSha: string;
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
