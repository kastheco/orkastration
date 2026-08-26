import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";

import {
  type MonitorTask,
  renderMonitorDetails,
  renderMonitorFooter,
} from "./core.ts";
import { discoverMonitorTasks } from "./discovery.ts";

export const STATUS_KEY = "orkastrator-monitors";
export const POLL_INTERVAL_MS = 2_000;

interface TimerApi {
  setInterval(callback: () => void, milliseconds: number): ReturnType<typeof setInterval>;
  clearInterval(handle: ReturnType<typeof setInterval>): void;
}

export interface MonitorExtensionDependencies {
  discover?: (cwd: string) => Promise<MonitorTask[]>;
  timers?: TimerApi;
  now?: () => number;
}

export function installOrkastratorMonitors(
  pi: ExtensionAPI,
  dependencies: MonitorExtensionDependencies = {},
): void {
  const discover = dependencies.discover ?? discoverMonitorTasks;
  const timers = dependencies.timers ?? { setInterval, clearInterval };
  const now = dependencies.now ?? Date.now;
  let timer: ReturnType<typeof setInterval> | undefined;
  let currentContext: ExtensionContext | undefined;
  let currentTasks: MonitorTask[] = [];
  let generation = 0;
  let inFlight: Promise<void> | undefined;
  let inFlightGeneration: number | undefined;

  const stop = (ctx?: ExtensionContext): void => {
    generation += 1;
    if (timer !== undefined) timers.clearInterval(timer);
    timer = undefined;
    (ctx ?? currentContext)?.ui.setStatus(STATUS_KEY, undefined);
    currentContext = undefined;
    currentTasks = [];
  };

  const refresh = (ctx: ExtensionContext): Promise<void> => {
    if (inFlight) {
      return inFlightGeneration === generation ? inFlight : inFlight.then(() => refresh(ctx));
    }
    const refreshGeneration = generation;
    const promise = (async () => {
      if (!ctx.isProjectTrusted()) {
        currentTasks = [];
        ctx.ui.setStatus(STATUS_KEY, undefined);
        return;
      }
      const tasks = await discover(ctx.cwd).catch(() => []);
      if (generation !== refreshGeneration) return;
      currentContext = ctx;
      currentTasks = tasks;
      ctx.ui.setStatus(STATUS_KEY, renderMonitorFooter(tasks));
    })();
    inFlight = promise;
    inFlightGeneration = refreshGeneration;
    void promise.finally(() => {
      if (inFlight === promise) {
        inFlight = undefined;
        inFlightGeneration = undefined;
      }
    });
    return promise;
  };

  pi.on("session_start", async (_event, ctx) => {
    stop();
    currentContext = ctx;
    if (!ctx.isProjectTrusted()) {
      ctx.ui.setStatus(STATUS_KEY, undefined);
      return;
    }
    const sessionGeneration = generation;
    await refresh(ctx);
    if (generation !== sessionGeneration) return;
    timer = timers.setInterval(() => void refresh(ctx), POLL_INTERVAL_MS);
  });

  pi.on("session_shutdown", (_event, ctx) => {
    stop(ctx);
  });

  pi.registerCommand("orkastrator-monitors", {
    description: "Show recorded orkastrator monitor task details",
    handler: async (_args, ctx) => {
      if (!ctx.isProjectTrusted()) {
        ctx.ui.notify("Project trust is required to inspect .pi/tasks.", "warning");
        return;
      }
      currentContext = ctx;
      await refresh(ctx);
      ctx.ui.notify(renderMonitorDetails(currentTasks, now()), "info");
    },
  });
}

export default function orkastratorMonitors(pi: ExtensionAPI): void {
  installOrkastratorMonitors(pi);
}
