import type { Theme } from "@earendil-works/pi-coding-agent";
import { truncateToWidth } from "@earendil-works/pi-tui";
import type {
  LoadedWorkflowRun,
  WorkflowDefinitionSnapshot,
  WorkflowRunState,
  WorkflowStepRecord,
} from "@osolmaz/pi-workflows";

type OutlineChild = { nodeId: string; label?: string };
type StatusKind = "cancelled" | "complete" | "failed" | "queued" | "running" | "timed_out" | "waiting";
type OutlineStatus = { glyph: string; kind: StatusKind; label?: string };
type WidgetTheme = Pick<Theme, "bold" | "fg">;

const OUTCOMES: Record<string, OutlineStatus> = {
  ok: { glyph: "✓", kind: "complete" },
  failed: { glyph: "✗", kind: "failed", label: "failed" },
  timed_out: { glyph: "×", kind: "timed_out", label: "timed out" },
  cancelled: { glyph: "~", kind: "cancelled", label: "cancelled" },
};

export function renderWorkflowWidgetLines(
  bundle: LoadedWorkflowRun,
  width: number,
  theme: WidgetTheme,
  now = new Date(),
): string[] {
  try {
    return renderWorkflowWidgetLinesUnsafe(bundle, width, theme, now);
  } catch (error) {
    const message = error instanceof Error ? error.message : "unknown rendering error";
    return [truncateToWidth(
      theme.fg("error", `✗ workflow display unavailable · ${sanitizeText(message)}`),
      width,
      "…",
    )];
  }
}

function renderWorkflowWidgetLinesUnsafe(
  bundle: LoadedWorkflowRun,
  width: number,
  theme: WidgetTheme,
  now: Date,
): string[] {
  const state = bundle.state;
  const title = sanitizeText(state.runTitle ?? state.workflowName);
  if (isTerminalStatus(state.status)) {
    return renderTerminalWidget(state, title, theme).map((line) => truncateToWidth(line, width, "…"));
  }

  const elapsed = formatDuration(Math.max(0, now.getTime() - Date.parse(state.startedAt)));
  const status = runStatus(state.status);
  const unknownStatus = status.kind === "queued" && status.label !== undefined
    ? ` · ${status.label}`
    : "";
  const header = [
    `${paintStatusGlyph(status, theme)} ${theme.fg("accent", theme.bold(title))}`
      + theme.fg("dim", `${unknownStatus}  ${elapsed} · ${state.steps.length} step${state.steps.length === 1 ? "" : "s"}`),
  ];
  const outline = renderWorkflowOutline(bundle.snapshot, state, now, theme);
  return [...header, ...outline].map((line) => truncateToWidth(line, width, "…"));
}

export function renderWorkflowOutline(
  snapshot: WorkflowDefinitionSnapshot,
  state: WorkflowRunState,
  now = new Date(),
  theme?: WidgetTheme,
): string[] {
  const lines: string[] = [];
  const emitted = new Set<string>();

  const visit = (
    nodeId: string,
    prefix: string,
    connector: "" | "↓ " | "├─ " | "└─ ",
    edgeLabel: string | undefined,
    ancestry: Set<string>,
  ): void => {
    const branchText = edgeLabel === undefined ? "" : `${sanitizeText(edgeLabel)} → `;
    const guide = theme === undefined ? `${prefix}${connector}` : theme.fg("borderMuted", `${prefix}${connector}`);
    const branch = theme === undefined ? branchText : theme.fg("syntaxKeyword", branchText);
    if (ancestry.has(nodeId)) {
      lines.push(`${guide}${branch}${paintReference("↩", nodeId, theme)}`);
      return;
    }
    if (emitted.has(nodeId)) {
      lines.push(`${guide}${branch}${paintReference("↳", nodeId, theme)}`);
      return;
    }

    emitted.add(nodeId);
    lines.push(`${guide}${branch}${nodeLine(snapshot, state, nodeId, now, theme)}`);
    const children = outgoingChildren(snapshot, nodeId);
    const nextAncestry = new Set(ancestry).add(nodeId);
    const childPrefix = prefix + (connector === "├─ " ? "│  " : connector === "└─ " ? "   " : "");
    for (const [index, child] of children.entries()) {
      visit(
        child.nodeId,
        childPrefix,
        children.length === 1
          ? "↓ "
          : index === children.length - 1
            ? "└─ "
            : "├─ ",
        child.label,
        nextAncestry,
      );
    }
  };

  visit(snapshot.startAt, "", "", undefined, new Set());
  for (const nodeId of Object.keys(snapshot.nodes)) {
    if (!emitted.has(nodeId)) visit(nodeId, "", "", undefined, new Set());
  }
  return lines;
}

