import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";

export type OrkastratorThinkingLevel = "minimal" | "low" | "medium" | "high" | "xhigh" | "max";

export interface OrkastratorStageConfig {
  model: string;
  thinking: OrkastratorThinkingLevel;
}

export interface OrkastratorConfig {
  review: {
    initial: OrkastratorStageConfig;
    fixer: OrkastratorStageConfig;
    reReview: OrkastratorStageConfig;
  };
}

export const DEFAULT_ORKASTRATOR_CONFIG: OrkastratorConfig = Object.freeze({
  review: Object.freeze({
    initial: Object.freeze({ model: "anthropic/claude-opus-5", thinking: "medium" }),
    fixer: Object.freeze({ model: "openai-codex/gpt-5.6-terra", thinking: "medium" }),
    reReview: Object.freeze({ model: "anthropic/claude-sonnet-5", thinking: "medium" }),
  }),
});

const THINKING_LEVELS = new Set<OrkastratorThinkingLevel>([
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
]);

function requireObject(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function rejectUnknown(value: Record<string, unknown>, allowed: string[], label: string): void {
  const keys = new Set(allowed);
  const unknown = Object.keys(value).find((key) => !keys.has(key));
  if (unknown !== undefined) throw new Error(`${label} has unknown field ${unknown}`);
}

function parseStage(
  value: unknown,
  fallback: OrkastratorStageConfig,
  label: string,
): OrkastratorStageConfig {
  if (value === undefined) return { ...fallback };
  const stage = requireObject(value, label);
  rejectUnknown(stage, ["model", "thinking"], label);
  if (stage.model !== undefined && (typeof stage.model !== "string" || stage.model.trim().length === 0)) {
    throw new Error(`${label}.model must be a non-empty string`);
  }
  if (stage.thinking !== undefined && !THINKING_LEVELS.has(stage.thinking as OrkastratorThinkingLevel)) {
    throw new Error(`${label}.thinking must be minimal, low, medium, high, xhigh, or max`);
  }
  return {
    model: stage.model === undefined ? fallback.model : stage.model.trim(),
    thinking: stage.thinking === undefined
      ? fallback.thinking
      : stage.thinking as OrkastratorThinkingLevel,
  };
}

export function parseOrkastratorConfig(value: unknown): OrkastratorConfig {
  const config = requireObject(value, "Orkastrator config");
  rejectUnknown(config, ["review"], "Orkastrator config");
  const review = config.review === undefined
    ? {}
    : requireObject(config.review, "Orkastrator config.review");
  rejectUnknown(review, ["initial", "fixer", "reReview"], "Orkastrator config.review");
  return {
    review: {
      initial: parseStage(review.initial, DEFAULT_ORKASTRATOR_CONFIG.review.initial, "Orkastrator config.review.initial"),
      fixer: parseStage(review.fixer, DEFAULT_ORKASTRATOR_CONFIG.review.fixer, "Orkastrator config.review.fixer"),
      reReview: parseStage(review.reReview, DEFAULT_ORKASTRATOR_CONFIG.review.reReview, "Orkastrator config.review.reReview"),
    },
  };
}

export function orkastratorConfigPath(env: NodeJS.ProcessEnv = process.env): string {
  if (env.ORKASTRATOR_CONFIG !== undefined) {
    if (env.ORKASTRATOR_CONFIG.trim().length === 0) {
      throw new Error("ORKASTRATOR_CONFIG must not be empty");
    }
    return isAbsolute(env.ORKASTRATOR_CONFIG)
      ? env.ORKASTRATOR_CONFIG
      : resolve(env.ORKASTRATOR_CONFIG);
  }
  const configHome = env.XDG_CONFIG_HOME?.trim() || join(homedir(), ".config");
  return join(configHome, "orkastrator", "config.json");
}

export async function loadOrkastratorConfig(
  path = orkastratorConfigPath(),
): Promise<OrkastratorConfig> {
  let source: string;
  try {
    source = await readFile(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return parseOrkastratorConfig({});
    throw new Error(`could not read Orkastrator config ${path}: ${String(error)}`);
  }
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch (error) {
    throw new Error(`Orkastrator config ${path} is not valid JSON: ${String(error)}`);
  }
  try {
    return parseOrkastratorConfig(value);
  } catch (error) {
    throw new Error(`invalid Orkastrator config ${path}: ${String(error)}`);
  }
}
