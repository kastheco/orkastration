import { createHash } from "node:crypto";

export type FindingSeverity = "critical" | "high" | "medium" | "low" | "info";
export type FindingCategory =
  | "correctness"
  | "security"
  | "data_loss"
  | "scope"
  | "acceptance"
  | "style";

export interface InitialReviewFinding {
  id: string;
  severity: FindingSeverity;
  category: FindingCategory;
  contract: string;
  evidence: string[];
  implicatedPaths: string[];
  writablePaths: string[];
  blocking: boolean;
}

export interface FrozenFinding extends InitialReviewFinding {
  evidence: string[];
  implicatedPaths: string[];
  writablePaths: string[];
}

export interface FixerGroup {
  groupId: string;
  findings: FrozenFinding[];
  writablePaths: string[];
}

export interface ReviewWave {
  wave: number;
  groupIds: string[];
}

export interface FrozenReviewPlan {
  findings: FrozenFinding[];
  fixerGroups: FixerGroup[];
  waves: ReviewWave[];
}

export interface FixResult {
  groupId: string;
  findingIds: string[];
  baseSha: string;
  commitSha: string;
  changedPaths: string[];
  validationEvidence: string[];
}

export interface ReReviewPacket {
  groupId: string;
  findings: Array<{
    id: string;
    severity: FindingSeverity;
    category: Exclude<FindingCategory, "style">;
    contract: string;
    evidence: string[];
    implicatedPaths: string[];
    writablePaths: string[];
  }>;
  fixerDiff: {
    baseSha: string;
    commitSha: string;
    changedPaths: string[];
  };
  validationEvidence: string[];
}

const FINDING_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u;
const SHA = /^[0-9a-f]{40}$/u;
const DRIVE_PATH = /^[A-Za-z]:/u;
const MAX_FINDINGS = 1_000;
const MAX_EVIDENCE_ITEMS = 50;
const MAX_TEXT = 4_000;
const MAX_PATH = 512;
const MAX_REVIEW_OUTPUT_BYTES = 1_048_576;
const SEVERITIES = new Set<FindingSeverity>([
  "critical",
  "high",
  "medium",
  "low",
  "info",
]);
const BLOCKING_CATEGORIES = new Set<FindingCategory>([
  "correctness",
  "security",
  "data_loss",
  "scope",
  "acceptance",
]);

/** Parse one strict reviewer JSON response before freezing or dispatching any fixer. */
export function parseInitialReviewOutput(
  output: string,
  maxParallelFixers: number,
): FrozenReviewPlan {
  if (Buffer.byteLength(output, "utf8") > MAX_REVIEW_OUTPUT_BYTES) {
    throw new Error(`initial review output exceeds ${MAX_REVIEW_OUTPUT_BYTES} bytes`);
  }
  let value: unknown;
  try {
    value = JSON.parse(output);
  } catch {
    throw new Error("initial review output must be one JSON object");
  }
  assertExactObject(value, ["findings"], "initial review output");
  if (!Array.isArray(value.findings)) throw new Error("initial review findings must be an array");
  for (const [index, finding] of value.findings.entries()) {
    assertExactObject(
      finding,
      [
        "blocking",
        "category",
        "contract",
        "evidence",
        "id",
        "implicatedPaths",
        "severity",
        "writablePaths",
      ],
      `initial review finding ${index}`,
    );
  }
  return planReviewWaves(value.findings as InitialReviewFinding[], maxParallelFixers);
}

/** Freeze one immutable initial review and deterministically schedule disjoint fixer waves. */
export function planReviewWaves(
  findings: readonly InitialReviewFinding[],
  maxParallelFixers: number,
): FrozenReviewPlan {
  if (!Array.isArray(findings)) throw new Error("initial findings must be an array");
  if (!Number.isSafeInteger(maxParallelFixers) || maxParallelFixers < 1 || maxParallelFixers > 16) {
    throw new Error("maxParallelFixers must be an integer from 1 to 16");
  }
  if (findings.length > MAX_FINDINGS) throw new Error(`initial review exceeds ${MAX_FINDINGS} findings`);

  const ids = new Set<string>();
  const frozenFindings = findings.map((finding) => normalizeFinding(finding, ids))
    .sort((left, right) => left.id.localeCompare(right.id));
  const blocking = frozenFindings.filter((finding) => finding.blocking);
  const groups = clumpFindings(blocking);
  const waves: ReviewWave[] = [];
  for (let offset = 0; offset < groups.length; offset += maxParallelFixers) {
    waves.push({
      wave: waves.length + 1,
      groupIds: groups.slice(offset, offset + maxParallelFixers).map((group) => group.groupId),
    });
  }
  return deepFreeze({
    findings: frozenFindings,
    fixerGroups: groups,
    waves,
  });
}

