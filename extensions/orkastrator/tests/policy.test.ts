import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { parse, stringify } from "yaml";

import { parsePolicyV1 } from "../policy.ts";

const exampleBytes = readFileSync(new URL("../../../orkastrator.v1.yaml", import.meta.url));
const example = parse(exampleBytes.toString("utf8")) as Record<string, unknown>;

function clonePolicy(): Record<string, unknown> {
  return structuredClone(example);
}

function bytes(value: unknown): Buffer {
  return Buffer.from(stringify(value), "utf8");
}

function objectAt(value: Record<string, unknown>, key: string): Record<string, unknown> {
  return value[key] as Record<string, unknown>;
}

function expectPath(value: Uint8Array, path: string): void {
  assert.throws(
    () => parsePolicyV1(value),
    (error: unknown) => error instanceof Error && (
      error.message.startsWith(`Policy ${path}:`)
      || error.message.startsWith(`Policy ${path}[`)
    ),
  );
}

test("the checked-in reduced policy parses as one deeply immutable exact value", () => {
  const policy = parsePolicyV1(exampleBytes);
  assert.equal(policy.version, 1);
  assert.deepEqual(policy.roles.worker, {
    model: "openai-codex/gpt-5.6-sol",
    thinking: "medium",
    fast: true,
  });
  assert.equal(policy.roles.initial_reviewer.fast, false);
  assert.equal(policy.roles.fixer.fast, true);
  assert.equal(policy.roles.re_reviewer.fast, false);
  assert.equal(Object.isFrozen(policy), true);
  assert.equal(Object.isFrozen(policy.roles), true);
  assert.equal(Object.isFrozen(policy.roles.worker), true);
  assert.equal(Object.isFrozen(policy.supervision.wake_on), true);
  assert.throws(() => {
    (policy.roles.worker as { fast: boolean }).fast = false;
  }, TypeError);
});

test("every top-level policy section is required", () => {
  for (const section of [
    "version",
    "roles",
    "review",
    "limits",
    "supervision",
    "validation",
    "completion",
  ]) {
    const value = clonePolicy();
    delete value[section];
    expectPath(bytes(value), `$.${section}`);
  }
});

test("all four execution roles are required", () => {
  for (const role of ["worker", "initial_reviewer", "fixer", "re_reviewer"]) {
    const value = clonePolicy();
    delete objectAt(value, "roles")[role];
    expectPath(bytes(value), `$.roles.${role}`);
  }
});

test("unknown keys are rejected recursively", () => {
  const cases: Array<[string, (value: Record<string, unknown>) => void]> = [
    ["$.unexpected", (value) => { value.unexpected = true; }],
    ["$.roles.unexpected", (value) => { objectAt(value, "roles").unexpected = {}; }],
    ["$.roles.worker.unexpected", (value) => {
      objectAt(objectAt(value, "roles"), "worker").unexpected = true;
    }],
    ["$.review.unexpected", (value) => { objectAt(value, "review").unexpected = true; }],
    ["$.limits.unexpected", (value) => { objectAt(value, "limits").unexpected = true; }],
    ["$.supervision.unexpected", (value) => { objectAt(value, "supervision").unexpected = true; }],
    ["$.validation.unexpected", (value) => { objectAt(value, "validation").unexpected = true; }],
    ["$.completion.unexpected", (value) => { objectAt(value, "completion").unexpected = true; }],
  ];
  for (const [path, mutate] of cases) {
    const value = clonePolicy();
    mutate(value);
    expectPath(bytes(value), path);
  }
});

test("nested required fields fail at their exact path", () => {
  const cases: Array<[string, string, string]> = [
    ["roles", "worker", "model"],
    ["review", "", "blocking"],
    ["limits", "", "total_tokens"],
    ["supervision", "", "human_wait"],
    ["validation", "", "profile"],
    ["completion", "", "integration"],
  ];
  for (const [section, child, field] of cases) {
    const value = clonePolicy();
    const parent = child.length === 0
      ? objectAt(value, section)
      : objectAt(objectAt(value, section), child);
    delete parent[field];
    expectPath(bytes(value), `$.${section}${child.length === 0 ? "" : `.${child}`}.${field}`);
  }
});

