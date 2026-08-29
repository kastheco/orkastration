import { randomUUID } from "node:crypto";

import type {
  SubagentDelegationRequest,
  SubagentDelegationResponse,
  SubagentDelegationTerminalResponse,
} from "pi-subagents/delegation";

export const SUBAGENT_DELEGATION_REQUEST_EVENT = "prompt-template:subagent:request";
export const SUBAGENT_DELEGATION_RESPONSE_EVENT = "prompt-template:subagent:response";
export const SUBAGENT_DELEGATION_CANCEL_EVENT = "prompt-template:subagent:cancel";

const BRIDGE = Symbol.for("orkastrator.pi-subagents-delegation.v1");

export interface DelegationEvents {
  on(event: string, handler: (payload: unknown) => void): (() => void) | void;
  emit(event: string, payload: unknown): void;
}

export interface DelegationSpec extends Omit<
  SubagentDelegationRequest,
  "requestId" | "result"
> {
  result:
    | { kind: "text" }
    | { kind: "structured"; schema: Record<string, unknown> };
}

interface InstalledBridge {
  events: DelegationEvents;
  owner: object;
}

type BridgeGlobal = typeof globalThis & { [BRIDGE]?: InstalledBridge };

/** Install the extension-to-extension transport used by workflow action nodes. */
export function installDelegationBridge(events: DelegationEvents, owner: object): () => void {
  const target = globalThis as BridgeGlobal;
  const installed = { events, owner };
  target[BRIDGE] = installed;
  return () => {
    if (target[BRIDGE] === installed) delete target[BRIDGE];
  };
}

/** Run one configured pi-subagents leaf and resolve only its correlated terminal response. */
export async function delegateSubagent(
  spec: DelegationSpec,
  signal: AbortSignal,
): Promise<SubagentDelegationTerminalResponse> {
  const bridge = (globalThis as BridgeGlobal)[BRIDGE];
  if (bridge === undefined) {
    throw new Error("Orkastrator workflow bridge is not installed");
  }
  const request: SubagentDelegationRequest = {
    ...spec,
    requestId: randomUUID(),
  };
  return await new Promise<SubagentDelegationTerminalResponse>((resolve, reject) => {
    let settled = false;
    const finish = (
      outcome: { response: SubagentDelegationTerminalResponse } | { error: Error },
    ): void => {
      if (settled) return;
      settled = true;
      unsubscribe?.();
      signal.removeEventListener("abort", abort);
      if ("response" in outcome) resolve(outcome.response);
      else reject(outcome.error);
    };
    const abort = (): void => {
      bridge.events.emit(SUBAGENT_DELEGATION_CANCEL_EVENT, {
        requestId: request.requestId,
        ownerRunId: request.ownerRunId,
        nodeId: request.nodeId,
      });
      finish({ error: new DOMException("Subagent delegation aborted", "AbortError") });
    };
    const unsubscribe = bridge.events.on(SUBAGENT_DELEGATION_RESPONSE_EVENT, (payload) => {
      const response = payload as SubagentDelegationResponse;
      if (response.requestId !== request.requestId) return;
      if (response.ownerRunId !== request.ownerRunId || response.nodeId !== request.nodeId) return;
      if (response.status === "invalid_request") {
        finish({ error: new Error(response.error ?? "pi-subagents rejected delegation") });
        return;
      }
      finish({ response });
    });
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) {
      abort();
      return;
    }
    bridge.events.emit(SUBAGENT_DELEGATION_REQUEST_EVENT, request);
  });
}
