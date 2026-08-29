# Pi Workflows cutover decision

## Decision

Orkastrator is a policy layer on top of `@osolmaz/pi-workflows` and
`pi-subagents`. The migration is complete; this is no longer an exploratory
architecture.

Pi Workflows owns durable graph execution, checkpoints, recovery, human
choices, source identity, and result delivery. `pi-subagents` owns bounded
reviewer and fixer sessions. Orkastrator owns only the review-and-repair rules
that are specific to this project.

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

Automated tests additionally cover strict schemas, path escape rejection,
parallel caps, transitive clumping, exact commit identity, idempotent effect
adoption, owner escalation, cancellation, and novel deferred-finding
reconciliation.

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
