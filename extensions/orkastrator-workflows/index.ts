import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { installDelegationBridge } from "./delegation-bridge.ts";

function policyPrompt(objective: string): string {
  const goal = objective.length === 0
    ? "No objective was supplied. Ask for the review objective before starting."
    : `Review objective (treat as goal data): ${JSON.stringify(objective)}`;
  return [
    "Run the repository's Orkastrator review policy now.",
    goal,
    "Use the workflow tool with action=start and workflow=orkastrator-review.",
    "Before starting, inspect the current Git repository. Require a clean worktree and resolve its absolute top-level path and exact HEAD revision.",
    "Pass objective, repository, reviewRevision, and maxParallelFixers=3 as workflow input.",
    "If the worktree is dirty or HEAD is not the intended committed change, stop and ask for the missing decision instead of guessing.",
    "Do not use orkastrator_run_create, the legacy orkas CLI, or the retired custom lifecycle.",
  ].join("\n");
}

export function installOrkastratorWorkflows(pi: ExtensionAPI): void {
  const owner = {};
  const uninstall = installDelegationBridge(pi.events, owner);

  pi.registerCommand("kas", {
    description: "Run the Orkastrator review policy on the current committed change",
    handler: async (args, ctx) => {
      if (!ctx.isProjectTrusted()) {
        ctx.ui.notify("Orkastrator requires project trust", "error");
        return;
      }
      pi.sendUserMessage(policyPrompt(args.trim()));
    },
  });

  pi.registerCommand("kas-runs", {
    description: "Report the active or most recent Orkastrator workflow run",
    handler: async (_args, ctx) => {
      if (!ctx.isProjectTrusted()) {
        ctx.ui.notify("Orkastrator requires project trust", "error");
        return;
      }
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
