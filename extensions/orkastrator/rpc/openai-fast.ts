import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Repository-pinned fast mode for owned OpenAI Codex workers. */
export default function openAiFast(pi: ExtensionAPI): void {
  pi.on("before_provider_request", (event) => {
    if (event.payload === null || typeof event.payload !== "object" || Array.isArray(event.payload)) {
      throw new Error("OpenAI Codex provider payload must be an object");
    }
    return { ...event.payload, service_tier: "priority" };
  });
}
