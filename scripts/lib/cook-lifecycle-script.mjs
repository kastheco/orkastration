import { latestStepContract } from "./scripted-model-server.mjs";

/**
 * Scripted model behaviour for the disposable `/kas:cook` lifecycle test.
 *
 * Two kinds of turns arrive at the scripted model:
 *
 * 1. Origin-session turns. Pi Workflows appends a step contract to every
 *    agent-node prompt. The script answers each node with a valid structured
 *    submission through the real `workflow` tool, so validators, the host, the
 *    step-message contract, and the RPC turn plumbing are all exercised.
 *
 * 2. pi-subagents child turns. Orkastrator delegates the initial review to a
 *    `reviewer` child through the Unix broker and pi-subagents. That child has
 *    no `workflow` tool; it is recognised by its `structured_output` tool and
 *    answers with the strict review envelope.
 *
 * The fixture is a Node project with a broken `sum(a, b)`. The worker turn
 * performs the real edit and commit in the prepared task worktree through Pi's
 * `bash` tool, so repository assertions rest on real filesystem state.
 */

const PLAN = {
  summary: "Return a + b from sum(a, b) so the existing test passes.",
  steps: [
    {
      change: "Replace the subtraction in sum(a, b) with addition.",
      where: "src/sum.js",
      verification: "npm test passes in the prepared task worktree.",
    },
  ],
  contracts: ["module.exports.sum keeps its signature and export name"],
  tests: ["test/sum.test.js already covers the expected sum"],
  risks: [{ risk: "Editing unrelated files", mitigation: "Only src/sum.js changes." }],
  boundaries: ["No dependency, API, or test changes."],
};

function submit(contract, output) {
  return {
    kind: "tool",
    toolName: "workflow",
    args: { action: "submit", step: contract.step, attempt: contract.attempt, output },
  };
}

function lastMessageText(messages) {
  const last = messages.at(-1);
  if (last === undefined) return "";
  const content = last.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((part) => (part && typeof part.text === "string" ? part.text : "")).join("\n");
  }
  return "";
}

function workspaceFromPrompt(text) {
  const match = /Prepared workspace: (\{.*\})$/mu.exec(text);
  if (match === null) return undefined;
  try {
    return JSON.parse(match[1]);
  } catch {
    return undefined;
  }
}

function repositoryFromPrompt(text) {
  const match = /^Repository: (\/\S+)$/mu.exec(text);
  return match?.[1];
}

function launchRepositoryFromPrompt(text) {
  const match = /^Launch repository: (\/\S+)$/mu.exec(text);
  return match?.[1];
}

function publishedFromPrompt(text) {
  const match = /Published repositories: (\{.*\})$/mu.exec(text);
  if (match === null) return undefined;
  try {
    return JSON.parse(match[1]);
  } catch {
    return undefined;
  }
}

function selectedFromPrompt(text) {
  const match = /Selected repositories: (\[.*\])$/mu.exec(text);
  if (match === null) return undefined;
  try {
    return JSON.parse(match[1]);
  } catch {
    return undefined;
  }
}

