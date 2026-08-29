import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildReReviewPacket,
  type InitialReviewFinding,
  parseInitialReviewOutput,
  planReviewWaves,
} from "../review-wave.ts";

function finding(
  id: string,
  paths: string[],
  overrides: Partial<InitialReviewFinding> = {},
): InitialReviewFinding {
  return {
    id,
    severity: "high",
    category: "correctness",
    contract: `Fix ${id} without changing unrelated behavior.`,
    evidence: [`Evidence for ${id}`],
    implicatedPaths: paths,
    writablePaths: paths,
    blocking: true,
    ...overrides,
  };
}

test("strict reviewer JSON is the only accepted initial review envelope", () => {
  const output = JSON.stringify({
    findings: [finding("finding-a", ["src/a.ts"])],
  });
  const plan = parseInitialReviewOutput(output, 3);
  assert.equal(plan.findings[0]?.id, "finding-a");

  assert.throws(
    () => parseInitialReviewOutput(`\`\`\`json\n${output}\n\`\`\``, 3),
    /one JSON object/u,
  );
  assert.throws(
    () => parseInitialReviewOutput(JSON.stringify({ findings: [], summary: "extra" }), 3),
    /unknown or missing fields/u,
  );
  assert.throws(
    () => parseInitialReviewOutput(JSON.stringify({ findings: [{
      ...finding("finding-b", ["src/b.ts"]),
      extra: true,
    }] }), 3),
    /unknown or missing fields/u,
  );
});

test("initial findings freeze once and overlap clumps transitively before bounded waves", () => {
  const input = [
    finding("finding-a", ["src/a.ts", "src/shared.ts"]),
    finding("finding-b", ["src/shared.ts", "src/bridge.ts"]),
    finding("finding-c", ["src/bridge.ts", "src/c.ts"]),
    finding("finding-d", ["src/d.ts"]),
    finding("finding-e", ["src/e.ts"]),
    finding("style-note", [], {
      severity: "low",
      category: "style",
      contract: "Optional naming cleanup.",
      blocking: false,
    }),
  ];

  const first = planReviewWaves(input, 2);
  const reordered = planReviewWaves([...input].reverse(), 2);

  assert.deepEqual(reordered, first);
  assert.equal(Object.isFrozen(first), true);
  assert.equal(Object.isFrozen(first.findings), true);
  assert.equal(first.findings.length, 6);
  assert.equal(first.fixerGroups.length, 3);
  const overlapping = first.fixerGroups.find((group) =>
    group.findings.some((item) => item.id === "finding-a")
  );
  assert.deepEqual(
    overlapping?.findings.map((item) => item.id),
    ["finding-a", "finding-b", "finding-c"],
  );
  assert.deepEqual(overlapping?.writablePaths, [
    "src/a.ts",
    "src/bridge.ts",
    "src/c.ts",
    "src/shared.ts",
  ]);
  assert.deepEqual(first.waves.map((wave) => wave.groupIds.length), [2, 1]);
  assert.equal(first.waves.every((wave) => wave.groupIds.length <= 2), true);
  assert.equal(
    first.fixerGroups.some((group) =>
      group.findings.some((item) => item.id === "style-note")
    ),
    false,
  );

  input[0]!.contract = "mutated caller value";
  input[0]!.implicatedPaths.push("src/escape.ts");
  input[0]!.writablePaths.push("src/write-escape.ts");
  assert.notEqual(first.findings[0]?.contract, "mutated caller value");
  assert.equal(
    first.findings.some((item) => item.implicatedPaths.includes("src/escape.ts")),
    false,
  );
  assert.equal(
    first.findings.some((item) => item.writablePaths.includes("src/write-escape.ts")),
    false,
  );
});

test("shared evidence paths do not clump disjoint writable scopes", () => {
  const plan = planReviewWaves([
    finding("finding-a", ["src/a.ts", "test/shared.test.ts"], {
      writablePaths: ["src/a.ts"],
    }),
    finding("finding-b", ["src/b.ts", "test/shared.test.ts"], {
      writablePaths: ["src/b.ts"],
    }),
  ], 2);

  assert.equal(plan.fixerGroups.length, 2);
  assert.deepEqual(
    plan.fixerGroups.map((group) => group.writablePaths).sort(),
    [["src/a.ts"], ["src/b.ts"]],
  );
});

