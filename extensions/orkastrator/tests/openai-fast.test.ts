import assert from "node:assert/strict";
import { test } from "node:test";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import openAiFast from "../rpc/openai-fast.ts";

type ProviderHandler = (event: { type: "before_provider_request"; payload: unknown }) => unknown;

function registeredHandler(): ProviderHandler {
  let handler: ProviderHandler | undefined;
  const pi = {
    on(event: string, value: ProviderHandler) {
      assert.equal(event, "before_provider_request");
      handler = value;
    },
  } as unknown as ExtensionAPI;
  openAiFast(pi);
  assert.ok(handler);
  return handler;
}

test("the pinned fast extension injects OpenAI priority without mutating the provider payload", () => {
  const handler = registeredHandler();
  const payload = { model: "gpt-5.6-sol", service_tier: "default", input: [] };
  const result = handler({ type: "before_provider_request", payload });
  assert.deepEqual(result, {
    model: "gpt-5.6-sol",
    service_tier: "priority",
    input: [],
  });
  assert.equal(payload.service_tier, "default");
});