export function workflowReceiptLines(state: WorkflowRunState): string[] {
  const status = runStatus(state.status);
  const elapsedMs = Math.max(
    0,
    Date.parse(state.finishedAt ?? state.updatedAt) - Date.parse(state.startedAt),
  );
  const title = sanitizeText(state.runTitle ?? state.workflowName);
  const summary = workflowReceiptSummary(state);
  return [
    `${status.glyph} ${title} · ${status.label ?? state.status}`,
    `${formatDuration(elapsedMs)} · ${state.steps.length} step${state.steps.length === 1 ? "" : "s"}`,
    ...(summary === undefined ? [] : [sanitizeText(summary)]),
  ];
}

function renderTerminalWidget(
  state: WorkflowRunState,
  title: string,
  theme: WidgetTheme,
): string[] {
  const status = runStatus(state.status);
  const elapsedMs = Math.max(
    0,
    Date.parse(state.finishedAt ?? state.updatedAt) - Date.parse(state.startedAt),
  );
  const summary = workflowReceiptSummary(state);
  const heading = `${paintStatusGlyph(status, theme)} ${theme.bold(title)}`
    + theme.fg(statusColor(status.kind), `  ${status.label ?? state.status}`)
    + theme.fg("dim", ` · ${formatDuration(elapsedMs)} · ${state.steps.length} steps`);
  return [heading, ...(summary === undefined ? [] : [theme.fg("muted", `  ${sanitizeText(summary)}`)])];
}

function outgoingChildren(snapshot: WorkflowDefinitionSnapshot, nodeId: string): OutlineChild[] {
  return snapshot.edges.flatMap((edge) => {
    if (edge.from !== nodeId) return [];
    if ("to" in edge) return [{ nodeId: edge.to }];
    return Object.entries(edge.switch.cases).map(([label, target]) => ({ nodeId: target, label }));
  });
}

function nodeLine(
  snapshot: WorkflowDefinitionSnapshot,
  state: WorkflowRunState,
  nodeId: string,
  now: Date,
  theme?: WidgetTheme,
): string {
  const node = snapshot.nodes[nodeId];
  const attempts = state.steps.filter((step) => step.nodeId === nodeId);
  const latest = attempts.at(-1);
  const status = nodeStatus(state, nodeId, latest);
  const elapsed = nodeElapsed(state, nodeId, latest, now);
  const retry = attempts.length > 1 ? `${attempts.length}× · ` : "";
  const timing = elapsed === undefined ? "" : ` · ${retry}${formatDuration(elapsed)}`;
  const type = node?.nodeType ?? "unknown";
  const statusText = status.label === undefined ? "" : ` · ${status.label}`;
  const detail = state.currentNode === nodeId && state.statusDetail
    ? ` · ${sanitizeText(state.statusDetail)}`
    : "";
  if (theme === undefined) {
    return `${status.glyph} ${sanitizeText(nodeId)}  ${type}${statusText}${timing}${detail}`;
  }

  const glyph = paintStatusGlyph(status, theme);
  const name = status.kind === "running"
    ? theme.fg("accent", theme.bold(sanitizeText(nodeId)))
    : status.kind === "queued"
      ? theme.fg("muted", sanitizeText(nodeId))
      : sanitizeText(nodeId);
  const typeText = theme.fg(nodeTypeColor(type), type);
  const statusLabel = status.label === undefined
    ? ""
    : theme.fg(statusColor(status.kind), ` · ${status.label}`);
  const timingText = timing.length === 0 ? "" : theme.fg("dim", timing);
  const detailText = detail.length === 0 ? "" : theme.fg("muted", detail);
  return `${glyph} ${name}  ${typeText}${statusLabel}${timingText}${detailText}`;
}