/** Origin-session step handlers keyed by the unqualified node id. */
function stepOutput(contract, promptText, options) {
  const node = contract.step.split("/").at(-1);
  const scope = contract.step.split("/").slice(0, -1).join("/");
  switch (node) {
    case "resolveRepository":
      return {
        status: "resolved",
        repository: launchRepositoryFromPrompt(promptText) ?? options.fixtureRepository,
        reason: "The launch repository is the only Git repository and owns src/sum.js.",
        evidence: ["AGENTS.md routes every task to this repository", "src/sum.js lives here"],
      };
    case "captureIntent":
      return { originalUserInstructions: options.task };
    case "frame":
      return {
        problem: "sum(a, b) subtracts instead of adding, so the existing test fails.",
        success: ["npm test passes"],
        inScope: ["src/sum.js"],
        outOfScope: ["test/sum.test.js", "package.json"],
        constraints: ["Keep the exported API unchanged"],
        controlBoundary: "Only the fixture repository may change.",
      };
    case "solutions":
      return {
        candidates: [
          {
            id: "fix-operator",
            title: "Fix the operator",
            gist: "Return a + b.",
            solution: "Replace the subtraction with addition in src/sum.js.",
            rationale: "Smallest correct change.",
            parts: ["src/sum.js"],
            tradeoffs: ["None"],
          },
          {
            id: "rewrite-module",
            title: "Rewrite the module",
            gist: "Rewrite sum.js with input validation.",
            solution: "Rewrite the module with runtime type checks and a new helper.",
            rationale: "More defensive, but out of scope.",
            parts: ["src/sum.js"],
            tradeoffs: ["Widens the change beyond the task"],
          },
        ],
      };
    case "holyGrail":
      return {
        ideal: "A correct sum with a test that already pins it.",
        outsideDependencies: [],
        additionalValue: ["None beyond the fix"],
      };
    case "select":
      return {
        status: "ready",
        selectedId: "fix-operator",
        why: "It is the proportionate, in-scope fix.",
        relationshipToIdeal: "Identical to the ideal for this task.",
        rejected: [
          { id: "rewrite-module", reason: "Changes more than the task asks for." },
          { id: "ideal", reason: "The ideal equals the selected option here." },
        ],
        compromises: [],
      };
    case "plan":
      return PLAN;
    case "summarize":
      return undefined; // assistant-message step; handled separately
    case "inspectDocumentation":
      return {
        route: "update",
        files: ["docs/plan.md"],
        digests: {},
        reason: "The repository has no documented plan for this change yet.",
        evidence: "docs/ does not exist in the task worktree.",
      };
    case "updateDocumentation":
      return undefined; // needs a real edit; handled by the tool-call phase
    case "findPlan":
      return {
        route: "found",
        plan: PLAN,
        documentation: "current",
        documents: ["docs/plan.md"],
        reason: "The plan was supplied by the calling workflow.",
        evidence: "workflow input",
      };
    case "implement":
      return undefined; // needs real edits; handled by the tool-call phase
    case "classifyImplementation":
      return { route: "verify", summary: "The fix is ready for tests.", evidence: "npm test passed locally." };
    case "planVerification": {
      const workspace = workspaceFromPrompt(promptText);
      const cwd = workspace?.worktreePath ?? workspace?.repository ?? options.fixtureRepository;
      return {
        commands: [{
          id: "npm-test",
          command: "npm",
          args: ["test"],
          cwd,
          timeoutMs: 120_000,
          maxOutputChars: 100_000,
        }],
        untested: [],
      };
    }
    case "planChecks": {
      const workspace = workspaceFromPrompt(promptText);
      const cwd = workspace?.worktreePath ?? workspace?.repository ?? options.fixtureRepository;
      return {
        checks: [{
          id: "syntax-check",
          command: "node",
          args: ["--check", "src/sum.js"],
          cwd,
          timeoutMs: 60_000,
          maxOutputChars: 100_000,
          readOnly: true,
          baseEligible: true,
          changedFileScope: false,
          findingFormat: "text",
        }],
      };
    }
    case "judge":
      return { route: "ready", reason: "The candidate passes and the base fails only on the defect under repair.", evidence: [] };
    case "publish":
      return undefined; // needs a real commit; handled by the tool-call phase
    case "assessReview": {
      const selected = selectedFromPrompt(promptText) ?? [];
      return {
        repositories: selected.map((repository) => ({
          id: repository.id,
          invocationSucceeded: true,
          p0: [],
          p1: [],
          p2: [],
          lower: [],
          reason: "The fixture reviewer reported no findings.",
        })),
        reason: "Clean review.",
      };
    }
    case "inspectComments":
      return { route: "ci", summary: "No pull request comments exist for the fixture.", evidence: [] };
    case "inspectCi": {
      const published = publishedFromPrompt(promptText);
      return {
        targets: (published?.repositories ?? []).map((repository) => ({
          repository: repository.repository,
          headRevision: repository.headRevision,
          pr: repository.pr,
          route: "green",
          reason: "The fixture has no CI; treat the local verification as the gate.",
          relatedFailures: [],
          unrelatedFailures: [],
        })),
      };
    }
    case "finalizeDelivery": {
      const published = publishedFromPrompt(promptText);
      const first = published?.repositories?.[0];
      return {
        status: "completed",
        merged: false,
        pr: first?.pr ?? "none",
        reportComment: "Fixture delivery left the branch ready without merging.",
        reason: "Workflow settings disable merge.",
        repositories: (published?.repositories ?? []).map((repository) => ({
          repository: repository.repository,
          pr: repository.pr,
          merged: false,
          reportComment: "Fixture delivery left the branch ready without merging.",
          reason: "merge disabled",
        })),
      };
    }
    default:
      throw new Error(`scripted model has no answer for workflow step ${contract.step} (scope ${scope})`);
  }
}

