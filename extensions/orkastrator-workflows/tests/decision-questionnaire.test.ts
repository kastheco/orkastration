import assert from "node:assert/strict";
import { test } from "node:test";

import type { HumanDecisionRequest } from "@osolmaz/pi-workflows";

import {
  WorkflowDecisionQuestionnaire,
  buildDecisionQuestionnaire,
  decisionResponse,
} from "../decision-questionnaire.ts";

function request(): HumanDecisionRequest {
  return {
    schema: "pi-workflows.human-decision-request.v1",
    decisionId: "decision-1",
    requestDigest: "sha256:request",
    runId: "run-1",
    workflowName: "orkastrator-cook",
    nodeId: "planning/approval/approve",
    attemptId: "attempt-1",
    audience: "operator",
    title: "Approve the implementation plan",
    choices: {
      continue: { label: "Yes, continue" },
      replan: {
        label: "Replan",
        input: {
          kind: "text",
          name: "instructions",
          prompt: "What should change?",
          minLength: 1,
          maxLength: 4000,
        },
      },
      stop: { label: "No, stop" },
    },
    createdAt: "2026-09-02T17:33:30.098Z",
    subject: { task: "build the terminal" },
    presentation: {
      schema: "pi-workflows.decision-presentation.v1",
      summary: "Use the existing React island rather than adding Svelte.",
      blocks: [
        { kind: "section", title: "Plan" },
        { kind: "bullets", items: ["Reuse the existing island", "Keep the bridge narrow"] },
      ],
    },
    revision: 1,
    subjectDigest: "sha256:subject",
    presentationDigest: "sha256:presentation",
  };
}

function context(input?: string) {
  const notifications: Array<[string, string]> = [];
  return {
    notifications,
    value: {
      ui: {
        input: async () => input,
        select: async () => undefined,
        notify: (message: string, level: string) => notifications.push([message, level]),
      },
    },
  };
}

function sessionContext(input?: string) {
  const base = context(input);
  return {
    notifications: base.notifications,
    value: {
      ...base.value,
      sessionManager: { getSessionId: () => "session-1" },
    },
  };
}

function pendingDecision(workflowRequest: HumanDecisionRequest) {
  return {
    requestId: "pending-1",
    runId: workflowRequest.runId,
    revision: 3,
    kind: "decision" as const,
    status: "pending" as const,
    contract: { request: workflowRequest },
    presentationClaimExpiresAt: null,
  };
}

/** Lets the coordinator's subscribe/claim/present/submit chain drain. */
async function settle(): Promise<void> {
  for (let tick = 0; tick < 10; tick += 1) await new Promise((resolve) => setTimeout(resolve, 1));
}

test("decision questionnaire keeps structured choices and the full plan preview", () => {
  const questionnaire = buildDecisionQuestionnaire(request());
  const [question] = questionnaire.params.questions;
  assert.equal(question?.header, "Decision");
  assert.equal(question?.question, "Approve the implementation plan?");
  assert.deepEqual(question?.options.map((option) => option.label), [
    "Yes, continue",
    "Replan",
    "No, stop",
  ]);
  assert.match(question?.options[0]?.preview ?? "", /Use the existing React island/u);
  assert.match(question?.options[0]?.preview ?? "", /Reuse the existing island/u);
});

test("a choice note supplies its required open-ended workflow input", async () => {
  const workflowRequest = request();
  const questionnaire = buildDecisionQuestionnaire(workflowRequest);
  const ctx = context();
  const response = await decisionResponse(ctx.value as never, workflowRequest, questionnaire, {
    answers: [
      {
        questionIndex: 0,
        question: "Approve the implementation plan?",
        kind: "option",
        answer: "Replan",
        notes: "Use React, not Svelte.",
      },
    ],
    cancelled: false,
    globalNote: "Preserve API compatibility.",
  });
  assert.deepEqual(response, {
    choice: "replan",
    input: { instructions: "Use React, not Svelte.\n\nPreserve API compatibility." },
  });
});

test("notes on a choice without workflow input are never silently discarded", async () => {
  const workflowRequest = request();
  const questionnaire = buildDecisionQuestionnaire(workflowRequest);
  const ctx = context();
  const response = await decisionResponse(ctx.value as never, workflowRequest, questionnaire, {
    answers: [
      {
        questionIndex: 0,
        question: "Approve the implementation plan?",
        kind: "option",
        answer: "Yes, continue",
        notes: "Preserve API compatibility.",
      },
    ],
    cancelled: false,
  });
  assert.equal(response, undefined);
  assert.deepEqual(ctx.notifications, [
    ["Yes, continue cannot carry notes. The workflow answer was not submitted.", "warning"],
  ]);
});