test("re-review packet contains only the frozen contracts, exact diff, and validation evidence", () => {
  const plan = planReviewWaves([
    finding("finding-a", ["src/a.ts", "src/shared.ts"], {
      category: "data_loss",
      evidence: ["The original write can truncate data."],
    }),
    finding("finding-b", ["src/shared.ts"]),
  ], 3);
  const group = plan.fixerGroups[0]!;
  const packet = buildReReviewPacket(plan, {
    groupId: group.groupId,
    findingIds: ["finding-b", "finding-a"],
    baseSha: "a".repeat(40),
    commitSha: "b".repeat(40),
    changedPaths: ["src/shared.ts", "src/a.ts"],
    validationEvidence: ["unit tests passed", "scope hash matched"],
  });

  assert.deepEqual(packet, {
    groupId: group.groupId,
    findings: group.findings.map((item) => ({
      id: item.id,
      severity: item.severity,
      category: item.category,
      contract: item.contract,
      evidence: item.evidence,
      implicatedPaths: item.implicatedPaths,
      writablePaths: item.writablePaths,
    })),
    fixerDiff: {
      baseSha: "a".repeat(40),
      commitSha: "b".repeat(40),
      changedPaths: ["src/a.ts", "src/shared.ts"],
    },
    validationEvidence: ["unit tests passed", "scope hash matched"],
  });
  assert.equal(Object.isFrozen(packet), true);
  assert.equal(Object.isFrozen(packet.findings), true);
  assert.equal(Object.isFrozen(packet.fixerDiff.changedPaths), true);

  assert.throws(
    () => buildReReviewPacket(plan, {
      groupId: group.groupId,
      findingIds: group.findings.map((item) => item.id),
      baseSha: "a".repeat(40),
      commitSha: "b".repeat(40),
      changedPaths: ["src/outside.ts"],
      validationEvidence: [],
    }),
    /outside group scope/u,
  );
  assert.throws(
    () => buildReReviewPacket(plan, {
      groupId: group.groupId,
      findingIds: ["finding-a"],
      baseSha: "a".repeat(40),
      commitSha: "b".repeat(40),
      changedPaths: ["src/a.ts"],
      validationEvidence: [],
    }),
    /do not match group/u,
  );
});

for (const [name, value, expected] of [
  ["duplicate IDs", [finding("same", ["a"]), finding("same", ["b"])], /duplicate finding ID/u],
  ["path escape", [finding("escape", ["../secret"])], /repository-relative path/u],
  ["absolute path", [finding("absolute", ["/tmp/secret"])], /repository-relative path/u],
  ["backslash path", [finding("backslash", ["src\\file.ts"])], /repository-relative path/u],
  ["blocking style", [finding("style", ["src/a.ts"], { category: "style" })], /inconsistent/u],
  [
    "unknown severity",
    [finding("severity", ["src/a.ts"], {
      severity: "urgent" as InitialReviewFinding["severity"],
    })],
    /unsupported finding severity/u,
  ],
  ["nonblocking correctness", [finding("bug", ["src/a.ts"], { blocking: false })], /inconsistent/u],
  ["empty blocking scope", [finding("empty", [])], /requires an implicated path/u],
  [
    "empty writable scope",
    [finding("empty-write", ["src/a.ts"], { writablePaths: [] })],
    /requires a writable path/u,
  ],
] as const) {
  test(`initial review rejects ${name}`, () => {
    assert.throws(() => planReviewWaves(value, 3), expected);
  });
}

test("parallel cap and commit identity are bounded", () => {
  assert.throws(() => planReviewWaves([], 0), /maxParallelFixers/u);
  assert.throws(() => planReviewWaves([], 17), /maxParallelFixers/u);
  const plan = planReviewWaves([finding("one", ["src/a.ts"])], 1);
  const group = plan.fixerGroups[0]!;
  assert.throws(
    () => buildReReviewPacket(plan, {
      groupId: group.groupId,
      findingIds: ["one"],
      baseSha: "not-a-sha",
      commitSha: "b".repeat(40),
      changedPaths: ["src/a.ts"],
      validationEvidence: [],
    }),
    /Git SHAs/u,
  );
  assert.throws(
    () => buildReReviewPacket(plan, {
      groupId: group.groupId,
      findingIds: ["one"],
      baseSha: "a".repeat(40),
      commitSha: "a".repeat(40),
      changedPaths: ["src/a.ts"],
      validationEvidence: [],
    }),
    /must differ/u,
  );
});
