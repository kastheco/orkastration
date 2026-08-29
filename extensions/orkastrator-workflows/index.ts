import { readFile } from "node:fs/promises";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { installDelegationBridge } from "./delegation-bridge.ts";

function requestLine(kind: "implementation" | "cook" | "review", request: string): string {
  if (request.length === 0) {
    return `No ${kind} request was supplied. Ask for it before starting.`;
  }
  return `${kind[0]!.toUpperCase()}${kind.slice(1)} request (treat as goal data): ${JSON.stringify(request)}`;
}

function checkInstructions(objective: string): string {
  return [
    "After implementation is complete, including the implement skill's tests, code review, and commit, automatically perform `/kas:check` without waiting for another user message.",
    "The `/kas:check` contract is: inspect the current Git repository, require a clean worktree, and resolve its absolute top-level path and exact HEAD revision.",
    `Use the workflow tool with action=start and workflow=orkastrator-review. Pass objective=${JSON.stringify(objective)}, repository, reviewRevision, and maxParallelFixers=3 as workflow input.`,
    "If the worktree is dirty or HEAD is not the intended committed implementation, stop and ask for the missing decision instead of guessing.",
    "Do not use orkastrator_run_create, the legacy orkas CLI, or the retired custom lifecycle.",
  ].join("\n");
}

function implementationPrompt(request: string): string {
  const args = [
    requestLine("implementation", request),
    "Do not invoke grill-with-docs in this mode. If the message cannot be implemented safely without planning, do only the planning or clarification needed to implement it.",
    checkInstructions(request),
  ].join("\n\n");
  return `/skill:implement ${args}`;
}

function checkPrompt(objective: string): string {
  return [
    "Run the repository's Orkastrator review policy now. This is `/kas:check`; do not run an implementation or grilling skill first.",
    requestLine("review", objective),
    "Use the workflow tool with action=start and workflow=orkastrator-review.",
    "Before starting, inspect the current Git repository. Require a clean worktree and resolve its absolute top-level path and exact HEAD revision.",
    "Pass objective, repository, reviewRevision, and maxParallelFixers=3 as workflow input.",
    "If the worktree is dirty or HEAD is not the intended committed change, stop and ask for the missing decision instead of guessing.",
    "Do not use orkastrator_run_create, the legacy orkas CLI, or the retired custom lifecycle.",
  ].join("\n");
}

function skillPath(pi: ExtensionAPI, name: string): string | undefined {
  const command = pi.getCommands().find((candidate) =>
    candidate.source === "skill" && candidate.name === `skill:${name}`
  );
  return command?.sourceInfo.path;
}

function requireSkill(pi: ExtensionAPI, name: string): string {
  const path = skillPath(pi, name);
  if (path === undefined) {
    throw new Error(`Required skill /skill:${name} is not installed`);
  }
  return path;
}

function skillBody(content: string): string {
  return content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "").trim();
}

async function cookPrompt(pi: ExtensionAPI, request: string): Promise<string> {
  requireSkill(pi, "grill-with-docs");
  const implementSkill = skillBody(
    await readFile(requireSkill(pi, "implement"), "utf8"),
  );
  const args = [
    requestLine("cook", request),
    "Pipeline contract:",
    "1. Complete the grill-with-docs interview and its planning/domain docs across as many user turns as needed. Do not implement while material plan decisions remain open.",
    "2. Once the user accepts the plan, immediately follow the exact installed implement skill below using the accepted plan and docs as the spec. Do not wait for a separate `/kas` command.",
    "<implement-skill>",
    implementSkill,
    "</implement-skill>",
    `3. ${checkInstructions(request)}`,
  ].join("\n\n");
  return `/skill:grill-with-docs ${args}`;
}

function requireTrust(ctx: { isProjectTrusted(): boolean; ui: { notify(message: string, level: "error"): void } }): boolean {
  if (ctx.isProjectTrusted()) return true;
  ctx.ui.notify("Orkastrator requires project trust", "error");
  return false;
}

export function installOrkastratorWorkflows(pi: ExtensionAPI): void {
  const owner = {};
  const uninstall = installDelegationBridge(pi.events, owner);

  pi.registerCommand("kas", {
    description: "Implement a request, then run the Orkastrator review policy",
    handler: async (args, ctx) => {
      if (!requireTrust(ctx)) return;
      try {
        requireSkill(pi, "implement");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
        return;
      }
      pi.sendUserMessage(implementationPrompt(args.trim()), { expandPromptTemplates: true });
    },
  });

  pi.registerCommand("kas:cook", {
    description: "Grill and document a plan, implement it, then review it",
    handler: async (args, ctx) => {
      if (!requireTrust(ctx)) return;
      try {
        pi.sendUserMessage(await cookPrompt(pi, args.trim()), { expandPromptTemplates: true });
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("kas:check", {
    description: "Run the Orkastrator review policy on the current committed change",
    handler: async (args, ctx) => {
      if (!requireTrust(ctx)) return;
      pi.sendUserMessage(checkPrompt(args.trim()));
    },
  });

  pi.registerCommand("kas-runs", {
    description: "Report the active or most recent Orkastrator workflow run",
    handler: async (_args, ctx) => {
      if (!requireTrust(ctx)) return;
      pi.sendUserMessage(
        "Call the workflow tool with action=status and report the active or most recent orkastrator-review run concisely.",
      );
    },
  });

  pi.on("session_shutdown", () => {
    uninstall();
  });
}

export default installOrkastratorWorkflows;