/** Convert the frozen blocking set to the reducer's exact initial-review input. */
export function reducerFindings(plan: FrozenReviewPlan): Array<{
  id: string;
  category: Exclude<FindingCategory, "style">;
  groupId: string;
}> {
  const groupByFinding = new Map<string, string>();
  for (const group of plan.fixerGroups) {
    for (const finding of group.findings) groupByFinding.set(finding.id, group.groupId);
  }
  return plan.findings
    .filter((finding): finding is FrozenFinding & {
      category: Exclude<FindingCategory, "style">;
    } => finding.blocking)
    .map((finding) => ({
      id: finding.id,
      category: finding.category,
      groupId: groupByFinding.get(finding.id)!,
    }));
}

/** Validate one exact fixer commit and build the narrow mandatory re-review packet. */
export function buildReReviewPacket(
  plan: FrozenReviewPlan,
  result: FixResult,
): ReReviewPacket {
  const group = plan.fixerGroups.find((candidate) => candidate.groupId === result.groupId);
  if (group === undefined) throw new Error(`unknown fixer group ${result.groupId}`);
  const expectedIds = group.findings.map((finding) => finding.id);
  const actualIds = sortedUnique(result.findingIds, "fix result finding IDs", validateFindingId);
  if (!sameStrings(actualIds, expectedIds)) {
    throw new Error(`fix result findings do not match group ${result.groupId}`);
  }
  if (!SHA.test(result.baseSha) || !SHA.test(result.commitSha)) {
    throw new Error("fix result requires lowercase 40-character Git SHAs");
  }
  if (result.baseSha === result.commitSha) throw new Error("fix result commit must differ from base");
  const changedPaths = sortedUnique(result.changedPaths, "fix result paths", normalizePath);
  const writable = new Set(group.writablePaths);
  const escaped = changedPaths.find((path) => !writable.has(path));
  if (escaped !== undefined) throw new Error(`fix result changed path outside group scope: ${escaped}`);
  const validationEvidence = boundedStrings(
    result.validationEvidence,
    "validation evidence",
    MAX_EVIDENCE_ITEMS,
  );
  return deepFreeze({
    groupId: group.groupId,
    findings: group.findings.map((finding) => ({
      id: finding.id,
      severity: finding.severity,
      category: finding.category as Exclude<FindingCategory, "style">,
      contract: finding.contract,
      evidence: [...finding.evidence],
      implicatedPaths: [...finding.implicatedPaths],
      writablePaths: [...finding.writablePaths],
    })),
    fixerDiff: {
      baseSha: result.baseSha,
      commitSha: result.commitSha,
      changedPaths,
    },
    validationEvidence,
  });
}

function normalizeFinding(
  finding: InitialReviewFinding,
  ids: Set<string>,
): FrozenFinding {
  if (finding === null || typeof finding !== "object") {
    throw new Error("initial finding must be an object");
  }
  validateFindingId(finding.id);
  if (ids.has(finding.id)) throw new Error(`duplicate finding ID ${finding.id}`);
  ids.add(finding.id);
  if (!SEVERITIES.has(finding.severity)) {
    throw new Error(`unsupported finding severity ${String(finding.severity)}`);
  }
  if (!BLOCKING_CATEGORIES.has(finding.category) && finding.category !== "style") {
    throw new Error(`unsupported finding category ${String(finding.category)}`);
  }
  if (typeof finding.blocking !== "boolean") {
    throw new Error(`finding ${finding.id} blocking status must be boolean`);
  }
  if (finding.category === "style" ? finding.blocking : !finding.blocking) {
    throw new Error(`finding ${finding.id} has inconsistent category and blocking status`);
  }
  if (typeof finding.contract !== "string" || finding.contract.trim().length === 0) {
    throw new Error(`finding ${finding.id} contract is required`);
  }
  if (finding.contract.length > MAX_TEXT) throw new Error(`finding ${finding.id} contract is too long`);
  const evidence = boundedStrings(finding.evidence, `finding ${finding.id} evidence`, MAX_EVIDENCE_ITEMS);
  const implicatedPaths = sortedUnique(
    finding.implicatedPaths,
    `finding ${finding.id} paths`,
    normalizePath,
  );
  const writablePaths = sortedUnique(
    finding.writablePaths,
    `finding ${finding.id} writable paths`,
    normalizePath,
  );
  if (finding.blocking && implicatedPaths.length === 0) {
    throw new Error(`blocking finding ${finding.id} requires an implicated path`);
  }
  if (finding.blocking && writablePaths.length === 0) {
    throw new Error(`blocking finding ${finding.id} requires a writable path`);
  }
  return {
    id: finding.id,
    severity: finding.severity,
    category: finding.category,
    contract: finding.contract,
    evidence,
    implicatedPaths,
    writablePaths,
    blocking: finding.blocking,
  };
}

