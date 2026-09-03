import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { HumanDecisionRequest, HumanDecisionResponse } from "@osolmaz/pi-workflows";
import {
  WorkflowClient,
  type ClientEvent,
  type WorkflowSessionView,
} from "@osolmaz/pi-workflows/client";

/**
 * Mirrors `ClientInteractiveRequest` from `@osolmaz/pi-workflows/client`.
 *
 * Pi Workflows 0.16.0 defines the type in `dist/client/view.d.ts` but omits it
 * from the `./client` barrel, and package exports block the deep path. Only the
 * fields this presenter reads are declared. Drop this in favour of the upstream
 * import once the barrel re-exports it.
 */
export type ClientInteractiveRequest = {
  requestId: string;
  runId: string;
  revision: number;
  kind: "agent" | "assistant" | "decision";
  status: "pending" | "presenting" | "settled" | "cancelled";
  contract: unknown;
  presentationClaimExpiresAt: string | null;
};

/**
 * One pending decision this presenter may claim and answer.
 *
 * Pi Workflows 0.16.0 replaced the process-global presenter hook with a
 * claim-based protocol: a presenter claims `requestId` at an exact `revision`,
 * presents, then settles. A live claim held by another presenter is skipped
 * rather than stolen, so a ClickClack presenter and the TUI can watch the same
 * session without racing a decision away from each other.
 */
export type WorkflowHumanDecisionPresentation = {
  requestId: string;
  runId: string;
  revision: number;
  request: HumanDecisionRequest;
};

export type WorkflowHumanDecisionPresenter = (
  presentation: WorkflowHumanDecisionPresentation,
  context: ExtensionContext,
) => Promise<HumanDecisionResponse | undefined>;

const MAX_OPTION_LABEL = 60;

type QuestionParams = {
  questions: Array<{
    question: string;
    header: string;
    options: Array<{ label: string; description: string; preview?: string }>;
    multiSelect?: boolean;
  }>;
};

type QuestionAnswer = {
  questionIndex: number;
  question: string;
  kind: "option" | "custom" | "multi";
  answer: string | null;
  selected?: string[];
  notes?: string;
  preview?: string;
};

type QuestionnaireResult = {
  answers: QuestionAnswer[];
  cancelled: boolean;
  globalNote?: string;
  error?: string;
};

type QuestionnaireToolResult = {
  content: Array<{ type: "text"; text: string }>;
  details: QuestionnaireResult;
};

type QuestionnairePresenter = (
  pi: ExtensionAPI,
  context: ExtensionContext,
  params: QuestionParams,
) => Promise<QuestionnaireToolResult>;

type DecisionQuestionnaire = {
  params: QuestionParams;
  choiceByLabel: ReadonlyMap<string, string>;
};

type QuestionnaireEnvelope = { details?: unknown };

type DecisionQuestionnaireDependencies = {
  /**
   * Subscribes to one session's pending interactions. Returns an unsubscribe.
   * Injected so tests drive the presenter without a running workflow host.
   */
  watch: (
    sessionId: string,
    listener: (interactions: ClientInteractiveRequest[]) => void,
  ) => Promise<() => Promise<void>>;
  /** Claims one pending decision. Resolves false when another presenter holds it. */
  claim: (interaction: ClientInteractiveRequest) => Promise<boolean>;
  /** Settles a claimed decision with the operator's answer. */
  submit: (
    interaction: ClientInteractiveRequest,
    response: HumanDecisionResponse,
  ) => Promise<void>;
  present: QuestionnairePresenter;
};

const require = createRequire(import.meta.url);
let presenterPromise: Promise<QuestionnairePresenter> | undefined;

async function loadQuestionnairePresenter(): Promise<QuestionnairePresenter> {
  const modulePath = require.resolve("@juicesharp/rpiv-ask-user-question");
  const imported = await import(pathToFileURL(modulePath).href) as {
    presentQuestionnaire?: QuestionnairePresenter;
  };
  if (typeof imported.presentQuestionnaire !== "function") {
    throw new Error("rpiv-ask-user-question does not expose presentQuestionnaire");
  }
  return imported.presentQuestionnaire;
}

let sharedClient: WorkflowClient | undefined;

