# Pi Workflows cutover decision

## Decision

Orkastrator is a policy layer on top of `@osolmaz/pi-workflows` and exactly
one supported delegation backend: `pi-subagents` or `pi-herdr-subagents`. The
migration is complete; this is no longer an exploratory architecture.

Pi Workflows owns durable graph execution, checkpoints, recovery, human
choices, source identity, and result delivery. The selected subagent backend
owns bounded reviewer and fixer sessions. Orkastrator owns only the
review-and-repair rules that are specific to this project.

## Command surface

- `/kas <request>` starts `orkastrator-implement.workflow.ts`. The workflow owns
  its implementation-ready plan, implementation, verification, delivery,
  committed review target, Orkastrator review, repair waves, and final result.
- `/kas:cook <request>` starts `orkastrator-cook.workflow.ts`. It composes
  planning, canonical documentation, required operator approval,
  autoimplementation, and the complete Orkastrator review policy in one durable
  run.
- `/kas:check <objective>` starts `orkastrator-review.workflow.ts` against a
  clean, exact committed revision.

Each command passes the workflow's exact file path inside the installed
Orkastrator package because Pi Workflows discovers names only from project
`.pi/workflows`, global `~/.pi/agent/workflows`, and built-ins. The launch turn
only resolves the repository and starts the graph. It does not run lifecycle
stages through skills or free-form prompt chaining.

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

The current 41-test suite additionally covers strict schemas, path escape
rejection, parallel caps, transitive clumping, exact commit identity, idempotent
effect adoption, owner escalation, cancellation, novel deferred-finding
reconciliation, paths removed by renames, and dual-backend capability routing.

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

A re-review observation is either a known sibling finding, a regression
introduced by the current fix, or a non-fix-introduced deferred finding. Known
siblings remain with their frozen fixer group. Introduced regressions reject the
current fix. Every reported deferred finding prevents workflow completion until
final reconciliation. The current runtime has no separate nonblocking
`unrelated` structured route.

Runtime-owned fixer worktrees now carry retention manifests outside the Git
worktree. Worktrees stay locked while active or unresolved. Fully integrated
worktrees become eligible for cleanup after 30 days by default, but a later
review removes one only when its marker identity, repository ownership, exact
HEAD, merged ancestry, clean status, unlocked state, and inactive process check
all still pass. Legacy and unmarked worktrees are never swept automatically.

## Delegation backends

`pi-subagents` is the default for ClickClack, desktop, and headless sessions. It
uses correlated request, response, and cancellation events. The Herdr path uses
the forked `pi-herdr-subagents` delegation API v1 with additive pane placement
and resource isolation options. Orkastrator probes both capabilities without
launching a child, but selects Herdr only when the current session has a real
`HERDR_PANE_ID`. If neither interactive backend is available, hosted workers use
the isolated Pi SDK runner instead of blocking workflow startup.

An Orkastrator workflow using either interactive backend receives a non-secret
launch binding before the workflow tool executes. The interactive extension owns
a user-private Unix socket broker and renders live workflow state through Pi's
persistent widget area above the editor. The widget uses a theme-aware vertical
outline: unary paths stay flat, branches indent, node types carry semantic colors,
and queued state is implied rather than repeated. Herdr reviewers and fixers open
in a right-hand worker column beside the originating Pi session, with concurrent
workers stacked downward. Desktop requests cross the same broker into
`pi-subagents` without pane placement. The broker validates run ownership,
propagates cancellation, and rejects stale or cross-run capabilities. Terminal
runs collapse to a concise in-editor receipt. Unbound runs retain the isolated Pi
SDK fallback.

## Disposable lifecycle test

`npm run test:cook-lifecycle` runs the complete `/kas:cook` lifecycle without a
real model, a real repository, or a person at the keyboard. It creates a
temporary home, agent directory, and Git fixture with a broken `sum(a, b)`,
installs this repository and `pi-subagents` as Pi packages there, and starts
the installed `pi` CLI in RPC mode with `HERDR_PANE_ID` removed. Every model
turn is served by a local OpenAI-compatible server (`scripts/lib/`) whose
replies are keyed on the Pi Workflows step contract, so the workflow host,
worker processes, the Unix broker, the `pi-subagents` reviewer child, Worktrunk
worktrees, and the fixture's `npm test` all run for real.

The plan-approval decision is answered only through Pi's RPC extension UI
sub-protocol, the same trusted client boundary ClickClack uses. The script
never talks to the workflow host to forge an approval.

Pass `--runs 2` to repeat on fresh fixtures and `--keep` to retain the
temporary root after success. A failure keeps the root and writes
`failure-report.txt` with the run lineage, terminal status, current node, run
error, extension errors, scripted model errors, decisions presented, and the Pi
stderr and event tail.

Two defects surfaced on the first complete runs and are fixed in this tree:

- Pi Workflows 0.16.0 reserved each human-decision continuation run with an
  empty input and never rewrote it when the engine initialized the run, so
  every worker generation after plan approval read `{}` as the run input and
  the review include failed with "review workflow objective is required". The
  package patch persists the carried input at initialization.
- The extension treated continuation following as inert, so the session broker
  stayed bound to the launched run and rejected the review delegation issued by
  the continuation run as another workflow lineage. The chain now follows the
  durable `parentRunId` link that the read-only run store already exposes.

## Remaining limitations

- The session broker pane-backs Orkastrator's explicit reviewer and fixer
  leaves. Generic Pi Workflows `agent(...)` nodes remain hosted and headless.
- Closing the originating Pi session cancels its active pane-backed children.
  Durable workflow state remains available, but broker reattachment after a
  session restart is not implemented.
- Rejected-round reviewer evidence is not its own workflow node, so an
  interruption may repeat a reviewer call. Git effects are still observed and
  adopted idempotently.
- Integration conflicts stop for owner intervention rather than invoking an
  automatic conflict resolver.
- Autoimplementation delivery happens before Orkastrator review. Fixes integrated
  during the later review stage are not automatically republished or sent through
  a second CI and delivery pass.
- `/kas:cook` has a disposable end-to-end test (`npm run test:cook-lifecycle`)
  that runs the real Pi, workflow host, `pi-subagents`, broker, and Worktrunk
  path against a scripted model. `/kas` and the fixer wave of `/kas:check` are
  still covered only by static and unit tests.

These are bounded adapter limitations, not reasons to restore the removed
workflow engine.

## Canonical records

- [Notion: orkastrator](https://app.notion.com/p/orkastrator-3c8b3a0a9c198166ab2bc9a3f9c1c3cb)
- [Linear project: orkastrator](https://linear.app/kashub/project/orkastrator-aae24ed01e8e)
- [GitHub repository](https://github.com/kastheco/orkastrator)