const STAGE_SCRIPTS = {
  updateDocumentation: (promptText) => {
    const workspace = workspaceFromPrompt(promptText);
    const root = workspace?.worktreePath ?? workspace?.repository;
    if (root === undefined) throw new Error("updateDocumentation prompt has no prepared workspace");
    return [
      {
        kind: "tool",
        toolName: "bash",
        args: {
          command: `mkdir -p ${JSON.stringify(`${root}/docs`)} && printf '%s\\n' '# Plan' '' 'Return a + b from sum(a, b) so the existing test passes.' > ${JSON.stringify(`${root}/docs/plan.md`)} && cd ${JSON.stringify(root)} && git add docs/plan.md && git -c user.name=orkastrator-fixture -c user.email=fixture@example.invalid commit -q -m 'docs: record the sum fix plan' && git rev-parse HEAD`,
        },
      },
      (contract) => submit(contract, {
        updated: true,
        files: ["docs/plan.md"],
        digests: { "docs/plan.md": "sha256:fixture" },
        summary: "Recorded the sum fix plan in docs/plan.md.",
      }),
    ];
  },
  implement: (promptText) => {
    const workspace = workspaceFromPrompt(promptText);
    const root = workspace?.worktreePath ?? workspace?.repository;
    if (root === undefined) throw new Error("implement prompt has no prepared workspace");
    return [
      {
        kind: "tool",
        toolName: "bash",
        args: {
          command: `cd ${JSON.stringify(root)} && sed -i 's/return a - b;/return a + b;/' src/sum.js && npm test --silent && git add src/sum.js && git -c user.name=orkastrator-fixture -c user.email=fixture@example.invalid commit -q -m 'fix: add instead of subtract in sum' && git rev-parse HEAD`,
        },
      },
      (contract) => submit(contract, {
        status: "implemented",
        summary: "sum(a, b) now returns a + b; npm test passes.",
        files: ["src/sum.js"],
        repositories: [root],
        issueKind: null,
        evidence: "npm test passed in the prepared worktree after the change.",
      }),
    ];
  },
  publish: (promptText) => {
    const workspace = workspaceFromPrompt(promptText);
    const root = workspace?.worktreePath ?? workspace?.repository;
    if (root === undefined) throw new Error("publish prompt has no prepared workspace");
    return [
      {
        kind: "tool",
        toolName: "bash",
        args: { command: `cd ${JSON.stringify(root)} && git rev-parse HEAD && git branch --show-current` },
      },
      (contract, toolOutput) => {
        const [head, branch] = toolOutput.trim().split("\n");
        if (!/^[0-9a-f]{40}$/u.test(head ?? "")) {
          throw new Error(`publish could not read the worktree head: ${toolOutput}`);
        }
        return submit(contract, {
          repositories: [{
            repository: root,
            branch,
            baseBranch: workspace.baseBranch,
            headRevision: head,
            pr: `fixture://pull/${head.slice(0, 8)}`,
            pushed: true,
          }],
        });
      },
    ];
  },
};

function lastToolOutput(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "tool") return lastMessageText([message]);
    if (message.role === "user") return "";
  }
  return "";
}

/** Name of the tool whose result is the last message, or undefined when the last message is not a tool result. */
function lastToolName(messages) {
  const last = messages.at(-1);
  if (last?.role !== "tool") return undefined;
  for (let index = messages.length - 2; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== "assistant") continue;
    const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
    const match = calls.find((call) => call?.id === last.tool_call_id) ?? calls.at(-1);
    return match?.function?.name;
  }
  return undefined;
}

