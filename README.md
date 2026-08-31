# Orkastrator

Orkastrator is an opinionated review-and-repair policy for Pi. It does not own a
workflow engine, process manager, ledger, or recovery system.

- [`@osolmaz/pi-workflows`](https://www.npmjs.com/package/@osolmaz/pi-workflows)
  owns durable runs, checkpoints, recovery, and workflow state.
- [`pi-subagents`](https://www.npmjs.com/package/pi-subagents) or
  [`pi-herdr-subagents`](https://github.com/brkastner/pi-herdr-subagents) runs
  reviewers and scoped fixers.
- Orkastrator supplies the policy: immutable findings, bounded write authority,
  parallel repair groups, scoped re-review, serial integration, and owner gates.

## Use it

Install Pi Workflows, Orkastrator, and exactly one subagent backend:

```bash
pi install npm:@osolmaz/pi-workflows
pi install npm:pi-subagents
pi install git:github.com/kastheco/orkastrator
```

To use Herdr instead, replace the `pi-subagents` line with the forked runner while
it awaits an upstream release:

```bash
pi install git:github.com/brkastner/pi-herdr-subagents@242437a
```

Orkastrator detects the backend through its non-launching delegation
capabilities. It refuses to start delegation when both backends are installed.
The `pi-subagents` backend uses its correlated event protocol. The Herdr backend uses
the fork's versioned global delegation API and public awaitable runner, and
requires a working Herdr installation.

Run Pi from a trusted Git repository, then choose the amount of ceremony:

```text
/kas <implementation request>
/kas:cook <implementation request>
/kas:check <review objective>
```

- `/kas` starts `orkastrator-implement.workflow.ts`. One durable workflow owns the
  implementation-ready plan, implementation, verification, delivery, committed
  review target, Orkastrator review, repair waves, and final result.
- `/kas:cook` starts `orkastrator-cook.workflow.ts`. It composes Pi Workflows'
  planning, canonical documentation, required operator approval, implementation,
  verification, delivery, and the complete Orkastrator review policy in one run.
- `/kas:check` starts `orkastrator-review.workflow.ts` against the repository's
  committed `HEAD`. It refuses to guess when the worktree is dirty.
- `/kas-runs` reports the active or most recent workflow visible to the current
  Pi session. It does not perform a name-filtered Orkastrator run lookup.

The commands address their packaged workflow by exact installed file path. The
command turn only resolves the repository and launches the workflow. It does not
run planning, implementation, grilling, or review stages outside the graph.

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

The implementation workflows compose their planning and implementation stages
with the review workflow. The review workflow executes this graph:

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
- novel non-fix-introduced finding: deferred until final reconciliation and
  blocks completion;
- an observation omitted by the scoped reviewer is not preserved as a structured
  finding.

## Proof

A live fixture produced two disjoint fixer groups in one parallel wave, re-reviewed
each exact commit, and integrated both serially at `a543512`. The first real run of the former review-only `/kas` command, now `/kas:check`,
then found and repaired three policy-boundary defects in `4e6f478`:
finding identity after sorting, deferred evidence across rejected rounds, and scope
enforcement across renames. The current suite passes 28 tests plus TypeScript
checking.

The composed `/kas` and `/kas:cook` workflows have static definition coverage,
package-inclusion tests, and passing TypeScript checks. They do not yet have a
live end-to-end dogfood run. Autoimplementation delivery currently happens
before the Orkastrator review stage, so repair commits integrated during review
are not automatically republished or sent through a second CI and delivery pass.

Durable architecture context lives in the
[orkastrator Notion page](https://app.notion.com/p/orkastrator-3c8b3a0a9c198166ab2bc9a3f9c1c3cb).
Tracked implementation history lives in the
[Linear project](https://linear.app/kashub/project/orkastrator-aae24ed01e8e).

## Files

```text
.pi/workflows/orkastrator-implement.workflow.ts
.pi/workflows/orkastrator-cook.workflow.ts
.pi/workflows/orkastrator-review.workflow.ts
extensions/orkastrator-workflows/index.ts
extensions/orkastrator-workflows/lifecycle-runtime.ts
extensions/orkastrator-workflows/delegation-bridge.ts
extensions/orkastrator-workflows/review-runtime.ts
extensions/orkastrator-workflows/review-wave.ts
extensions/orkastrator-workflows/worktree-retention.ts
```

The extension registers `/kas`, `/kas:cook`, `/kas:check`, `/kas-runs`, and the
bridge between Pi Workflows and the selected `pi-subagents` or
`pi-herdr-subagents` backend. The three workflow definitions own their complete
command lifecycles.

## Development

```bash
npm install
npm run typecheck
npm run test:extension
```

The old custom lifecycle, reducer, ledger, Worktrunk identity, RPC worker manager,
and monitor extension were removed at cutover. Git history remains the reference
for that implementation.
