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

orkastrator is an opinionated software delivery policy and workflow suite for pi. it covers planning, implementation, review, and bounded repair. it ships and loads a pinned [`@osolmaz/pi-workflows`](https://www.npmjs.com/package/@osolmaz/pi-workflows) engine so its workflow protocol and protected decision UI stay on one compatible copy.

- `@osolmaz/pi-workflows` owns durable runs, checkpoints, recovery, and workflow state.
- [`pi-subagents`](https://www.npmjs.com/package/pi-subagents) or [`pi-herdr-subagents`](https://github.com/brkastner/pi-herdr-subagents) runs reviewers and scoped fixers.
- orkastrator supplies the delivery workflows and policy: planning and implementation composition, immutable findings, bounded write authority, parallel repair groups, scoped re-review, serial integration, and owner gates.

## install and run

install orkastrator and exactly one subagent backend:

```bash
pi install npm:pi-subagents
pi install git:github.com/kastheco/orkastrator
```

orkastrator loads its pinned workflow engine itself. don't install `@osolmaz/pi-workflows` as a separate pi package: duplicate workflow extensions would compete for the same commands and decision requests. remove an older standalone installation before starting pi again:

```bash
pi remove npm:@osolmaz/pi-workflows
```

to use herdr instead, replace the `pi-subagents` line with the forked runner while it awaits an upstream release:

```bash
pi install git:github.com/brkastner/pi-herdr-subagents@1817e6d670110100fbdc67ef08a31316a3a05bf4
```

orkastrator detects the interactive backend through its non-launching delegation capabilities. it refuses to delegate when both backends are installed. the `pi-subagents` backend uses its correlated event protocol. the herdr backend uses the fork's versioned global delegation api and public awaitable runner, and it requires a working herdr installation.

for an orkastrator workflow started from herdr, the extension renders a compact, theme-aware workflow widget above pi's editor. it shows a bounded window around the active step and summarizes omitted nodes above and below. `/kas:workflow` opens the complete graph in a scrollable overlay. unary steps stay in one lane, real branches indent, node types carry distinct colors, and implied queued labels are omitted. the extension adds a non-secret launch id to the accepted workflow input and keeps the socket capability in a user-private runtime descriptor. hosted reviewer and fixer actions use that binding to call a session-owned unix socket broker. active workers open in a right-hand column beside the originating pi session, and concurrent workers stack downward there. completed worker panes close automatically. a terminal workflow collapses to a concise in-editor receipt without dumping its raw json output. a bound request fails closed if the originating session or broker disappears instead of silently creating an invisible child.

reviewer children receive only read-only tools. fixer children receive repository editing tools inside their assigned worktree. both run with discovered extensions, skills, prompt templates, themes, and context files disabled while retaining the explicit completion extension. they use the configured pi model instead of hard-coded provider dispatches. unbound and non-herdr hosted runs retain the isolated in-memory pi sdk fallback.

## configuration

orkastrator has built-in routing defaults. they apply when no user config exists and remain in place for every field a partial config doesn't override:

| stage | model | thinking |
|---|---|---|
| initial review | `anthropic/claude-opus-5` | `medium` |
| fixer | `openai-codex/gpt-5.6-terra` | `medium` |
| re-review | `anthropic/claude-sonnet-5` | `medium` |

user configuration lives at `$XDG_CONFIG_HOME/orkastrator/config.json`, or `~/.config/orkastrator/config.json` when `XDG_CONFIG_HOME` isn't set. `ORKASTRATOR_CONFIG` can point to a different file. every field is optional:

```json
{
  "review": {
    "initial": {
      "model": "anthropic/claude-opus-5",
      "thinking": "high"
    },
    "fixer": {
      "model": "openai-codex/gpt-5.6-terra"
    },
    "reReview": {
      "model": "anthropic/claude-sonnet-5"
    }
  }
}
```

supported thinking levels are `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. invalid JSON, unknown fields, empty model names, and unsupported thinking levels stop the workflow rather than silently falling back to the active parent model.

run pi from a trusted git repository, then choose how much ceremony you want:

```text
/kas <implementation request>
/kas:cook <implementation request>
/kas:check <review objective>
/kas:workflow
/kas:status [run-id]
/kas:pause
/kas:resume
/kas:cancel [run-id]
```

- `/kas` starts `orkastrator-implement.workflow.ts`. it immediately creates an isolated Worktrunk branch and worktree, then one durable workflow owns the implementation-ready plan, implementation, verification, delivery, committed review target, review, repair waves, and final result there.
- `/kas:cook` starts `orkastrator-cook.workflow.ts`. it resolves one implementation repository from ticket labels, repository routing documents, and code ownership evidence, records the launch checkout baseline, and keeps solution design read-only. before autodoc can mutate a canonical file, the workflow creates one isolated Worktrunk task branch and worktree. documentation, protected approval and replanning, implementation, verification, delivery, and Orkastrator review all reuse that prepared workspace. ambiguous ownership stops before planning.
- `/kas:check` starts `orkastrator-review.workflow.ts` against the repository's committed `HEAD`. it won't guess when the worktree is dirty.
- `/kas:workflow` expands the attached workflow into a scrollable overlay. use `↑`/`↓` or `j`/`k`, jump with home/end, and close with `q` or escape.
- `/kas:status [run-id]` reports the active workflow or the specified durable run.
- `/kas:pause` and `/kas:resume` control the workflow attached to the current pi session. resume acts immediately without a preliminary status call.
- `/kas:cancel [run-id]` cancels the active workflow or the specified run. cancellation keeps the durable terminal record.
- `/kas-runs` remains a compatibility alias for `/kas:status`.

each command addresses its packaged workflow by exact installed file path. the command turn only resolves the repository and launches the workflow. planning, implementation, grilling, and review stay inside the graph.

implementation worktrees use deterministic per-run branches based on the invoking `HEAD`. Worktrunk runs non-interactively with hooks disabled, and the workflow verifies the returned root, branch, base revision, and clean state before passing it to autoimplementation. creation or identity failures block the run instead of falling back to the invoking checkout.

### `/kas:cook` workspace lifecycle

`/kas:cook` keeps three repository identities separate:

1. **launch repository** — the checkout supplied when the command starts. repository routing and solution design may read it, but the workflow records its branch, `HEAD`, and changed paths before any workspace mutation. pre-existing tracked or untracked user paths remain there and are never reset, stashed, copied into the task branch, or treated as workflow output.
2. **prepared task worktree** — one deterministic Worktrunk worktree created from the resolved repository's committed `HEAD`. the durable `pi-workflows.prepared-workspace.v1` receipt records its owner repository, path, base and task branches, base revision, original changed paths, and scope. autodoc is the first mutating stage and receives this receipt. replans and resumed continuations adopt the same receipt even after workflow-owned documentation changes make the task worktree dirty.
3. **child implementation worktrees** — implementation or review workers may create child Worktrunk worktrees from the prepared task worktree. those children do not change the launch checkout or replace the prepared workspace identity owned by the workflow.

cleanliness checks are relative to the prepared-workspace baseline. the task worktree must begin clean, but it is expected to change after autodoc and implementation begin. final review still requires the reported implementation repository to be committed and clean. cancellation retains durable workflow state and existing worktree safety behavior; it never cleans up by rewriting user changes.

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

A later run of the former review-only `/kas` command, now `/kas:check`, found and repaired three policy-boundary defects in `4e6f478`: finding identity after sorting, deferred evidence across rejected rounds, and scope enforcement across renames. The Orkastrator suite now passes 94 tests plus TypeScript checking. The Herdr runner passes 247 tests and lint.

The session broker has a real separate-process Unix socket test for result delivery, cancellation, and accepted continuation runs. Herdr worker placement remains rooted in the originating session while the widget follows the newest continuation through its terminal state. The composed `/kas` and `/kas:cook` workflows still don't have a complete live dogfood run.

After installing these changes, reload Pi before retrying `/kas:cook`. Start a new run instead of resuming a failed run created from an older workflow snapshot. Durable history remains available through `/kas:status <run-id>`.

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

The extension registers `/kas`, `/kas:cook`, `/kas:check`, `/kas:workflow`, `/kas:status`, `/kas:pause`, `/kas:resume`, `/kas:cancel`, the `/kas-runs` compatibility alias, the in-process backend bridge, and the session-owned Herdr broker. Herdr-bound workflow leaves return to the originating session for visible worker-pane execution while status remains in the embedded widget. Other hosted workflow leaves use the isolated Pi SDK child path. The three workflow definitions own their complete command lifecycles.

## development

```bash
npm install
npm run typecheck
npm run test:extension
```

the old custom lifecycle, reducer, ledger, worktrunk identity, rpc worker manager, and monitor extension were removed at cutover. git history remains the reference for that implementation.