function workflowClient(): WorkflowClient {
  sharedClient ??= new WorkflowClient({ clientId: `orkastrator-${randomUUID()}` });
  return sharedClient;
}

function sessionInteractions(event: ClientEvent): ClientInteractiveRequest[] | undefined {
  const view = (event as { view?: unknown }).view;
  if (typeof view !== "object" || view === null) return undefined;
  const pending: unknown = (view as WorkflowSessionView).pendingInteractions;
  if (!Array.isArray(pending)) return undefined;
  return pending.filter(
    (entry): entry is ClientInteractiveRequest =>
      isRecord(entry) && typeof entry.requestId === "string" && typeof entry.runId === "string",
  );
}

const defaultDependencies: DecisionQuestionnaireDependencies = {
  watch: async (sessionId, listener) => {
    const client = workflowClient();
    await client.ensureAvailable();
    return await client.watchSession(sessionId, (event) => {
      const interactions = sessionInteractions(event);
      if (interactions !== undefined) listener(interactions);
    });
  },
  claim: async (interaction) => {
    const client = workflowClient();
    await client.ensureAvailable();
    const key = `claim-presentation-${interaction.requestId}-${interaction.revision}-${client.clientId}`;
    const response = await client.request({
      operation: "interaction.update",
      requestId: key,
      idempotencyKey: key,
      runId: interaction.runId,
      expectedRevision: interaction.revision,
      payload: { requestId: interaction.requestId, claimPresentation: true },
    });
    if (response.outcome === "conflict") return false;
    if (response.outcome !== "accepted" && response.outcome !== "adopted") {
      throw new Error(response.error ?? "Workflow host rejected the presentation claim");
    }
    return true;
  },
  submit: async (interaction, response) => {
    const client = workflowClient();
    await client.ensureAvailable();
    const submissionId = randomUUID();
    const settled = await client.requestDurable({
      operation: "decision.answer",
      idempotencyKey: submissionId,
      runId: interaction.runId,
      expectedRevision: interaction.revision,
      payload: {
        requestId: interaction.requestId,
        submissionId,
        response,
      } as never,
    });
    if (settled.outcome !== "accepted" && settled.outcome !== "adopted") {
      throw new Error(settled.error ?? "Workflow host rejected decision.answer");
    }
  },
  present: async (pi, context, params) => {
    presenterPromise ??= loadQuestionnairePresenter();
    return await (await presenterPromise)(pi, context, params);
  },
};

export class WorkflowDecisionQuestionnaire {
  private activeRequestId: string | undefined;
  private generation = 0;
  private unwatch: (() => Promise<void>) | undefined;
  private readonly pi: ExtensionAPI;
  private readonly dependencies: DecisionQuestionnaireDependencies;

  constructor(
    pi: ExtensionAPI,
    dependencies: Partial<DecisionQuestionnaireDependencies> = {},
  ) {
    this.pi = pi;
    this.dependencies = { ...defaultDependencies, ...dependencies };
  }

  /**
   * Subscribes to this session's pending decisions. The host replays pending
   * interactions on subscribe, so a decision raised before the session started
   * is still delivered and cannot race session_start.
   */
  start(context: ExtensionContext): void {
    const sessionId = context.sessionManager.getSessionId();
    const generation = this.generation;
    void this.dependencies
      .watch(sessionId, (interactions) => {
        if (generation !== this.generation) return;
        void this.consume(interactions, context);
      })
      .then((unwatch) => {
        if (generation === this.generation) this.unwatch = unwatch;
        else void unwatch();
      })
      .catch((error: unknown) => {
        context.ui.notify(
          `Workflow decisions are unavailable: ${
            error instanceof Error ? error.message : String(error)
          }`,
          "warning",
        );
      });
  }

  stop(): void {
    this.generation += 1;
    this.activeRequestId = undefined;
    const unwatch = this.unwatch;
    this.unwatch = undefined;
    if (unwatch !== undefined) void unwatch();
  }

