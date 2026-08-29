# Orkastrator Pi extension (v1)

This is the canonical Pi-native Orkastrator v1 implementation path. The existing Python/Orca controller is legacy pre-v1 code; it remains parallel during migration and is not imported or migrated here. Product v1 is separate from the legacy controller's YAML `version: 2` workflow schema.

## Loading the extension

A merged checkout includes `.pi/extensions/orkastrator.ts`, which Pi auto-discovers after the project is trusted:

```bash
npm install
pi --approve
```

Use `/reload` after editing extension source. `/kas-runs` confirms that the extension loaded. The private package manifest also exposes the extension for local `pi install <path>` development, but there is no published package yet.

## KAS-740 lifecycle slice

The extension currently owns only session lifecycle and durable local run evidence:

- trusted-project run creation through `orkastrator_run_create`;
- one nonterminal run per supervisor Pi session;
- append-first `events.jsonl` plus projected `state.json`;
- exact opaque effective-policy snapshot in `policy.yaml`;
- stale-run reporting without automatic claim, resume, cleanup, or deletion;
- same-session, same-process reload rebind when every recorded identity is proven;
- fail-closed interruption when reload continuity is missing or mismatched;
- resumable nonterminal `awaiting_owner` records and explicit owner answers;
- interruption on new, resume, fork, quit, SIGTERM, and SIGHUP session boundaries.

It does not launch workers, mutate Worktrunk state, recover after crashes, merge branches, or publish to ClickClack.

## Ledger layout

```text
~/.local/state/orkastrator/runs/<run-id>/
  state.json
  events.jsonl
  policy.yaml
```

`ORKASTRATOR_STATE_DIR` overrides the root for isolated tests; production uses the path above.

`events.jsonl` is authoritative. Every event contains its full resulting projection and records event ID, sequence, timestamp, rule, actor, evidence, and state transition. The ledger appends and fsyncs the event before atomically replacing `state.json`; replacement files, run directories, and the ledger root are fsynced. A crash-safe kernel `flock` serializes the active-run check and every mutation across Pi processes; the kernel releases ownership automatically when a writer exits or crashes. The implementation intentionally pays this durability cost for every event because the MVP has one local writer and low event volume.

On load, a final non-LF-terminated fragment is replaced atomically with the retained events plus one `ledger_tail_recovered` event recording the dropped byte count. Invalid complete records, malformed nested identity evidence, broken sequence continuity, cross-run events, policy tampering, symlink escapes, and a state sequence ahead of the event log fail loudly.

Pi session history and custom entries are status projections only. They are never required to reconstruct a run.

## Reload proof

`session_shutdown` with reason `reload` durably records:

- supervisor session ID;
- current ledger generation;
- Pi host PID;
- canonical repository root;
- recorded worker session files, PIDs, process groups, and attempt tokens;
- recorded repository and Worktrunk identities.

The replacement extension instance may rebind only when `session_start` also reports `reload`, session ID/generation/host PID/canonical repository root match exactly, and an injected identity verifier confirms every nonempty worker and worktree record. This KAS-740 slice has no workers, so the empty set can rebind. The default verifier rejects every nonempty resource set until KAS-743 supplies live identity checks. A missing marker, changed process, mismatch, or verifier error transitions the run to `interrupted` and preserves all evidence.

Normal startup after a crash never rebinds, even if the Pi session ID matches. It reports the run as stale and leaves it unchanged.

## Human wait policy

`awaiting_owner` has no automatic timeout in the MVP. The run remains nonterminal until an explicit allowed owner answer is recorded or the supervisor session shuts down. No model process is kept open merely to wait. The record stores the triggering rule, evidence, allowed decisions, wait start, answer, rationale, and resume state. Retention and cleanup remain explicit later policy actions.

## Extension surface

- `orkastrator_run_create`: creates the lifecycle-only run and snapshots caller-supplied policy bytes. KAS-742 replaces this temporary seam with strict repository-owned `repo-default` resolution.
- `orkastrator_owner_answer`: records an allowed answer for a run owned by the current session and resumes that same run.
- `/kas <objective>`: starts a model turn that creates the Pi-native lifecycle run directly through `orkastrator_run_create`; it never invokes the legacy `orkas` CLI or Orca.
- `/kas-runs`: reports the current session's run plus preserved runs from other sessions.

All surfaces require project trust. The package manifest exposes `index.ts` as a first-class Pi package extension, while `.pi/extensions/orkastrator.ts` is the repository-local trusted entrypoint.