test("a fully custom answer maps to the only choice that accepts text", async () => {
  const workflowRequest = request();
  const questionnaire = buildDecisionQuestionnaire(workflowRequest);
  const ctx = context();
  const response = await decisionResponse(ctx.value as never, workflowRequest, questionnaire, {
    answers: [
      {
        questionIndex: 0,
        question: "Approve the implementation plan?",
        kind: "custom",
        answer: "Use React, not Svelte.",
      },
    ],
    cancelled: false,
  });
  assert.deepEqual(response, {
    choice: "replan",
    input: { instructions: "Use React, not Svelte." },
  });
});

test("selecting an input choice opens a text field when no note was attached", async () => {
  const workflowRequest = request();
  const questionnaire = buildDecisionQuestionnaire(workflowRequest);
  const ctx = context("Keep this inside the existing island.");
  const response = await decisionResponse(ctx.value as never, workflowRequest, questionnaire, {
    answers: [
      {
        questionIndex: 0,
        question: "Approve the implementation plan?",
        kind: "option",
        answer: "Replan",
      },
    ],
    cancelled: false,
  });
  assert.deepEqual(response, {
    choice: "replan",
    input: { instructions: "Keep this inside the existing island." },
  });
});

test("questionnaire coordinator claims a pending decision and submits its answer", async () => {
  const workflowRequest = request();
  const submitted: Array<{ requestId: string; response: unknown }> = [];
  const claimed: string[] = [];
  const coordinator = new WorkflowDecisionQuestionnaire({} as never, {
    watch: (async (_sessionId: string, listener: (value: unknown) => void) => {
      listener([pendingDecision(workflowRequest)]);
      return async () => {};
    }) as never,
    claim: (async (interaction: { requestId: string }) => {
      claimed.push(interaction.requestId);
      return true;
    }) as never,
    submit: (async (interaction: { requestId: string }, response: unknown) => {
      submitted.push({ requestId: interaction.requestId, response });
    }) as never,
    present: (async () => ({
      content: [{ type: "text", text: "answered" }],
      details: {
        answers: [{
          questionIndex: 0,
          question: "Approve the implementation plan?",
          kind: "custom",
          answer: "Use React, not Svelte.",
        }],
        cancelled: false,
      },
    })) as never,
  });

  coordinator.start(sessionContext().value as never);
  await settle();

  assert.deepEqual(claimed, ["pending-1"]);
  assert.equal(submitted.length, 1);
  assert.deepEqual(submitted[0]?.response, {
    choice: "replan",
    input: { instructions: "Use React, not Svelte." },
  });
});

test("questionnaire coordinator skips a decision another presenter already claimed", async () => {
  const workflowRequest = request();
  let presented = 0;
  const coordinator = new WorkflowDecisionQuestionnaire({} as never, {
    watch: (async (_sessionId: string, listener: (value: unknown) => void) => {
      listener([{
        ...pendingDecision(workflowRequest),
        presentationClaimExpiresAt: new Date(Date.now() + 60_000).toISOString(),
      }]);
      return async () => {};
    }) as never,
    claim: (async () => {
      throw new Error("a live claim must not be contested");
    }) as never,
    submit: (async () => {}) as never,
    present: (async () => {
      presented += 1;
      return { content: [], details: { answers: [], cancelled: true } };
    }) as never,
  });

  coordinator.start(sessionContext().value as never);
  await settle();

  assert.equal(presented, 0);
});

test("questionnaire coordinator yields when the host reports a claim conflict", async () => {
  const workflowRequest = request();
  let presented = 0;
  const coordinator = new WorkflowDecisionQuestionnaire({} as never, {
    watch: (async (_sessionId: string, listener: (value: unknown) => void) => {
      listener([pendingDecision(workflowRequest)]);
      return async () => {};
    }) as never,
    claim: (async () => false) as never,
    submit: (async () => {
      throw new Error("a lost claim must not submit an answer");
    }) as never,
    present: (async () => {
      presented += 1;
      return { content: [], details: { answers: [], cancelled: true } };
    }) as never,
  });

  coordinator.start(sessionContext().value as never);
  await settle();

  assert.equal(presented, 0);
});

test("stopping the coordinator discards an in-flight popup answer", async () => {
  const workflowRequest = request();
  let finishPresentation: ((value: unknown) => void) | undefined;
  const submitted: unknown[] = [];
  const coordinator = new WorkflowDecisionQuestionnaire({} as never, {
    watch: (async (_sessionId: string, listener: (value: unknown) => void) => {
      listener([pendingDecision(workflowRequest)]);
      return async () => {};
    }) as never,
    claim: (async () => true) as never,
    submit: (async (_interaction: unknown, response: unknown) => {
      submitted.push(response);
    }) as never,
    present: (() => new Promise((resolve) => { finishPresentation = resolve; })) as never,
  });

  coordinator.start(sessionContext().value as never);
  await settle();
  coordinator.stop();
  finishPresentation?.({
    content: [{ type: "text", text: "answered" }],
    details: {
      answers: [{
        questionIndex: 0,
        question: "Approve the implementation plan?",
        kind: "custom",
        answer: "Use React, not Svelte.",
      }],
      cancelled: false,
    },
  });
  await settle();

  assert.deepEqual(submitted, []);
});
