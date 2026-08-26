export const TASK_STATUSES = ["running", "completed", "failed", "killed"] as const;

export type TaskStatus = (typeof TASK_STATUSES)[number];

export interface MonitorTask {
  id: string;
  name?: string;
  command: string;
  status: TaskStatus;
  outputPath: string;
  startTime: number;
  endTime?: number;
  pid?: number;
  runId: string;
  stale: boolean;
  sourcePath: string;
}

export interface ParsedMonitorCommand {
  runId: string;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;
const TASK_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/u;
const UV_VALUE_OPTIONS = new Set([
  "--config-file",
  "--default-index",
  "--directory",
  "--env-file",
  "--index",
  "--project",
  "--python",
  "--with",
  "--with-editable",
]);
const UV_FLAG_OPTIONS = new Set([
  "--active",
  "--exact",
  "--frozen",
  "--isolated",
  "--locked",
  "--no-config",
  "--no-project",
  "--no-sync",
  "--offline",
]);

function basename(command: string): string {
  return command.split(/[\\/]/u).at(-1) ?? command;
}

/**
 * Tokenize the deliberately small shell subset emitted by bg_run. Expansion,
 * command substitution, operators, and newlines are rejected instead of
 * trying to emulate a shell.
 */
export function tokenizeCommand(command: string): string[] | undefined {
  const tokens: string[] = [];
  let token = "";
  let quote: "'" | '"' | undefined;
  let escaped = false;
  let started = false;

  for (const char of command.trim()) {
    if (escaped) {
      token += char;
      escaped = false;
      started = true;
      continue;
    }
    if (char === "\\" && quote !== "'") {
      escaped = true;
      started = true;
      continue;
    }
    if (quote !== undefined) {
      if (char === quote) quote = undefined;
      else {
        if (quote === '"' && (char === "$" || char === "`")) return undefined;
        token += char;
      }
      started = true;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      started = true;
      continue;
    }
    if (/\s/u.test(char)) {
      if (char === "\n" || char === "\r") return undefined;
      if (started) tokens.push(token);
      token = "";
      started = false;
      continue;
    }
    if (";&|<>$`".includes(char)) return undefined;
    token += char;
    started = true;
  }

  if (escaped || quote !== undefined) return undefined;
  if (started) tokens.push(token);
  return tokens.length > 0 ? tokens : undefined;
}

function orkastratorCommandIndex(tokens: readonly string[]): number | undefined {
  const direct = basename(tokens[0] ?? "");
  if (direct === "orkas" || direct === "orkastrator") return 0;
  if (basename(tokens[0] ?? "") !== "uv" || tokens[1] !== "run") return undefined;

  let index = 2;
  while (index < tokens.length) {
    const token = tokens[index] ?? "";
    const executable = basename(token);
    if (executable === "orkas" || executable === "orkastrator") return index;
    const equalsIndex = token.indexOf("=");
    const option = equalsIndex === -1 ? token : token.slice(0, equalsIndex);
    if (UV_FLAG_OPTIONS.has(option)) {
      if (equalsIndex !== -1) return undefined;
      index += 1;
      continue;
    }
    if (UV_VALUE_OPTIONS.has(option)) {
      if (equalsIndex !== -1) {
        if (token.slice(equalsIndex + 1).length === 0) return undefined;
        index += 1;
      } else {
        if (index + 1 >= tokens.length) return undefined;
        index += 2;
      }
      continue;
    }
    return undefined;
  }
  return undefined;
}

export function parseMonitorCommand(command: string): ParsedMonitorCommand | undefined {
  const tokens = tokenizeCommand(command);
  if (!tokens) return undefined;
  const commandIndex = orkastratorCommandIndex(tokens);
  if (commandIndex === undefined) return undefined;

  const args = tokens.slice(commandIndex + 1);
  if (args[0] !== "monitor" || !UUID_PATTERN.test(args[1] ?? "")) return undefined;
  let watch = false;
  for (let index = 2; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--watch") {
      watch = true;
      continue;
    }
    if (arg === "--json") continue;
    if (arg === "--interval") {
      const value = args[index + 1];
      if (!value || !/^\d+(?:\.\d+)?$/u.test(value) || Number(value) <= 0) return undefined;
      index += 1;
      continue;
    }
    return undefined;
  }
  return watch ? { runId: args[1]!.toLowerCase() } : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteNonNegative(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function positiveInteger(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : undefined;
}

export function parseMonitorTask(
  value: unknown,
  sourcePath: string,
  isProcessAlive: (pid: number) => boolean,
): MonitorTask | undefined {
  if (!isRecord(value)) return undefined;
  const id = value.id;
  const command = value.command;
  const status = value.status;
  const outputPath = value.outputPath;
  const startTime = finiteNonNegative(value.startTime);
  if (
    typeof id !== "string" ||
    !TASK_ID_PATTERN.test(id) ||
    typeof command !== "string" ||
    !TASK_STATUSES.includes(status as TaskStatus) ||
    typeof outputPath !== "string" ||
    outputPath.length === 0 ||
    startTime === undefined
  ) {
    return undefined;
  }
  const monitor = parseMonitorCommand(command);
  if (!monitor) return undefined;

  const pid = positiveInteger(value.pid);
  const endTime = finiteNonNegative(value.endTime);
  const name = typeof value.name === "string" && value.name.trim() ? value.name : undefined;
  const stale = status === "running" && (pid === undefined || !isProcessAlive(pid));
  return {
    id,
    ...(name === undefined ? {} : { name }),
    command,
    status: status as TaskStatus,
    outputPath,
    startTime,
    ...(endTime === undefined ? {} : { endTime }),
    ...(pid === undefined ? {} : { pid }),
    runId: monitor.runId,
    stale,
    sourcePath,
  };
}

function cleanLabel(value: string): string {
  return value.replace(/[\u0000-\u001f\u007f-\u009f]/gu, " ").replace(/\s+/gu, " ").trim();
}

function truncate(value: string, maxWidth: number): string {
  if (value.length <= maxWidth) return value;
  if (maxWidth <= 1) return value.slice(0, maxWidth);
  return `${value.slice(0, maxWidth - 1)}…`;
}

function monitorLabel(task: MonitorTask): string {
  return cleanLabel(task.name ?? "monitor") || "monitor";
}

export function liveMonitorTasks(tasks: readonly MonitorTask[]): MonitorTask[] {
  return tasks
    .filter((task) => task.status === "running" && !task.stale)
    .sort((left, right) => left.startTime - right.startTime || left.id.localeCompare(right.id));
}

export function renderMonitorFooter(tasks: readonly MonitorTask[], maxWidth = 72): string | undefined {
  if (maxWidth <= 0) return undefined;
  const live = liveMonitorTasks(tasks);
  if (live.length === 0) return undefined;
  const descriptions = live.map(
    (task) => `● ${monitorLabel(task)} ${task.runId.replaceAll("-", "").slice(0, 8)}`,
  );
  if (descriptions.length === 1) return truncate(descriptions[0]!, maxWidth);

  let rendered = "";
  for (let index = 0; index < descriptions.length; index += 1) {
    const remaining = descriptions.length - index - 1;
    const separator = rendered ? " · " : "";
    const suffix = remaining > 0 ? ` · +${remaining}` : "";
    const candidate = `${rendered}${separator}${descriptions[index]}`;
    if (`${candidate}${suffix}`.length <= maxWidth) {
      rendered = candidate;
      continue;
    }
    if (!rendered) {
      const descriptionWidth = Math.max(0, maxWidth - suffix.length);
      return `${truncate(descriptions[index]!, descriptionWidth)}${suffix}`.slice(0, maxWidth);
    }
    return truncate(`${rendered} · +${descriptions.length - index}`, maxWidth);
  }
  return truncate(rendered, maxWidth);
}

export function formatElapsed(task: MonitorTask, now = Date.now()): string | undefined {
  const stop = task.endTime ?? (task.status === "running" ? now : undefined);
  if (stop === undefined || stop < task.startTime) return undefined;
  const totalSeconds = Math.floor((stop - task.startTime) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(seconds).padStart(2, "0")}s`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

export function renderMonitorDetails(tasks: readonly MonitorTask[], now = Date.now()): string {
  const monitors = [...tasks].sort(
    (left, right) => right.startTime - left.startTime || left.id.localeCompare(right.id),
  );
  if (monitors.length === 0) return "No orkastrator monitor tasks found.";
  return monitors
    .map((task) => {
      const elapsed = formatElapsed(task, now);
      return [
        `task id: ${task.id}`,
        `run id: ${task.runId}`,
        `PID: ${task.pid ?? "unavailable"}`,
        `status: ${task.status}`,
        ...(elapsed === undefined ? [] : [`elapsed: ${elapsed}`]),
        `output: ${task.outputPath}`,
      ].join("\n");
    })
    .join("\n\n");
}
