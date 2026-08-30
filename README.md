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

Install the three Pi packages once:

```bash
pi install npm:@osolmaz/pi-workflows
pi install npm:pi-subagents
pi install git:github.com/kastheco/orkastrator
```

Run Pi from a trusted Git repository, then choose the amount of ceremony:

```text
/kas <implementation request>
/kas:cook <implementation request>
/kas:check <review objective>
```

- `/kas` explicitly runs the installed `implement` skill. It skips
  `grill-with-docs`, doing only the planning or clarification the request actually
  needs, then automatically runs `/kas:check` after the implementation is tested,
  reviewed, and committed.
- `/kas:cook` explicitly runs Matt Pocock's `grill-with-docs` skill across as many
  user turns as needed. Once the plan and domain docs are accepted, it runs the
  installed `implement` skill and then automatically runs `/kas:check`.
- `/kas:check` starts the packaged `orkastrator-review.workflow.ts` by its exact
  installed file path against the repository's committed `HEAD`. It doesn't rely
  on project/global workflow-name discovery and refuses to guess when the
  worktree is dirty.
- `/kas-runs` reports the active or most recent review workflow run.

`/kas` requires `/skill:implement`. `/kas:cook` also requires
`/skill:grill-with-docs`; missing skills fail closed before work starts.

The direct `/kas:check` equivalent is:

```text
/workflow /absolute/path/to/orkastrator/.pi/workflows/orkastrator-review.workflow.ts --input-json {
  "objective": "preserve the parser contract",
  "repository": "/absolute/path/to/repository",
  "reviewRevision": "<40-character commit SHA>",
  "maxParallelFixers": 3,
  "worktreeRetentionDays": 30
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

Fixer worktrees are locked while a run owns them. A completed, fully integrated
fix is unlocked and scheduled for cleanup after 30 days by default. Future
reviews remove only runtime-marked worktrees whose exact commit remains merged,
whose worktree is clean and unchanged, and which have no active process using
them. Unresolved, dirty, active, unmarked, and cross-repository worktrees remain
preserved. `worktreeRetentionDays` may be set from 1 to 365 days.

A finding observed during scoped re-review follows one of four routes:

- known sibling finding: handled by its existing fixer group;
- introduced by this fix: blocks the fixer;
- novel and relevant: blocks final completion for reconciliation;
- unrelated repository issue: preserved as evidence without widening the fix.

## Proof

A live fixture produced two disjoint fixer groups in one parallel wave, re-reviewed
each exact commit, and integrated both serially at `a543512`. The first real `/kas`
dogfood run then found and repaired three policy-boundary defects in `4e6f478`:
finding identity after sorting, deferred evidence across rejected rounds, and scope
enforcement across renames. The current suite passes 26 tests plus TypeScript
checking.

Durable architecture context lives in the
[orkastrator Notion page](https://app.notion.com/p/orkastrator-3c8b3a0a9c198166ab2bc9a3f9c1c3cb).
Tracked implementation history lives in the
[Linear project](https://linear.app/kashub/project/orkastrator-aae24ed01e8e).

## Files

```text
.pi/workflows/orkastrator-review.workflow.ts
extensions/orkastrator-workflows/index.ts
extensions/orkastrator-workflows/delegation-bridge.ts
extensions/orkastrator-workflows/review-runtime.ts
extensions/orkastrator-workflows/review-wave.ts
extensions/orkastrator-workflows/worktree-retention.ts
```

The extension registers `/kas`, `/kas:cook`, `/kas:check`, `/kas-runs`, and the
bridge between Pi Workflows and `pi-subagents`. The workflow definition remains
the durable review control graph.

## Development

```bash
npm install
npm run typecheck
npm run test:extension
```

The old custom lifecycle, reducer, ledger, Worktrunk identity, RPC worker manager,
and monitor extension were removed at cutover. Git history remains the reference
for that implementation.
