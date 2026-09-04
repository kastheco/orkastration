import http from "node:http";

/**
 * Minimal OpenAI-compatible chat-completions server for lifecycle tests.
 *
 * The supplied script inspects each request and returns either a text reply or
 * one tool call. Pi's `openai-completions` API consumes the streamed result
 * exactly as it would from a hosted provider, so the surrounding runtime is
 * untouched: the workflow host, Pi RPC session, pi-subagents children, and the
 * Orkastrator broker all run for real. Only the model is scripted.
 */

function messageText(message) {
  if (message === undefined || message === null) return "";
  const content = message.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (part !== null && typeof part === "object" && "text" in part ? String(part.text) : ""))
      .join("\n");
  }
  return "";
}

function sseChunks(reply) {
  const base = {
    id: `chatcmpl-scripted-${Date.now()}`,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model: "scripted-model",
  };
  const chunks = [];
  if (reply.kind === "tool") {
    chunks.push({
      ...base,
      choices: [{
        index: 0,
        delta: {
          role: "assistant",
          tool_calls: [{
            index: 0,
            id: `call-${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
            type: "function",
            function: { name: reply.toolName, arguments: JSON.stringify(reply.args) },
          }],
        },
        finish_reason: null,
      }],
    });
    chunks.push({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }] });
  } else {
    chunks.push({
      ...base,
      choices: [{ index: 0, delta: { role: "assistant", content: reply.text }, finish_reason: null }],
    });
    chunks.push({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
  }
  const usage = { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 };
  const lines = chunks.map((chunk, index) =>
    `data: ${JSON.stringify(index === chunks.length - 1 ? { ...chunk, usage } : chunk)}\n\n`);
  lines.push("data: [DONE]\n\n");
  return lines;
}

/**
 * Start the server. `script` receives:
 *   { messages, tools, lastUserText, lastRole, systemText, toolNames }
 * and must return `{ kind: "text", text }` or `{ kind: "tool", toolName, args }`.
 * A thrown error becomes a 500 and is recorded in `errors` for diagnostics.
 */
export async function startScriptedModelServer(script, options = {}) {
  const requests = [];
  const errors = [];
  const log = options.log ?? (() => undefined);
  const server = http.createServer((req, res) => {
    if (req.method !== "POST" || !req.url?.includes("/chat/completions")) {
      res.writeHead(404).end();
      return;
    }
    let body = "";
    req.on("data", (chunk) => { body += chunk.toString("utf8"); });
    req.on("end", () => {
      let parsed;
      try {
        parsed = JSON.parse(body);
      } catch (error) {
        errors.push(`invalid request body: ${error instanceof Error ? error.message : String(error)}`);
        res.writeHead(400).end();
        return;
      }
      const messages = Array.isArray(parsed.messages) ? parsed.messages : [];
      const lastMessage = messages.at(-1);
      const lastUser = [...messages].reverse().find((message) => message.role === "user");
      const system = messages.find((message) => message.role === "system" || message.role === "developer");
      const toolNames = Array.isArray(parsed.tools)
        ? parsed.tools.map((tool) => tool?.function?.name).filter((name) => typeof name === "string")
        : [];
      const context = {
        messages,
        tools: parsed.tools ?? [],
        toolNames,
        lastUserText: messageText(lastUser),
        lastRole: lastMessage?.role ?? "",
        systemText: messageText(system),
      };
      let reply;
      try {
        reply = script(context);
        if (reply === undefined || reply === null) {
          throw new Error(`script returned no reply for last role ${context.lastRole}`);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        errors.push(message);
        log(`scripted model error: ${message}`);
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { message, type: "scripted_model_error" } }));
        return;
      }
      requests.push({
        at: new Date().toISOString(),
        lastRole: context.lastRole,
        toolNames,
        lastUserText: context.lastUserText.slice(0, 400),
        reply: reply.kind === "tool" ? { kind: "tool", toolName: reply.toolName } : { kind: "text" },
      });
      log(`scripted model reply: ${reply.kind === "tool" ? `tool ${reply.toolName}` : "text"}`);
      res.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "keep-alive",
      });
      for (const chunk of sseChunks(reply)) res.write(chunk);
      res.end();
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  return {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    requests,
    errors,
    close: () => new Promise((resolve, reject) => {
      server.closeAllConnections?.();
      server.close((error) => (error ? reject(error) : resolve()));
    }),
  };
}

/** Parse the trailing step contract Pi Workflows appends to every agent-node prompt. */
export function latestStepContract(messages) {
  // Only the most recent user turn can carry a live contract. Earlier ones in
  // the same session have already been settled or superseded.
  const lastUser = [...messages].reverse().find((message) => message.role === "user");
  const text = messageText(lastUser);
  const matches = [
    ...text.matchAll(/Workflow step contract \(workflow: ([^,]+), step: ([^,]+), attempt: ([A-Za-z0-9._-]+)\)/gu),
  ];
  const match = matches.at(-1);
  return match === undefined
    ? undefined
    : { workflow: match[1], step: match[2], attempt: match[3] };
}

export { messageText };
