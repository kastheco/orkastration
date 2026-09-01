<table>
  <tr>
    <td width="260" align="center">
      <img src="docs/assets/orkastrator.png" alt="ukiyo-e puppet master controlling puppets on strings" width="220">
    </td>
    <td>
      <h1>orkastrator</h1>
      <p><strong>plan, implement, review, and repair with bounded agents.</strong></p>
      <p><a href="https://github.com/kastheco/orkastrator/actions/workflows/pr-checks.yml"><img src="https://github.com/kastheco/orkastrator/actions/workflows/pr-checks.yml/badge.svg" alt="ci"></a></p>
    </td>
  </tr>
</table>

orkastrator is an opinionated software delivery policy and workflow suite for pi. it covers planning, implementation, review, and bounded repair. it doesn't own the workflow engine, process manager, ledger, or recovery system underneath those flows.

- [`@osolmaz/pi-workflows`](https://www.npmjs.com/package/@osolmaz/pi-workflows) owns durable runs, checkpoints, recovery, and workflow state.
- [`pi-subagents`](https://www.npmjs.com/package/pi-subagents) or [`pi-herdr-subagents`](https://github.com/brkastner/pi-herdr-subagents) runs reviewers and scoped fixers.
- orkastrator supplies the delivery workflows and policy: planning and implementation composition, immutable findings, bounded write authority, parallel repair groups, scoped re-review, serial integration, and owner gates.

## install and run

install pi workflows, orkastrator, and exactly one subagent backend:

```bash
pi install npm:@osolmaz/pi-workflows
pi install npm:pi-subagents
pi install git:github.com/kastheco/orkastrator
```

to use herdr instead, replace the `pi-subagents` line with the forked runner while it awaits an upstream release:

```bash
pi install git:github.com/brkastner/pi-herdr-subagents@1817e6d670110100fbdc67ef08a31316a3a05bf4
```

Orkastrator detects the interactive backend through its non-launching delegation capabilities. It refuses to delegate when both backends are installed. The `pi-subagents` backend uses its correlated event protocol. The Herdr backend uses the fork's versioned global delegation API and public awaitable runner, and it requires a working Herdr installation.

For an Orkastrator workflow started from Herdr, the extension renders a live, theme-aware workflow widget above Pi's editor. Unary steps stay in one lane, real branches indent, node types carry distinct colors, and implied queued labels are omitted. The extension adds a non-secret launch ID to the accepted workflow input and keeps the socket capability in a user-private runtime descriptor. Hosted reviewer and fixer actions use that binding to call a session-owned Unix socket broker. Active workers open in a right-hand column beside the originating Pi session, and concurrent workers stack downward there. Completed worker panes close automatically. A terminal workflow collapses to a concise in-editor receipt without dumping its raw JSON output. A bound request fails closed if the originating session or broker disappears instead of silently creating an invisible child.

Reviewer children receive only read-only tools. Fixer children receive repository editing tools inside their assigned worktree. Both run with discovered extensions, skills, prompt templates, themes, and context files disabled while retaining the explicit completion extension. They use the configured Pi model instead of hard-coded provider dispatches. Unbound and non-Herdr hosted runs retain the isolated in-memory Pi SDK fallback.

run pi from a trusted git repository, then choose how much ceremony you want:

```text
/kas <implementation request>
/kas:cook <implementation request>
/kas:check <review objective>
```

- `/kas` starts `orkastrator-implement.workflow.ts`. it immediately creates an isolated Worktrunk branch and worktree, then one durable workflow owns the implementation-ready plan, implementation, verification, delivery, committed review target, review, repair waves, and final result there.
- `/kas:cook` starts `orkastrator-cook.workflow.ts`. planning, canonical documentation, and required operator approval stay on the invoking checkout. once the plan is approved, the workflow creates an isolated Worktrunk branch and worktree for implementation, verification, delivery, and the full orkastrator review policy.
- `/kas:check` starts `orkastrator-review.workflow.ts` against the repository's committed `HEAD`. it won't guess when the worktree is dirty.
- `/kas-runs` reports the active or most recent workflow visible to the current pi session. it doesn't perform a name-filtered orkastrator run lookup.

each command addresses its packaged workflow by exact installed file path. the command turn only resolves the repository and launches the workflow. planning, implementation, grilling, and review stay inside the graph.

implementation worktrees use deterministic per-run branches based on the invoking `HEAD`. Worktrunk runs non-interactively with hooks disabled, and the workflow verifies the returned root, branch, base revision, and clean state before passing it to autoimplementation. creation or identity failures block the run instead of falling back to the invoking checkout.

the direct equivalent of `/kas:check` is:

```text
/workflow /absolute/path/to/orkastrator/.pi/workflows/orkastrator-review.workflow.ts --input-json {
  "objective": "preserve the parser contract",
  "repository": "/absolute/path/to/repository",
  "reviewRevision": "<40-character commit SHA>",
  "maxParallelFixers": 3,
  "worktreeRetentionDays": 30
}
```

## policy

the implementation workflows combine their planning and implementation stages with the review workflow. the review graph does this:

1. run one strict initial review against an immutable commit.
2. freeze finding ids, contracts, evidence paths, and writable paths.
3. group blocking findings by overlapping writable paths.
4. run disjoint fixer groups in bounded parallel waves.
5. reject any fixer that changes a path outside its assigned scope.
6. re-review each exact fixer commit against its frozen contracts.
7. integrate accepted commits serially onto the reviewed branch.
8. stop for owner intervention when a group remains unresolved or a genuinely novel out-of-scope finding needs final reconciliation.

evidence location isn't write authority. shared tests may support several findings without forcing their source fixes into one group.

fixer worktrees stay locked while a run owns them. a completed and fully integrated fix is unlocked, then scheduled for cleanup after 30 days by default. future reviews remove a runtime-marked worktree only when its exact commit remains merged, the worktree is clean and unchanged, and no active process is using it.

unresolved, dirty, active, unmarked, and cross-repository worktrees are preserved. set `worktreeRetentionDays` from 1 to 365 days to change the retention window.

a finding observed during scoped re-review takes one of four routes:

- a known sibling finding stays with its existing fixer group.
- a finding introduced by the current fix blocks that fixer.
- a novel finding not introduced by the fix is deferred until final reconciliation and blocks completion.
- an observation omitted by the scoped reviewer isn't preserved as a structured finding.

## proof and limits

historical run records show that a live fixture produced two disjoint fixer groups in one parallel wave, re-reviewed each exact commit, and integrated both serially at `a543512`.

A later run of the former review-only `/kas` command, now `/kas:check`, found and repaired three policy-boundary defects in `4e6f478`: finding identity after sorting, deferred evidence across rejected rounds, and scope enforcement across renames. The Orkastrator suite now passes 55 tests plus TypeScript checking. The Herdr runner passes 247 tests and lint.

The session broker has a real separate-process Unix socket test for result delivery and cancellation. Herdr worker placement remains rooted in the originating session while workflow status stays inside Pi's own widget area. The composed `/kas` and `/kas:cook` workflows still don't have a complete live dogfood run.

autoimplementation delivery currently happens before the orkastrator review stage. repair commits integrated during review aren't automatically republished or sent through a second ci and delivery pass.

architecture context lives in the [orkastrator notion page](https://app.notion.com/p/orkastrator-3c8b3a0a9c198166ab2bc9a3f9c1c3cb). tracked implementation history lives in the [linear project](https://linear.app/kashub/project/orkastrator-aae24ed01e8e).

## files

```text
.pi/workflows/orkastrator-implement.workflow.ts
.pi/workflows/orkastrator-cook.workflow.ts
.pi/workflows/orkastrator-review.workflow.ts
extensions/orkastrator-workflows/index.ts
extensions/orkastrator-workflows/lifecycle-runtime.ts
extensions/orkastrator-workflows/delegation-bridge.ts
extensions/orkastrator-workflows/herdr-launch.ts
extensions/orkastrator-workflows/herdr-delegation-broker.ts
extensions/orkastrator-workflows/herdr-delegation-client.ts
extensions/orkastrator-workflows/herdr-session-pane.ts
extensions/orkastrator-workflows/workflow-widget.ts
extensions/orkastrator-workflows/review-runtime.ts
extensions/orkastrator-workflows/review-wave.ts
extensions/orkastrator-workflows/worktree-retention.ts
```

The extension registers `/kas`, `/kas:cook`, `/kas:check`, `/kas-runs`, the in-process backend bridge, and the session-owned Herdr broker. Herdr-bound workflow leaves return to the originating session for visible worker-pane execution while status remains in the embedded widget. Other hosted workflow leaves use the isolated Pi SDK child path. The three workflow definitions own their complete command lifecycles.

## development

```bash
npm install
npm run typecheck
npm run test:extension
```

the old custom lifecycle, reducer, ledger, worktrunk identity, rpc worker manager, and monitor extension were removed at cutover. git history remains the reference for that implementation.
