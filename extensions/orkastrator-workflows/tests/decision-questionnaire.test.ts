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

test("questionnaire coordinator returns a typed answer to the live workflow channel", async () => {
  let registered: ((presentation: unknown, context: unknown) => Promise<unknown>) | undefined;
  const workflowRequest = request();
  new WorkflowDecisionQuestionnaire({} as never, {
    register: (presenter) => {
      registered = presenter as typeof registered;
      return () => {};
    },
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
  assert.notEqual(registered, undefined);
  const response = await registered!(
    {
      requestId: "pending-1",
      runId: workflowRequest.runId,
      revision: 3,
      request: workflowRequest,
    },
    context().value,
  );
  assert.deepEqual(response, {
    choice: "replan",
    input: { instructions: "Use React, not Svelte." },
  });
});

test("stopping the coordinator invalidates an in-flight popup", async () => {
  let registered: ((presentation: unknown, context: unknown) => Promise<unknown>) | undefined;
  let finishPresentation: ((value: unknown) => void) | undefined;
  const workflowRequest = request();
  const coordinator = new WorkflowDecisionQuestionnaire({} as never, {
    register: (presenter) => {
      registered = presenter as typeof registered;
      return () => {};
    },
    present: (() => new Promise((resolve) => { finishPresentation = resolve; })) as never,
  });
  assert.notEqual(registered, undefined);
  const response = registered!(
    {
      requestId: "pending-1",
      runId: workflowRequest.runId,
      revision: 3,
      request: workflowRequest,
    },
    context().value,
  );
  await new Promise((resolve) => setTimeout(resolve, 5));
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
  assert.equal(await response, undefined);
});
