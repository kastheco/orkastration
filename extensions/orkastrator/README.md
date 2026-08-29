# Orkastrator Pi extension (v1)

This is the canonical Pi-native Orkastrator v1 implementation path. The existing Python/Orca controller is legacy pre-v1 code; it remains parallel during migration and is not imported or migrated here. Product v1 is separate from the legacy controller's YAML `version: 2` workflow schema.

## Loading the extension

A merged checkout includes `.pi/extensions/orkastrator.ts`, which Pi auto-discovers after the project is trusted:

```bash
npm install
pi --approve
```

Use `/reload` after editing extension source. `/kas-runs` confirms that the extension loaded. The private package manifest also exposes the extension for local `pi install <path>` development, but there is no published package yet.

## KAS-740 and KAS-741 slice

The extension owns session lifecycle, durable local run evidence, and one fresh owned Pi RPC worker attempt:

- trusted-project run creation through `orkastrator_run_create`;
- one nonterminal run per supervisor Pi session;
- append-first `events.jsonl` plus projected `state.json`;
- exact opaque effective-policy snapshot in `policy.yaml`;
- stale-run reporting without automatic claim, resume, cleanup, or deletion;
- same-session, same-process reload rebind when every recorded identity is proven;
- fail-closed interruption when reload continuity is missing or mismatched;
- resumable nonterminal `awaiting_owner` records and explicit owner answers;
- interruption on new, resume, fork, quit, SIGTERM, and SIGHUP session boundaries;
- strict LF-only Pi RPC framing, correlated prompt acceptance, `agent_settled`, usage, and bounded stderr;
- PID, process group, unique session file, and attempt token recorded before the prompt is sent;
- AbortSignal cancellation with process-group SIGTERM/SIGKILL escalation and absence proof;
- shutdown waits for worker reap and durable ownership clear before recording interruption.

The worker currently runs in the trusted repository checkout. KAS-743 adds an isolated Worktrunk checkout and destructive-action identity checks. This slice does not implement policy reduction, review/fix waves, recovery, publication, merge, or ClickClack.

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

The replacement extension instance may rebind only when `session_start` also reports `reload`, session ID/generation/host PID/canonical repository root match exactly, and an injected identity verifier confirms every nonempty worker and worktree record. A completed or cancelled KAS-741 attempt clears its owned process before shutdown. The default verifier rejects any nonempty process or worktree set until KAS-743 supplies live identity checks. A missing marker, changed process, mismatch, or verifier error transitions the run to `interrupted` and preserves all evidence.

Normal startup after a crash never rebinds, even if the Pi session ID matches. It reports the run as stale and leaves it unchanged.

## Human wait policy

`awaiting_owner` has no automatic timeout in the MVP. The run remains nonterminal until an explicit allowed owner answer is recorded or the supervisor session shuts down. No model process is kept open merely to wait. The record stores the triggering rule, evidence, allowed decisions, wait start, answer, rationale, and resume state. Retention and cleanup remain explicit later policy actions.

## Extension surface

- `orkastrator_run_create`: creates the run, snapshots caller-supplied policy bytes, and runs one fresh owned Pi RPC worker attempt. KAS-742 replaces the temporary policy seam with strict repository-owned `repo-default` resolution.
- `orkastrator_owner_answer`: records an allowed answer for a run owned by the current session and resumes that same run.
- `/kas <objective>`: starts a model turn that creates the Pi-native lifecycle run directly through `orkastrator_run_create`; it never invokes the legacy `orkas` CLI or Orca.
- `/kas-runs`: reports the current session's run plus preserved runs from other sessions.

All surfaces require project trust. The package manifest exposes `index.ts` as a first-class Pi package extension, while `.pi/extensions/orkastrator.ts` is the repository-local trusted entrypoint.
