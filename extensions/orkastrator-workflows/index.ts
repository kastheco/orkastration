import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { installDelegationBridge } from "./delegation-bridge.ts";

export function installOrkastratorWorkflows(pi: ExtensionAPI): void {
  const owner = {};
  const uninstall = installDelegationBridge(pi.events, owner);
  pi.on("session_shutdown", () => {
    uninstall();
  });
}

export default installOrkastratorWorkflows;