  /**
   * Presents the first claimable decision. Only one decision is presented at a
   * time; the rest stay pending and arrive on a later subscription event.
   */
  private async consume(
    interactions: ClientInteractiveRequest[],
    context: ExtensionContext,
  ): Promise<void> {
    if (this.activeRequestId !== undefined) return;
    const generation = this.generation;
    for (const interaction of interactions) {
      if (interaction.kind !== "decision" || interaction.status !== "pending") continue;
      if (claimIsLive(interaction)) continue;

      const contract = interactionRequest(interaction);
      if (contract === undefined) continue;
      if (!await this.dependencies.claim(interaction)) continue;
      if (generation !== this.generation) return;

      const response = await this.presentDecision(
        {
          requestId: interaction.requestId,
          runId: interaction.runId,
          revision: interaction.revision,
          request: contract,
        },
        context,
      );
      if (response !== undefined && generation === this.generation) {
        await this.dependencies.submit(interaction, response);
      }
      return;
    }
  }

  async presentDecision(
    presentation: WorkflowHumanDecisionPresentation,
    context: ExtensionContext,
  ): Promise<HumanDecisionResponse | undefined> {
    const generation = this.generation;
    if (this.activeRequestId !== undefined) return undefined;
    const request = decisionRequest(presentation.request);
    if (request === undefined) throw new Error("Workflow decision contract is invalid");

    this.activeRequestId = presentation.requestId;
    try {
      const questionnaire = buildDecisionQuestionnaire(request);
      const result = await this.dependencies.present(this.pi, context, questionnaire.params);
      if (generation !== this.generation) return undefined;
      const details = (result as QuestionnaireEnvelope).details;
      if (!isQuestionnaireResult(details) || details.cancelled) return undefined;
      const response = await decisionResponse(context, request, questionnaire, details);
      return generation === this.generation ? response : undefined;
    } finally {
      if (this.activeRequestId === presentation.requestId) this.activeRequestId = undefined;
    }
  }
}

export function buildDecisionQuestionnaire(request: HumanDecisionRequest): DecisionQuestionnaire {
  const choiceByLabel = new Map<string, string>();
  const usedLabels = new Set<string>();
  const preview = decisionPreview(request);
  const options = Object.entries(request.choices).map(([key, choice]) => {
    const label = uniqueOptionLabel(choice.label, key, usedLabels);
    choiceByLabel.set(label, key);
    return {
      label,
      description:
        choice.input === undefined
          ? "Choose this workflow route."
          : `${choice.input.prompt} You can type the answer directly or attach it as a note.`,
      ...(preview === "" ? {} : { preview }),
    };
  });
  if (options.length < 2 || options.length > 4) {
    throw new Error(`Workflow decision has ${options.length} choices; the popup supports 2 through 4.`);
  }
  const question = request.title.trim().replace(/[?.!]*$/u, "");
  return {
    params: {
      questions: [
        {
          question: `${question || "How should this workflow proceed"}?`,
          header: "Decision",
          options,
        },
      ],
    },
    choiceByLabel,
  };
}

export async function decisionResponse(
  context: Pick<ExtensionContext, "ui">,
  request: HumanDecisionRequest,
  questionnaire: DecisionQuestionnaire,
  result: QuestionnaireResult,
): Promise<HumanDecisionResponse | undefined> {
  const answer = result.answers[0];
  if (answer === undefined) return undefined;
  if (answer.kind === "option") {
    const choice = answer.answer === null ? undefined : questionnaire.choiceByLabel.get(answer.answer);
    if (choice === undefined) throw new Error("Questionnaire returned an unknown workflow choice.");
    return await responseForChoice(
      context,
      request,
      choice,
      combinedText(answer.notes, result.globalNote),
    );
  }
  if (answer.kind !== "custom" || answer.answer === null || answer.answer.trim() === "") {
    return undefined;
  }

  const inputChoices = Object.entries(request.choices).filter(([, choice]) => choice.input !== undefined);
  let selected: string | undefined;
  if (inputChoices.length === 1) {
    selected = inputChoices[0]?.[0];
  } else if (inputChoices.length > 1) {
    const labels = inputChoices.map(([key, choice]) => `${choice.label} (${key})`);
    const selectedLabel = await context.ui.select("Apply this answer to which choice?", labels);
    selected = inputChoices[labels.indexOf(selectedLabel ?? "")]?.[0];
  }
  if (selected === undefined) {
    context.ui.notify("This decision has no route that accepts an open-ended answer.", "warning");
    return undefined;
  }
  return await responseForChoice(
    context,
    request,
    selected,
    combinedText(answer.answer, answer.notes, result.globalNote),
  );
}

