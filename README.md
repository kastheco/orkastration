# Orkastrator

Orkastrator is an opinionated review-and-repair policy for Pi. It does not own a
workflow engine, process manager, ledger, or recovery system.

- [`@osolmaz/pi-workflows`](https://www.npmjs.com/package/@osolmaz/pi-workflows)
  owns durable runs, checkpoints, recovery, and workflow state.
- [`pi-subagents`](https://www.npmjs.com/package/pi-subagents) runs reviewers and
  scoped fixers.
- Orkastrator supplies the policy: immutable findings, bounded write authority,
  parallel repair groups, scoped re-review, serial integration, and owner gates.

## Use it

Run Pi from a trusted Git repository with a clean, committed change, then use:

```text
/kas <review objective>
```

`/kas` is a shorthand for starting the `orkastrator-review` workflow. It resolves
the repository's exact committed `HEAD` and refuses to guess when the worktree is
dirty. `/kas-runs` reports the active or most recent workflow run.

The direct equivalent is:

```text
/workflow orkastrator-review --input-json {
  "objective": "preserve the parser contract",
  "repository": "/absolute/path/to/repository",
  "reviewRevision": "<40-character commit SHA>",
  "maxParallelFixers": 3
}
```

## Policy

The workflow executes this graph:

1. Run one strict initial review against an immutable commit.
2. Freeze finding IDs, contracts, evidence paths, and writable paths.
3. Group blocking findings by overlapping writable paths.
4. Run disjoint fixer groups in bounded parallel waves.
5. Reject any fixer that changes a path outside its assigned scope.
6. Re-review each exact fixer commit against its frozen contracts.
7. Integrate accepted commits serially onto the reviewed branch.
8. Stop for owner intervention when a group remains unresolved or a genuinely
   novel out-of-scope finding requires final reconciliation.

Evidence location is not write authority. Shared tests may support multiple
findings without forcing their source fixes into one group.

A finding observed during scoped re-review follows one of four routes:

- known sibling finding: handled by its existing fixer group;
- introduced by this fix: blocks the fixer;
- novel and relevant: blocks final completion for reconciliation;
- unrelated repository issue: preserved as evidence without widening the fix.

## Files

```text
.pi/workflows/orkastrator-review.workflow.ts
extensions/orkastrator-workflows/index.ts
extensions/orkastrator-workflows/delegation-bridge.ts
extensions/orkastrator-workflows/review-runtime.ts
extensions/orkastrator-workflows/review-wave.ts
```

The extension registers `/kas`, `/kas-runs`, and the bridge between Pi Workflows
and `pi-subagents`. The workflow definition remains the durable control graph.

## Development

```bash
npm install
npm run typecheck
npm run test:extension
```

The old custom lifecycle, reducer, ledger, Worktrunk identity, RPC worker manager,
and monitor extension were removed at cutover. Git history remains the reference
for that implementation.
