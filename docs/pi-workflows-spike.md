# Pi Workflows cutover decision

## Decision

Orkastrator is a policy layer on top of `@osolmaz/pi-workflows` and
`pi-subagents`. The migration is complete; this is no longer an exploratory
architecture.

Pi Workflows owns durable graph execution, checkpoints, recovery, human
choices, source identity, and result delivery. `pi-subagents` owns bounded
reviewer and fixer sessions. Orkastrator owns only the review-and-repair rules
that are specific to this project.

## Command surface

- `/kas <request>` expands the installed `implement` skill, allows only the
  planning needed by the request, and automatically applies the `/kas:check`
  contract after the implementation is reviewed, tested, and committed.
- `/kas:cook <request>` expands `grill-with-docs`, keeps its interview and domain
  documentation phase active across user turns, then follows the exact installed
  `implement` skill and the `/kas:check` contract.
- `/kas:check <objective>` runs only the durable `orkastrator-review` workflow
  against a clean, exact committed revision.

Required skills are discovered through Pi's command registry. Missing
`implement` or `grill-with-docs` resources fail closed before the pipeline starts.

## Why the custom runtime was removed

The original implementation directly built a lifecycle engine, JSONL ledger,
policy reducer, Pi RPC process manager, Worktrunk identity layer, monitoring
extension, timeout behavior, and recovery protocol. Those components were
useful for discovering the required invariants, but retaining them would mean
maintaining a second workflow platform beside Pi Workflows.

The cutover removed that duplicate runtime after a live vertical slice proved
the replacement path.

## Live proof

Run `20260829T212547609Z-orkastrator-review-3556c326` reviewed a committed
fixture containing independent regressions in `parseCount` and
`normalizeName`.

The run demonstrated:

- one immutable structured initial review;
- two blocking findings with a shared test evidence path;
- two disjoint writable scopes and two deterministic fixer groups;
- both groups dispatched in one bounded parallel wave;
- exact one-file fixer commits `8945c1a` and `19dae23`;
- mandatory scoped re-review for each commit;
- unrelated sibling observations not blocking the wrong fixer;
- serial integration at `a543512`;
- a clean final worktree with all fixture tests passing;
- durable Pi Workflows completion with no unresolved groups.

The first real review dogfood run (triggered by the former `/kas` review-only
command, now `/kas:check`),
`20260829T215431995Z-orkastrator-review-053a7221`, reviewed the cutover itself.
It found and repaired three blocking boundary defects in `4e6f478`: finding
identity after sorting, deferred evidence across a rejected round, and rename
scope enforcement. The cleanup commit `11d33cd` removed the last unused reducer
seam.

The current 21-test suite additionally covers strict schemas, path escape
rejection, parallel caps, transitive clumping, exact commit identity, idempotent
effect adoption, owner escalation, cancellation, novel deferred-finding
reconciliation, and paths removed by renames.

## Retained policy

Orkastrator keeps:

- immutable initial finding contracts;
- separate evidence paths and writable paths;
- deterministic grouping by overlapping write authority;
- bounded parallel fixer waves;
- exact-commit and scope enforcement;
- mandatory scoped re-review;
- a two-round repair bound;
- serial integration;
- final reconciliation and owner escalation.

A re-review observation is classified as a known sibling finding, a regression
introduced by the current fix, a genuinely novel relevant finding, or an
unrelated repository issue. Only the first may be ignored at the current fixer
boundary without further action. A novel deferred finding prevents workflow
completion until reconciliation.

## Remaining limitations

- The extension-to-extension delegation bridge is currently an interactive Pi
  integration. Standalone `pi-workflows host` support requires a supported host
  extension seam or generic public agent action.
- Rejected-round reviewer evidence is not its own workflow node, so an
  interruption may repeat a reviewer call. Git effects are still observed and
  adopted idempotently.
- Fixer worktrees are preserved for evidence; automated retention cleanup is
  not yet implemented.
- Integration conflicts stop for owner intervention rather than invoking an
  automatic conflict resolver.

These are bounded adapter limitations, not reasons to restore the removed
workflow engine.

## Canonical records

- [Notion: orkastrator](https://app.notion.com/p/orkastrator-3c8b3a0a9c198166ab2bc9a3f9c1c3cb)
- [Linear project: orkastrator](https://linear.app/kashub/project/orkastrator-aae24ed01e8e)
- [GitHub repository](https://github.com/kastheco/orkastrator)
