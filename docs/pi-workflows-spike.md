# Pi Workflows migration spike

## Decision

Use `@osolmaz/pi-workflows` as Orkastrator's durable control plane. Use `pi-subagents` only for bounded specialist execution that Pi Workflows does not expose as a general public node.

Do not continue building a second workflow engine, Pi RPC process manager, run ledger, or session lifecycle implementation unless a verified product requirement cannot be expressed through Pi Workflows.

The existing implementation remains untouched until a live spike proves the replacement path. It is reference code, not the target architecture.

## Implemented spike

The project now contains:

- `.pi/workflows/orkastrator-review.workflow.ts`: the durable review workflow graph
- `extensions/orkastrator-workflows/index.ts`: the lightweight Pi extension bridge
- `extensions/orkastrator-workflows/delegation-bridge.ts`: correlated `pi-subagents` delegation and cancellation
- `extensions/orkastrator-workflows/review-runtime.ts`: bounded review, fixer worktree, re-review, and integration actions
- `extensions/orkastrator/review-wave.ts`: strict finding contracts and deterministic clumping

The graph delegates state, checkpoints, recovery, durable output, source revision checks, and owner decisions to Pi Workflows.

## Proven in automated tests

The spike proves:

- strict workflow input and graph validation
- one immutable structured initial review
- deterministic transitive clumping by overlapping writable paths
- at most three concurrent disjoint fixer groups
- one detached Git worktree per fixer group
- one exact clean commit per fixer attempt
- rejection of changed paths outside the declared scope
- mandatory scoped re-review after every fixer attempt
- a maximum of two fix rounds
- deferral of re-review findings not introduced by the fix
- serial integration that preserves the accepted fixer commit in repository history
- idempotent adoption of already integrated commits when the action reruns
- one protected owner decision when blocking risk or integration conflict remains
- correlated cancellation through the public `pi-subagents` delegation event contract

## Architecture after the spike

```text
Pi Workflows
  graph, durable SQLite state, claims, fencing, parking/resume,
  human decisions, output delivery, source digests, viewer

pi-subagents
  configured reviewer and fixer leaf sessions, model routing,
  bounded tool use, structured output, cancellation

Orkastrator
  workflow definitions, review schemas, path-scope enforcement,
  fixer worktree adapter, integration policy
```

## Replaced Orkastrator components

These should be deleted after the live proof succeeds:

- custom run lifecycle and session rebind logic
- custom JSONL ledger and projected state
- custom policy reducer used only to advance the review graph
- owned Pi RPC process manager
- duplicate timeout, cancellation, and terminal-delivery machinery
- the large production extension entrypoint that coordinates those components

The review schema and deterministic grouping logic remain useful. Worktree identity checks may remain as a small adapter if Pi Workflows' built-in workspace preparation cannot cover group-scoped fixer worktrees.

## Known gaps

The spike is intentionally not production-ready yet.

1. **Standalone host compatibility.** The bridge depends on a loaded Pi extension and therefore works in an interactive Pi process. Pi Workflows' standalone host excludes project extensions from headless agent children and does not currently expose a general public child-agent action. Always-on execution needs either a supported host plugin seam or a public generic agent-group API from Pi Workflows.
2. **Action-level recovery granularity.** Git effects are observable and adopted after an interrupted action, but individual re-review results and rejected-round evidence are not separate Pi Workflows nodes. An interruption can repeat a reviewer call or a rejected fixer round.
3. **Fallback policy.** The confirmed alternate-model fallback boundary is not implemented in the spike.
4. **Conflict handling.** Integration conflicts stop and escalate. The supervisor does not yet resolve independently safe conflicts.
5. **Worktree retention.** Fixer worktrees are preserved for evidence. A verified finalizer and retention policy are still required.
6. **Live model availability.** The configured owner-selected model IDs must exist in the active Pi registry before a paid run.

## Local use

Install both Pi packages:

```sh
pi install npm:@osolmaz/pi-workflows
pi install npm:pi-subagents
```

Trust the project, commit the worker changes, and start the workflow from that clean worktree:

```sh
/workflow orkastrator-review --input-json {
  "objective": "the exact implementation objective",
  "repository": "/absolute/path/to/the/worktree",
  "reviewRevision": "40-character-lowercase-git-sha",
  "maxParallelFixers": 3
}
```

The workflow refuses a dirty worktree, a revision mismatch, unknown input fields, malformed review output, scope escape, multiple commits from one fixer attempt, or an unverified model result.

## Next proof

Run one no-cost fixture through the installed Pi extensions, then one owner-approved live task. The live proof must demonstrate:

- Pi Workflows run visibility in `piw`
- real structured delegation to `pi-subagents`
- two disjoint fixer worktrees active concurrently
- one rejected fix entering exactly one second round
- serial integration and an owner checkpoint
- interruption and resume without repeating a Git effect

Only after that proof should the replaced Orkastrator modules be removed.
