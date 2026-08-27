import type {
  ExtensionAPI,
  ExtensionContext,
  RegisteredCommand,
  SessionShutdownEvent,
  SessionStartEvent,
} from "@earendil-works/pi-coding-agent";

import {
  type MonitorTask,
  renderMonitorDetails,
  renderMonitorFooter,
} from "./core.ts";
import { discoverMonitorTasks } from "./discovery.ts";

export const STATUS_KEY = "orkastrator-monitors";
export const POLL_INTERVAL_MS = 2_000;

export type MonitorExtensionContext = Pick<ExtensionContext, "cwd" | "isProjectTrusted"> & {
  ui: Pick<ExtensionContext["ui"], "notify" | "setStatus">;
};

export type MonitorExtensionHandler<Event> = (
  event: Event,
  ctx: MonitorExtensionContext,
) => Promise<void> | void;

export type MonitorCommandOptions = Omit<RegisteredCommand, "name" | "sourceInfo" | "handler"> & {
  handler: (args: string, ctx: MonitorExtensionContext) => Promise<void>;
};

export interface MonitorExtensionAPI {
  on(event: "session_start", handler: MonitorExtensionHandler<SessionStartEvent>): void;
  on(event: "session_shutdown", handler: MonitorExtensionHandler<SessionShutdownEvent>): void;
  registerCommand(name: string, options: MonitorCommandOptions): void;
}

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
  pi: MonitorExtensionAPI,
  dependencies: MonitorExtensionDependencies = {},
): void {
  const discover = dependencies.discover ?? discoverMonitorTasks;
  const timers = dependencies.timers ?? { setInterval, clearInterval };
  const now = dependencies.now ?? Date.now;
  let timer: ReturnType<typeof setInterval> | undefined;
  let currentContext: MonitorExtensionContext | undefined;
  let currentTasks: MonitorTask[] = [];
  let generation = 0;
  let inFlight: Promise<void> | undefined;
  let inFlightGeneration: number | undefined;

  const stop = (ctx?: MonitorExtensionContext): void => {
    generation += 1;
    const activeTimer = timer;
    timer = undefined;
    if (activeTimer !== undefined) {
      try {
        timers.clearInterval(activeTimer);
      } catch {
        // A host timer failure must not escape the lifecycle callback.
      }
    }
    const statusContext = ctx ?? currentContext;
    currentContext = undefined;
    currentTasks = [];
    try {
      statusContext?.ui.setStatus(STATUS_KEY, undefined);
    } catch {
      // The host may tear down the status pane before the extension does.
    }
  };

  const refresh = (ctx: MonitorExtensionContext): Promise<void> => {
    if (inFlight) {
      return inFlightGeneration === generation ? inFlight : inFlight.then(() => refresh(ctx));
    }
    const refreshGeneration = generation;
    const promise = (async () => {
      try {
        if (!ctx.isProjectTrusted()) {
          currentTasks = [];
          ctx.ui.setStatus(STATUS_KEY, undefined);
          return;
        }
        const tasks = await discover(ctx.cwd);
        if (generation !== refreshGeneration) return;
        currentContext = ctx;
        currentTasks = tasks;
        ctx.ui.setStatus(STATUS_KEY, renderMonitorFooter(tasks));
      } catch {
        // Discovery and host UI calls are best-effort for each refresh tick.
      }
    })();
    inFlight = promise;
    inFlightGeneration = refreshGeneration;
    const clearInFlight = (): void => {
      if (inFlight === promise) {
        inFlight = undefined;
        inFlightGeneration = undefined;
      }
    };
    void promise.then(clearInFlight, clearInFlight);
    return promise;
  };

  pi.on("session_start", async (_event, ctx) => {
    try {
      stop();
      currentContext = ctx;
      if (!ctx.isProjectTrusted()) {
        ctx.ui.setStatus(STATUS_KEY, undefined);
        return;
      }
      const sessionGeneration = generation;
      await refresh(ctx);
      if (generation !== sessionGeneration) return;
      timer = timers.setInterval(() => {
        void refresh(ctx).catch(() => undefined);
      }, POLL_INTERVAL_MS);
    } catch {
      stop(ctx);
    }
  });

  pi.on("session_shutdown", (_event, ctx) => {
    stop(ctx);
  });

  pi.registerCommand("orkastrator-monitors", {
    description: "Show recorded orkastrator monitor task details",
    handler: async (_args, ctx) => {
      try {
        if (!ctx.isProjectTrusted()) {
          ctx.ui.notify("Project trust is required to inspect .pi/tasks.", "warning");
          return;
        }
        currentContext = ctx;
        await refresh(ctx);
        ctx.ui.notify(renderMonitorDetails(currentTasks, now()), "info");
      } catch {
        // Command output is best-effort when the host UI is being torn down.
      }
    },
  });
}

export default function orkastratorMonitors(pi: ExtensionAPI): void {
  installOrkastratorMonitors(pi);
}
