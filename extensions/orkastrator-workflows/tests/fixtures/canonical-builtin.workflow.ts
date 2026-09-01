import { join } from "node:path";
import { pathToFileURL } from "node:url";

import {
  compute,
  defineWorkflow,
  includeWorkflow,
} from "@osolmaz/pi-workflows";

const contractRoot = process.env.PIW_CONTRACT_PACKAGE_ROOT;
if (!contractRoot) throw new Error("PIW_CONTRACT_PACKAGE_ROOT is required");
const { autoimplementWorkflow } = await import(
  pathToFileURL(join(contractRoot, "dist/builtins/index.js")).href
);

export default defineWorkflow({
  source: import.meta.url,
  name: "canonical-builtin-fixture",
  startAt: "start",
  includes: {
    implementation: includeWorkflow({
      workflow: "builtin:autoimplement",
      contract: autoimplementWorkflow,
      input: () => ({ task: "fixture", plan: { steps: [] }, repository: process.cwd() }),
    }),
  },
  nodes: {
    start: compute({ run: () => ({}) }),
    completed: compute({ run: () => ({ status: "completed" }) }),
    blocked: compute({ run: () => ({ status: "blocked" }) }),
  },
  edges: [
    { from: "start", to: "implementation" },
    { from: "implementation.completed", to: "completed" },
    { from: "implementation.blocked", to: "blocked" },
  ],
});