function clumpFindings(findings: FrozenFinding[]): FixerGroup[] {
  const parent = findings.map((_, index) => index);
  const find = (index: number): number => {
    let root = index;
    while (parent[root] !== root) root = parent[root]!;
    while (parent[index] !== index) {
      const next = parent[index]!;
      parent[index] = root;
      index = next;
    }
    return root;
  };
  const unite = (left: number, right: number): void => {
    const leftRoot = find(left);
    const rightRoot = find(right);
    if (leftRoot !== rightRoot) parent[Math.max(leftRoot, rightRoot)] = Math.min(leftRoot, rightRoot);
  };
  const ownerByPath = new Map<string, number>();
  findings.forEach((finding, index) => {
    for (const path of finding.writablePaths) {
      const owner = ownerByPath.get(path);
      if (owner === undefined) ownerByPath.set(path, index);
      else unite(owner, index);
    }
  });

  const byRoot = new Map<number, FrozenFinding[]>();
  findings.forEach((finding, index) => {
    const root = find(index);
    const group = byRoot.get(root) ?? [];
    group.push(finding);
    byRoot.set(root, group);
  });
  return [...byRoot.values()]
    .map((groupFindings) => {
      groupFindings.sort((left, right) => left.id.localeCompare(right.id));
      const writablePaths = [...new Set(groupFindings.flatMap((finding) => finding.writablePaths))]
        .sort((left, right) => left.localeCompare(right));
      const digest = createHash("sha256")
        .update(JSON.stringify({
          findingIds: groupFindings.map((finding) => finding.id),
          writablePaths,
        }))
        .digest("hex")
        .slice(0, 16);
      return {
        groupId: `fix-${digest}`,
        findings: groupFindings,
        writablePaths,
      };
    })
    .sort((left, right) => left.groupId.localeCompare(right.groupId));
}

function assertExactObject(
  value: unknown,
  expectedKeys: readonly string[],
  label: string,
): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  const keys = Object.keys(value).sort((left, right) => left.localeCompare(right));
  const expected = [...expectedKeys].sort((left, right) => left.localeCompare(right));
  if (!sameStrings(keys, expected)) throw new Error(`${label} has unknown or missing fields`);
}

function validateFindingId(value: string): string {
  if (typeof value !== "string" || !FINDING_ID.test(value)) {
    throw new Error(`invalid finding ID ${String(value)}`);
  }
  return value;
}

function normalizePath(value: string): string {
  if (
    typeof value !== "string"
    || value.length === 0
    || value.length > MAX_PATH
    || value.includes("\\")
    || value.includes("\0")
    || value.startsWith("/")
    || DRIVE_PATH.test(value)
  ) {
    throw new Error(`invalid repository-relative path ${String(value)}`);
  }
  const parts = value.split("/");
  if (parts.some((part) => part.length === 0 || part === "." || part === "..")) {
    throw new Error(`invalid repository-relative path ${value}`);
  }
  return parts.join("/");
}

function boundedStrings(values: string[], label: string, maximum: number): string[] {
  if (!Array.isArray(values) || values.length > maximum) {
    throw new Error(`${label} exceeds ${maximum} items`);
  }
  return values.map((value) => {
    if (typeof value !== "string" || value.trim().length === 0 || value.length > MAX_TEXT) {
      throw new Error(`${label} contains invalid text`);
    }
    return value;
  });
}

function sortedUnique(
  values: string[],
  label: string,
  normalize: (value: string) => string,
): string[] {
  if (!Array.isArray(values)) throw new Error(`${label} must be an array`);
  const normalized = values.map(normalize).sort((left, right) => left.localeCompare(right));
  if (new Set(normalized).size !== normalized.length) throw new Error(`${label} contains duplicates`);
  return normalized;
}

function sameStrings(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function deepFreeze<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const item of Object.values(value)) deepFreeze(item);
    Object.freeze(value);
  }
  return value;
}