async function responseForChoice(
  context: Pick<ExtensionContext, "ui">,
  request: HumanDecisionRequest,
  choiceKey: string,
  suppliedText?: string,
): Promise<HumanDecisionResponse | undefined> {
  const choice = request.choices[choiceKey];
  if (choice === undefined) throw new Error("Workflow choice disappeared from its decision contract.");
  if (choice.input === undefined) {
    if (suppliedText?.trim()) {
      context.ui.notify(
        `${choice.label} cannot carry notes. The workflow answer was not submitted.`,
        "warning",
      );
      return undefined;
    }
    return { choice: choiceKey };
  }

  let text = suppliedText?.trim();
  if (text === undefined || !validInputLength(text, choice.input.minLength, choice.input.maxLength)) {
    text = (await context.ui.input(choice.input.prompt, text ?? ""))?.trim();
  }
  if (text === undefined) return undefined;
  if (!validInputLength(text, choice.input.minLength, choice.input.maxLength)) {
    context.ui.notify(
      `Answer must be ${choice.input.minLength} through ${choice.input.maxLength} characters.`,
      "error",
    );
    return undefined;
  }
  return { choice: choiceKey, input: { [choice.input.name]: text } };
}

function decisionRequest(value: unknown): HumanDecisionRequest | undefined {
  if (!isRecord(value) || value.schema !== "pi-workflows.human-decision-request.v1") return undefined;
  if (typeof value.title !== "string" || !isRecord(value.choices) || !isRecord(value.presentation)) {
    return undefined;
  }
  return value as HumanDecisionRequest;
}

function decisionPreview(request: HumanDecisionRequest): string {
  const lines = request.presentation.blocks.flatMap((block) => {
    if (block.kind === "section") return [`## ${block.title}`];
    if (block.kind === "paragraph") return [block.text];
    if (block.kind === "preformatted") return ["```", block.text, "```"];
    if (block.kind === "bullets") return block.items.map((item) => `- ${item}`);
    return block.items.map((item) => `**${item.label}:** ${item.value}`);
  });
  return [request.presentation.summary, ...lines].filter((line) => line.trim() !== "").join("\n\n");
}

function uniqueOptionLabel(label: string, key: string, used: Set<string>): string {
  const base = label.trim().slice(0, MAX_OPTION_LABEL) || key.slice(0, MAX_OPTION_LABEL);
  if (!used.has(base)) {
    used.add(base);
    return base;
  }
  const suffix = ` (${key})`;
  const unique = `${base.slice(0, MAX_OPTION_LABEL - suffix.length)}${suffix}`;
  if (used.has(unique)) throw new Error("Workflow decision choice labels are not unique.");
  used.add(unique);
  return unique;
}

function combinedText(...values: Array<string | undefined>): string | undefined {
  const parts = values.map((value) => value?.trim()).filter((value): value is string => Boolean(value));
  return parts.length === 0 ? undefined : parts.join("\n\n");
}

function validInputLength(value: string, minimum: number, maximum: number): boolean {
  return value.length >= minimum && value.length <= maximum;
}

function isQuestionnaireResult(value: unknown): value is QuestionnaireResult {
  return isRecord(value) && Array.isArray(value.answers) && typeof value.cancelled === "boolean";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export const __decisionQuestionnaireTest__ = {
  decisionRequest,
  decisionPreview,
  responseForChoice,
};

/** True when another presenter holds an unexpired claim on this decision. */
function claimIsLive(interaction: ClientInteractiveRequest): boolean {
  const expiry = interaction.presentationClaimExpiresAt;
  return expiry !== null && Date.parse(expiry) > Date.now();
}

/** Reads the human-decision request out of one pending interaction's contract. */
function interactionRequest(
  interaction: ClientInteractiveRequest,
): HumanDecisionRequest | undefined {
  const contract = interaction.contract;
  if (!isRecord(contract)) return undefined;
  return decisionRequest(contract.request ?? contract);
}
