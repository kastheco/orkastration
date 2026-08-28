import assert from "node:assert/strict";
import { Readable } from "node:stream";
import { test } from "node:test";

import {
  attachJsonlLineReader,
  LfJsonlDecoder,
  serializeJsonLine,
} from "../rpc/jsonl.ts";

test("strict JSONL uses LF only and preserves Unicode separators inside JSON strings", () => {
  const decoder = new LfJsonlDecoder();
  const first = serializeJsonLine({ id: 1, text: "left\u2028middle\u2029right" });
  const second = `${JSON.stringify({ id: 2 })}\r\n`;

  assert.deepEqual(decoder.push(first + second), [
    JSON.stringify({ id: 1, text: "left\u2028middle\u2029right" }),
    JSON.stringify({ id: 2 }),
  ]);
  assert.deepEqual(decoder.end(), []);
});

test("strict JSONL rejects values that JSON.stringify cannot encode", () => {
  assert.throws(() => serializeJsonLine(undefined), /JSON-serializable/u);
  assert.throws(() => serializeJsonLine(() => undefined), /JSON-serializable/u);
  assert.throws(() => serializeJsonLine(Symbol("record")), /JSON-serializable/u);
});

test("strict JSONL preserves split UTF-8 code points and emits a final unterminated record", () => {
  const decoder = new LfJsonlDecoder();
  const record = Buffer.from(JSON.stringify({ text: "ready 🧭" }), "utf8");
  const emojiStart = record.indexOf(Buffer.from("🧭"));

  assert.deepEqual(decoder.push(record.subarray(0, emojiStart + 2)), []);
  assert.deepEqual(decoder.push(record.subarray(emojiStart + 2)), []);
  assert.deepEqual(decoder.end(), [JSON.stringify({ text: "ready 🧭" })]);
});

test("stream reader can be detached without Node readline semantics", async () => {
  const stream = new Readable({ read(): void {} });
  const lines: string[] = [];
  const detach = attachJsonlLineReader(stream, (line) => lines.push(line));

  stream.push(`${JSON.stringify({ text: "a\u2028b" })}\n`);
  await new Promise<void>((resolve) => setImmediate(resolve));
  detach();
  stream.push(`${JSON.stringify({ ignored: true })}\n`);
  stream.push(null);
  await new Promise<void>((resolve) => setImmediate(resolve));

  assert.deepEqual(lines, [JSON.stringify({ text: "a\u2028b" })]);
});
