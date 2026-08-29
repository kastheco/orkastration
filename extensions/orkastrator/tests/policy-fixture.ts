import { readFileSync } from "node:fs";

/** Exact checked-in PolicyV1 bytes used by persistence fixtures. */
export const POLICY_SNAPSHOT = readFileSync(
  new URL("../../../orkastrator.v1.yaml", import.meta.url),
  "utf8",
);
