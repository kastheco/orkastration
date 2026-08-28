import type { Readable } from "node:stream";
import { StringDecoder } from "node:string_decoder";

/** Serialize one strict LF-delimited JSONL record. */
export function serializeJsonLine(value: unknown): string {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new TypeError("JSONL records must be JSON-serializable values");
  }
  return `${serialized}\n`;
}

/**
 * Decode strict JSONL without treating U+2028 or U+2029 as record delimiters.
 * A trailing carriage return is accepted so CRLF senders remain compatible.
 */
export class LfJsonlDecoder {
  readonly #decoder = new StringDecoder("utf8");
  #buffer = "";

  push(chunk: Buffer | string): string[] {
    this.#buffer += typeof chunk === "string" ? chunk : this.#decoder.write(chunk);
    return this.#drainCompleteLines();
  }

  end(): string[] {
    this.#buffer += this.#decoder.end();
    const lines = this.#drainCompleteLines();
    if (this.#buffer.length > 0) {
      lines.push(this.#stripCarriageReturn(this.#buffer));
      this.#buffer = "";
    }
    return lines;
  }

  #drainCompleteLines(): string[] {
    const lines: string[] = [];
    while (true) {
      const newlineIndex = this.#buffer.indexOf("\n");
      if (newlineIndex === -1) return lines;
      lines.push(this.#stripCarriageReturn(this.#buffer.slice(0, newlineIndex)));
      this.#buffer = this.#buffer.slice(newlineIndex + 1);
    }
  }

  #stripCarriageReturn(line: string): string {
    return line.endsWith("\r") ? line.slice(0, -1) : line;
  }
}

/** Attach the strict decoder to a readable stream and return an unsubscribe function. */
export function attachJsonlLineReader(stream: Readable, onLine: (line: string) => void): () => void {
  const decoder = new LfJsonlDecoder();
  const onData = (chunk: Buffer | string): void => {
    for (const line of decoder.push(chunk)) onLine(line);
  };
  const onEnd = (): void => {
    for (const line of decoder.end()) onLine(line);
  };
  stream.on("data", onData);
  stream.on("end", onEnd);
  return () => {
    stream.off("data", onData);
    stream.off("end", onEnd);
  };
}
