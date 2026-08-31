import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  DEFAULT_ORKASTRATOR_CONFIG,
  loadOrkastratorConfig,
  orkastratorConfigPath,
  parseOrkastratorConfig,
} from "../config.ts";

test("empty config retains every shipped model and thinking default", () => {
  assert.deepEqual(parseOrkastratorConfig({}), DEFAULT_ORKASTRATOR_CONFIG);
  assert.equal(DEFAULT_ORKASTRATOR_CONFIG.review.initial.model, "anthropic/claude-opus-5");
  assert.equal(DEFAULT_ORKASTRATOR_CONFIG.review.fixer.model, "openai-codex/gpt-5.6-terra");
  assert.equal(DEFAULT_ORKASTRATOR_CONFIG.review.reReview.model, "anthropic/claude-sonnet-5");
});

test("partial user config overrides only explicitly supplied values", () => {
  const config = parseOrkastratorConfig({
    review: {
      initial: { thinking: "high" },
      reReview: { model: "anthropic/claude-opus-5" },
    },
  });
  assert.deepEqual(config.review.initial, {
    model: "anthropic/claude-opus-5",
    thinking: "high",
  });
  assert.deepEqual(config.review.fixer, DEFAULT_ORKASTRATOR_CONFIG.review.fixer);
  assert.deepEqual(config.review.reReview, {
    model: "anthropic/claude-opus-5",
    thinking: "medium",
  });
});

test("missing config file uses shipped defaults", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-config-"));
  assert.deepEqual(
    await loadOrkastratorConfig(join(root, "missing.json")),
    DEFAULT_ORKASTRATOR_CONFIG,
  );
});

test("config file is parsed as a partial override", async () => {
  const root = await mkdtemp(join(tmpdir(), "orkastrator-config-"));
  const path = join(root, "config.json");
  await writeFile(path, JSON.stringify({ review: { fixer: { thinking: "xhigh" } } }));
  const config = await loadOrkastratorConfig(path);
  assert.equal(config.review.fixer.model, "openai-codex/gpt-5.6-terra");
  assert.equal(config.review.fixer.thinking, "xhigh");
});

test("config path supports XDG and explicit file overrides", () => {
  assert.equal(
    orkastratorConfigPath({ XDG_CONFIG_HOME: "/tmp/xdg" }),
    "/tmp/xdg/orkastrator/config.json",
  );
  assert.equal(
    orkastratorConfigPath({ ORKASTRATOR_CONFIG: "/tmp/custom.json" }),
    "/tmp/custom.json",
  );
});

test("unknown config fields fail closed", () => {
  assert.throws(
    () => parseOrkastratorConfig({ review: { initial: { provider: "anthropic" } } }),
    /unknown field provider/u,
  );
});