function nodeStatus(
  state: WorkflowRunState,
  nodeId: string,
  latest: WorkflowStepRecord | undefined,
): OutlineStatus {
  if (state.currentNode === nodeId) return { glyph: "◐", kind: "running", label: "running" };
  if (state.waitingOn === nodeId) return { glyph: "⏸", kind: "waiting", label: "waiting" };
  if (latest === undefined) return { glyph: "·", kind: "queued" };
  return OUTCOMES[latest.outcome] ?? { glyph: "✗", kind: "failed", label: "failed" };
}

function runStatus(status: unknown): OutlineStatus {
  const statuses: Record<WorkflowRunState["status"], OutlineStatus> = {
    completed: { glyph: "✓", kind: "complete", label: "complete" },
    failed: { glyph: "✗", kind: "failed", label: "failed" },
    timed_out: { glyph: "×", kind: "timed_out", label: "timed out" },
    cancelled: { glyph: "~", kind: "cancelled", label: "cancelled" },
    running: { glyph: "◐", kind: "running", label: "running" },
    waiting: { glyph: "⏸", kind: "waiting", label: "waiting" },
  };
  if (typeof status === "string" && status in statuses) {
    return statuses[status as WorkflowRunState["status"]];
  }
  return {
    glyph: "·",
    kind: "queued",
    label: typeof status === "string" && status.trim().length > 0
      ? sanitizeText(status)
      : "unknown",
  };
}

function nodeElapsed(
  state: WorkflowRunState,
  nodeId: string,
  latest: WorkflowStepRecord | undefined,
  now: Date,
): number | undefined {
  if (state.currentNode === nodeId) {
    return Math.max(0, now.getTime() - Date.parse(state.currentNodeStartedAt ?? state.updatedAt));
  }
  if (latest === undefined) return undefined;
  return Math.max(0, Date.parse(latest.finishedAt) - Date.parse(latest.startedAt));
}

function paintStatusGlyph(status: OutlineStatus, theme: WidgetTheme): string {
  return theme.fg(statusColor(status.kind), status.glyph);
}

function paintReference(glyph: string, nodeId: string, theme?: WidgetTheme): string {
  const text = `${glyph} ${sanitizeText(nodeId)}`;
  return theme === undefined ? text : theme.fg("dim", text);
}

function statusColor(kind: StatusKind): Parameters<WidgetTheme["fg"]>[0] {
  if (kind === "complete") return "success";
  if (kind === "failed" || kind === "timed_out") return "error";
  if (kind === "cancelled" || kind === "waiting") return "warning";
  if (kind === "running") return "accent";
  return "dim";
}

function nodeTypeColor(type: string): Parameters<WidgetTheme["fg"]>[0] {
  if (type === "action") return "warning";
  if (type === "compute") return "syntaxFunction";
  if (type === "checkpoint") return "syntaxKeyword";
  if (type === "agent") return "success";
  if (type === "notify") return "syntaxString";
  return "muted";
}

function workflowReceiptSummary(state: WorkflowRunState): string | undefined {
  if (state.error) return state.error;
  if (state.finalOutput !== null && typeof state.finalOutput === "object" && !Array.isArray(state.finalOutput)) {
    const output = state.finalOutput as Record<string, unknown>;
    if (typeof output.reason === "string" && output.reason.trim().length > 0) return output.reason;
    if (typeof output.status === "string" && output.status.trim().length > 0) {
      return output.status.replaceAll("_", " ");
    }
  }
  if (state.status === "completed") return "all steps completed";
  return undefined;
}

function isTerminalStatus(status: WorkflowRunState["status"]): boolean {
  return status === "completed" || status === "failed" || status === "timed_out" || status === "cancelled";
}

function formatDuration(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1_000));
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function sanitizeText(value: string): string {
  return value
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/gu, "")
    .replace(/[\r\n]+/gu, " ");
}