test("duplicate and malformed YAML identify a path without echoing source", () => {
  const duplicate = exampleBytes.toString("utf8").replace(
    "    fast: true\n  initial_reviewer:",
    "    fast: true\n    fast: false\n  initial_reviewer:",
  );
  expectPath(Buffer.from(duplicate), "$.roles.worker.fast");

  const secret = "do-not-echo-this-secret";
  let message = "";
  try {
    parsePolicyV1(Buffer.from(`version: [${secret}\n`, "utf8"));
  } catch (error) {
    message = (error as Error).message;
  }
  assert.match(message, /^Policy \$: malformed YAML/u);
  assert.equal(message.includes(secret), false);
});

test("wrong scalar types fail at the scalar path", () => {
  const cases: Array<[string, (value: Record<string, unknown>) => void]> = [
    ["$.version", (value) => { value.version = "1"; }],
    ["$.roles.worker.model", (value) => { objectAt(objectAt(value, "roles"), "worker").model = 1; }],
    ["$.roles.worker.thinking", (value) => { objectAt(objectAt(value, "roles"), "worker").thinking = true; }],
    ["$.roles.worker.fast", (value) => { objectAt(objectAt(value, "roles"), "worker").fast = "true"; }],
    ["$.review.max_fix_rounds_per_finding", (value) => {
      objectAt(value, "review").max_fix_rounds_per_finding = "2";
    }],
    ["$.limits.total_cost_usd", (value) => { objectAt(value, "limits").total_cost_usd = "10"; }],
    ["$.supervision.human_wait", (value) => { objectAt(value, "supervision").human_wait = false; }],
    ["$.validation.profile", (value) => { objectAt(value, "validation").profile = 1; }],
    ["$.completion.require_commit", (value) => { objectAt(value, "completion").require_commit = false; }],
  ];
  for (const [path, mutate] of cases) {
    const value = clonePolicy();
    mutate(value);
    expectPath(bytes(value), path);
  }
});

test("closed policy lists reject empty, duplicate, and unknown values", () => {
  const cases: Array<[string, string, unknown[]]> = [
    ["review", "blocking", []],
    ["review", "blocking", ["scope", "scope"]],
    ["review", "blocking", ["style"]],
    ["supervision", "wake_on", []],
    ["supervision", "wake_on", ["worker_blocked", "worker_blocked"]],
    ["supervision", "wake_on", ["unknown"]],
    ["supervision", "require_human_on", []],
    ["supervision", "require_human_on", ["policy_override", "policy_override"]],
    ["supervision", "require_human_on", ["unknown"]],
  ];
  for (const [section, field, list] of cases) {
    const value = clonePolicy();
    objectAt(value, section)[field] = list;
    expectPath(bytes(value), `$.${section}.${field}`);
  }
});

test("numeric policy bounds are finite, positive, and intentionally small", () => {
  const cases: Array<[string, number]> = [
    ["review.max_fix_rounds_per_finding", 0],
    ["review.max_fix_rounds_per_finding", 11],
    ["review.max_parallel_fixer_groups", 0],
    ["review.max_parallel_fixer_groups", 17],
    ["limits.wall_clock_minutes", 0],
    ["limits.wall_clock_minutes", 1_441],
    ["limits.max_worker_attempts", 0],
    ["limits.max_worker_attempts", 11],
    ["limits.total_tokens", 0],
    ["limits.total_tokens", 10_000_001],
    ["limits.total_cost_usd", 0],
    ["limits.total_cost_usd", 1_001],
  ];
  for (const [path, invalid] of cases) {
    const value = clonePolicy();
    const [section, field] = path.split(".") as [string, string];
    objectAt(value, section)[field] = invalid;
    expectPath(bytes(value), `$.${path}`);
  }

  const fractional = clonePolicy();
  objectAt(fractional, "limits").max_worker_attempts = 1.5;
  expectPath(bytes(fractional), "$.limits.max_worker_attempts");
});

test("fast mode is explicit and limited to the Pi-supported Codex provider", () => {
  const standardCodex = clonePolicy();
  objectAt(objectAt(standardCodex, "roles"), "worker").fast = false;
  assert.equal(parsePolicyV1(bytes(standardCodex)).roles.worker.fast, false);

  const unsupported = clonePolicy();
  objectAt(objectAt(unsupported, "roles"), "initial_reviewer").fast = true;
  expectPath(bytes(unsupported), "$.roles.initial_reviewer.fast");
});

test("invalid UTF-8 and empty policy bytes fail at the policy root", () => {
  expectPath(new Uint8Array(), "$");
  expectPath(Uint8Array.of(0xc3, 0x28), "$");
});
