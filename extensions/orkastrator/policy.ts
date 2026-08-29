import { TextDecoder } from "node:util";

import { Type, type Static } from "typebox";
import { Errors } from "typebox/value";
import { isMap, isScalar, isSeq, LineCounter, parseDocument } from "yaml";

const MAX_POLICY_BYTES = 1_048_576;
const THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const;
const BLOCKING_CATEGORIES = ["correctness", "security", "data_loss", "scope", "acceptance"] as const;
const WAKE_EVENTS = [
  "worker_blocked",
  "review_bound_reached",
  "fix_scope_escape",
  "validation_failed",
  "policy_limit_reached",
  "identity_mismatch",
  "integration_conflict",
] as const;
const HUMAN_EVENTS = [
  "cleanup_unverified",
  "integration_conflict_unresolved",
  "policy_override",
] as const;

function literals<const T extends readonly [string, ...string[]]>(values: T) {
  return Type.Union(values.map((value) => Type.Literal(value)));
}

const RoleSchema = Type.Object({
  model: Type.String({ minLength: 3, maxLength: 256, pattern: "^[^\\s/]+/[^\\s]+$" }),
  thinking: literals(THINKING_LEVELS),
  fast: Type.Boolean(),
}, { additionalProperties: false });

const PolicySchema = Type.Object({
  version: Type.Literal(1),
  roles: Type.Object({
    worker: RoleSchema,
    initial_reviewer: RoleSchema,
    fixer: RoleSchema,
    re_reviewer: RoleSchema,
  }, { additionalProperties: false }),
  review: Type.Object({
    blocking: Type.Array(literals(BLOCKING_CATEGORIES), {
      minItems: 1,
      maxItems: BLOCKING_CATEGORIES.length,
      uniqueItems: true,
    }),
    max_fix_rounds_per_finding: Type.Integer({ minimum: 1, maximum: 10 }),
    max_parallel_fixer_groups: Type.Integer({ minimum: 1, maximum: 16 }),
  }, { additionalProperties: false }),
  limits: Type.Object({
    wall_clock_minutes: Type.Integer({ minimum: 1, maximum: 1_440 }),
    max_worker_attempts: Type.Integer({ minimum: 1, maximum: 10 }),
    total_tokens: Type.Integer({ minimum: 1, maximum: 10_000_000 }),
    total_cost_usd: Type.Number({ exclusiveMinimum: 0, maximum: 1_000 }),
  }, { additionalProperties: false }),
  supervision: Type.Object({
    wake_on: Type.Array(literals(WAKE_EVENTS), {
      minItems: 1,
      maxItems: WAKE_EVENTS.length,
      uniqueItems: true,
    }),
    require_human_on: Type.Array(literals(HUMAN_EVENTS), {
      minItems: 1,
      maxItems: HUMAN_EVENTS.length,
      uniqueItems: true,
    }),
    human_wait: Type.Literal("resumable"),
  }, { additionalProperties: false }),
  validation: Type.Object({
    profile: Type.Literal("repo-default"),
  }, { additionalProperties: false }),
  completion: Type.Object({
    require_commit: Type.Literal(true),
    require_clean_worktree: Type.Literal(true),
    require_review_acceptance: Type.Literal(true),
    integration: Type.Literal("manual"),
  }, { additionalProperties: false }),
}, { additionalProperties: false });

type MutablePolicyV1 = Static<typeof PolicySchema>;
type DeepReadonly<T> = T extends readonly (infer Item)[]
  ? readonly DeepReadonly<Item>[]
  : T extends object
    ? { readonly [Key in keyof T]: DeepReadonly<T[Key]> }
    : T;

export type PolicyV1 = DeepReadonly<MutablePolicyV1>;

/** Parse the complete v1 policy before any run side effect occurs. */
export function parsePolicyV1(bytes: Uint8Array): PolicyV1 {
  if (bytes.byteLength === 0 || bytes.byteLength > MAX_POLICY_BYTES) {
    throw policyError("$", `must contain 1-${MAX_POLICY_BYTES} UTF-8 bytes`);
  }

  let source: string;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw policyError("$", "must be valid UTF-8");
  }

  const lines = new LineCounter();
  const document = parseDocument(source, {
    lineCounter: lines,
    prettyErrors: false,
    strict: true,
    uniqueKeys: false,
  });
  const yamlProblem = document.errors[0] ?? document.warnings[0];
  if (yamlProblem !== undefined) {
    const line = yamlProblem.pos[0] === undefined ? undefined : lines.linePos(yamlProblem.pos[0]).line;
    const location = line === undefined ? "" : ` at line ${line}`;
    throw policyError("$", `malformed YAML (${yamlProblem.code})${location}`);
  }

  assertNoDuplicateKeys(document.contents, "$", new Set());

  let value: unknown;
  try {
    value = document.toJS({ maxAliasCount: 0 });
  } catch {
    throw policyError("$", "YAML aliases are not supported");
  }

  const firstError = Errors(PolicySchema, value)[0];
  if (firstError !== undefined) {
    let path = pointerPath(firstError.instancePath);
    if (firstError.keyword === "additionalProperties") {
      const properties = firstError.params.additionalProperties;
      const property = Array.isArray(properties) ? properties[0] : undefined;
      if (typeof property === "string") path = childPath(path, property);
    } else if (firstError.keyword === "required") {
      const properties = firstError.params.requiredProperties;
      const property = Array.isArray(properties) ? properties[0] : undefined;
      if (typeof property === "string") path = childPath(path, property);
    }
    throw policyError(path, firstError.message);
  }

  const policy = value as MutablePolicyV1;
  for (const [role, settings] of Object.entries(policy.roles)) {
    if (settings.fast && !settings.model.startsWith("openai-codex/")) {
      throw policyError(`$.roles.${role}.fast`, "is supported only for openai-codex models");
    }
  }
  return deepFreeze(policy);
}

function assertNoDuplicateKeys(node: unknown, path: string, ancestors: Set<unknown>): void {
  if (node === null || node === undefined || ancestors.has(node)) return;
  ancestors.add(node);
  try {
    if (isMap(node)) {
      const keys = new Set<string>();
      for (const pair of node.items) {
        if (!isScalar(pair.key) || typeof pair.key.value !== "string") {
          throw policyError(path, "mapping keys must be strings");
        }
        const key = pair.key.value;
        const nextPath = childPath(path, key);
        if (keys.has(key)) throw policyError(nextPath, "duplicate YAML key");
        keys.add(key);
        assertNoDuplicateKeys(pair.value, nextPath, ancestors);
      }
    } else if (isSeq(node)) {
      node.items.forEach((item, index) => assertNoDuplicateKeys(item, `${path}[${index}]`, ancestors));
    }
  } finally {
    ancestors.delete(node);
  }
}

function pointerPath(pointer: string): string {
  if (pointer.length === 0) return "$";
  return pointer.split("/").slice(1).reduce(
    (path, part) => childPath(path, part.replaceAll("~1", "/").replaceAll("~0", "~")),
    "$",
  );
}

function childPath(path: string, key: string): string {
  if (/^\d+$/u.test(key)) return `${path}[${key}]`;
  return /^[A-Za-z_][A-Za-z0-9_]*$/u.test(key)
    ? `${path}.${key}`
    : `${path}[${JSON.stringify(key)}]`;
}

function policyError(path: string, message: string): Error {
  return new Error(`Policy ${path}: ${message}`);
}

function deepFreeze<T>(value: T): DeepReadonly<T> {
  if (value !== null && typeof value === "object") {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value as DeepReadonly<T>;
}