/** True once the current attempt has already executed its staged shell command. */
function stageShellAlreadyRan(messages, attempt) {
  let seenAttempt = false;
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    const text = lastMessageText([message]);
    if (message.role === "user" && text.includes(`attempt: ${attempt})`)) {
      seenAttempt = true;
      continue;
    }
    if (!seenAttempt || message.role !== "assistant") continue;
    const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
    if (calls.some((call) => call?.function?.name === "bash")) return true;
  }
  return false;
}

function submissionOutcome(messages) {
  const output = lastToolOutput(messages);
  if (/accepted/iu.test(output)) return { kind: "text", text: "Step accepted." };
  throw new Error(`workflow tool rejected the submission: ${output.slice(0, 500)}`);
}

/**
 * Build the script closure. `options` carries the fixture repository path and
 * the task text so the answers stay consistent with the launched workflow.
 */
export function createCookLifecycleScript(options) {
  const seenNodes = [];
  const presentations = [];
  return {
    seenNodes,
    presentations,
    script: ({ messages, toolNames, lastRole, lastUserText }) => {
      // pi-subagents reviewer child: the strict structured output tool is present.
      if (toolNames.includes("structured_output")) {
        if (lastRole === "tool") return { kind: "text", text: "Review submitted." };
        return {
          kind: "tool",
          toolName: "structured_output",
          args: { value: { findings: [] } },
        };
      }

      const contract = latestStepContract(messages);
      if (contract === undefined) {
        // The `/kas:cook` launch turn: start the workflow exactly once.
        if (lastRole === "tool") return { kind: "text", text: "The Orkastrator workflow is running." };
        const start = /workflow=("[^"]+")/u.exec(lastUserText);
        const repositoryMatch = /repository: <absolute top-level path>/u.test(lastUserText);
        if (start !== null && repositoryMatch) {
          return {
            kind: "tool",
            toolName: "workflow",
            args: {
              action: "start",
              workflow: JSON.parse(start[1]),
              input: {
                task: options.task,
                repository: options.fixtureRepository,
                maxParallelFixers: 3,
                worktreeRetentionDays: 30,
              },
            },
          };
        }
        if (/Report the complete Orkastrator cook lifecycle result/u.test(lastUserText)
          || /Summarize what was implemented/u.test(lastUserText)
          || /Report the Orkastrator review workflow result/u.test(lastUserText)) {
          presentations.push(lastUserText.slice(0, 200));
          return { kind: "text", text: "Lifecycle presentation: the fixture change completed." };
        }
        return { kind: "text", text: "No workflow step is pending." };
      }

      const node = contract.step.split("/").at(-1);
      if (!seenNodes.includes(contract.step)) seenNodes.push(contract.step);
      const promptText = lastMessageText(messages.filter((message) => message.role === "user"));

      if (node === "summarize") {
        return { kind: "text", text: "The plan replaces subtraction with addition in src/sum.js so the existing test passes." };
      }

      // A workflow tool result ends the turn: accepted means done, anything else is a script bug.
      if (lastRole === "tool" && lastToolName(messages) === "workflow") return submissionOutcome(messages);

      const stage = STAGE_SCRIPTS[node];
      if (stage !== undefined) {
        const [toolCall, finish] = stage(promptText);
        if (!stageShellAlreadyRan(messages, contract.attempt)) return toolCall;
        const shellOutput = lastToolOutput(messages);
        if (lastRole === "tool" && lastToolName(messages) === "bash" && /exit code: [1-9]|command failed|error/iu.test(shellOutput) && !/^[0-9a-f]{40}/mu.test(shellOutput)) {
          throw new Error(`staged shell command for ${contract.step} failed: ${shellOutput.slice(0, 800)}`);
        }
        return finish(contract, shellOutput);
      }

      if (lastRole === "tool") {
        throw new Error(`unexpected ${lastToolName(messages) ?? "unknown"} tool result while serving ${contract.step}`);
      }
      const output = stepOutput(contract, promptText, options);
      if (output === undefined) throw new Error(`no scripted output for ${contract.step}`);
      return submit(contract, output);
    },
  };
}
