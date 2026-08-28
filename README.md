# orkastrator

orkastrator is a supervised execution-graph controller for Orca-managed software-delivery lanes.

The supervisor is the interactive agent you are talking to through `$orkastrate`, `$orkas`,
`/orkastrate`, or `/orkas`. It reads Linear and Notion, discusses priorities and tradeoffs with
you, proposes independent lanes, and waits for exact acceptance. The Python controller begins at
that accepted proposal: it creates and monitors the Orca work, persists evidence, publishes lane
branches, and enforces the review and CI convergence policy.

```text
you <-> interactive orkastrator supervisor
              |
              | reads Linear/Notion, answers questions, proposes lanes
              v
       exact owner acceptance
              |
              v
       deterministic controller
              |
              +-- worker -> initial review
              |                 |
              |                 `-- frozen findings -> fix -> scoped re-review
              |                                              |
              |                                              `-- escalation -> adjudicated
              v
         persisted Orca tasks
              |
              v
  orkastrator/<run>/<lane> -> draft PR -> exact-SHA GitHub checks
                                          |
                                          +-- pass -> ready
                                          |           `-> merge commit
                                          `-- fail -> scoped CI fix, max 2
```

There is no separate planner agent. The supervisor uses its current conversation model and its
existing connectors. It records the proposal with `orkas propose`; the YAML config controls only
the execution roles and convergence policy.

## Configuration

The default policy is [`orkastrator.yaml`](orkastrator.yaml). An all-Claude execution-role example
is [`orkastrator.claude.yaml`](orkastrator.claude.yaml).

```yaml
version: 2
max_parallel_lanes: 3
max_parallel_workers: 6

roles:
  worker:
    agent: codex
    model: gpt-5.6-sol
    strength: medium
  initial_reviewer:
    agent: claude
    model: opus
    strength: high
  fixer:
    agent: codex
    model: gpt-5.6-luna
    strength: high
    fast: true
    fallback:
      agent: codex
      model: gpt-5.6-terra
      strength: high
      fast: true
      trigger: capability_mismatch
  re_reviewer:
    agent: claude
    model: sonnet
    strength: medium
```

Every execution profile accepts optional `fast`, defaulting to `false`. Codex fast mode uses the
priority service tier; Claude fast mode requests Claude Code fast mode. The surrounding supervisor
model is selected by the interactive session, not this file.

The review cycle freezes initial findings, limits each finding to two semantic fix rounds, rejects
out-of-scope path changes, serializes overlapping fixers, and defers unrelated re-review findings.
An integration conflict is a fact about the lane head moving rather than about the fix, so it
retries the same round instead of spending one. Accepted fixes integrate serially into the lane
checkout.

Publication uses non-force deterministic branches and one GitHub PR per lane, opened as a draft and
marked ready once the exact-SHA checks on the published head pass. Those checks are the gate before
anything lands. An empty or still-running rollup remains pending. A check run that contradicts itself
is read as terminal when it has a `conclusion`, a completed status, or a `completed_at`, regardless of
a stale `in_progress` status. A missing conclusion on an otherwise terminal check fails closed: it
cannot pass, and leaving it pending would wait forever for a check GitHub says already finished.
If required checks do not conclude within `final_gate.timeout_seconds`, the lane blocks and leaves
the pull request open. Advisory checks are recorded but do not gate, and the concluded rollup is
copied onto the publication receipt before an enabled merge runs.

A pull request has three states and each one gets its own answer. `OPEN` is the working case.
`MERGED` means the lane's branch reached the base branch, which is the outcome the lane exists to
produce: the receipt records `landed`, the lane completes, and it is never published again. `CLOSED`
means somebody rejected the branch and blocks the lane, saying so. Publication reads the pull request
before it pushes anything, because GitHub deletes the head branch on merge and pushing first would
recreate it.

**Landing is shipped, but opt-in.** `publication.merge` is `false` by default. When enabled, after
the final gate passes the controller lands the lane in the current base branch with a real merge
commit, never a squash and never a fast-forward, and records the resulting merge SHA in the
publication receipt. A merge conflict raises a lane-scoped `integration_conflict` escalation rather
than failing the run.
orkastrator does not deploy.

Pi users can install the optional [orkastrator monitor extension](extensions/orkastrator-monitors/README.md)
to show live `orkas monitor --watch` background tasks in Pi's footer and inspect their recorded
details with `/orkastrator-monitors`.

The [Pi-native Orkastrator extension](extensions/orkastrator/README.md) is the product's v1 path. The
Python/Orca controller documented below is legacy pre-v1 code retained during migration. Product v1
is unrelated to the legacy YAML `version: 2` workflow schema.

The repository-local `.pi/extensions/orkastrator.ts` entrypoint is auto-discovered after Pi trusts
the checkout. From a merged checkout:

```bash
npm install
pi --approve
```

Use `/reload` after source changes and `/orkastrator-runs` to inspect active and preserved runs. The
current KAS-740 slice owns lifecycle and durable ledger evidence only; worker RPC, policy reduction,
and Worktrunk mutation land in follow-up issues.

A finding can settle wrongly - most often because the supervisor lacked a word for what an
adjudicator meant. `orkas reopen` sends it back to an earlier phase, retires the settled stages at
and after that round, and drops the frozen contracts the reopened phase supersedes. It keeps a
committed fix on purpose, because `accept_fix` settles a finding on exactly that commit.

SQLModel provides the typed SQLite ledger for proposals, findings, attempts, verdicts, integration,
publication, CI, and transition evidence. Raw SQL is limited to SQLite locking and additive
compatibility migrations.

## Setup

```bash
cd /home/kas/dev/orkastrator
uv sync --extra dev

export ORKASTRATOR_CONFIG=/home/kas/dev/orkastrator/orkastrator.yaml
export ORCA_CLI_COMMAND=orca-ide

uv run orkas doctor --json
uv run orkas snapshot --json
```

`uv run orkastrator` and `uv run orkas` are equivalent. orkastrator does not load `.env` files or
own connector credentials.

## Supervisor workflow

Invoke the installed operator skill with details:

```text
$orkastrate find the unblocked KASHH lanes and explain why they can run together
$orkas show me what is blocked and what decision you need from me
/orkastrate propose one low-risk lane for KASHH; do not accept it
/orkas resume monitoring run <run-id>
```

The supervisor reads authoritative Linear issues and relevant Notion context, answers questions,
and presents the proposal before recording it. It creates a proposal matching
[`proposal.example.yaml`](proposal.example.yaml), then runs:

```bash
uv run --project /home/kas/dev/orkastrator orkas propose \
  --file <proposal.yaml> --json
```

Nothing is created in Orca until you explicitly accept the returned run ID:

```bash
uv run --project /home/kas/dev/orkastrator orkas accept <run-id> --json
```

Advance once or watch until the graph reaches a terminal state:

```bash
uv run --project /home/kas/dev/orkastrator orkas monitor <run-id> --json
uv run --project /home/kas/dev/orkastrator orkas monitor \
  <run-id> --watch --interval 5 --json
```

Watch mode exits zero for a complete graph or an unanswered operator question. A terminal `blocked`,
`failed`, or `report_failed` graph is still emitted as JSON but exits nonzero, so shell supervision does
not mistake a stopped run for successful completion.

Inspect persisted state without touching Orca:

```bash
uv run --project /home/kas/dev/orkastrator orkas show <run-id> --json
uv run --project /home/kas/dev/orkastrator orkas schemas --json
```

Send a finding that settled wrongly back to an earlier phase:

```bash
uv run --project /home/kas/dev/orkastrator orkas reopen <run-id> \
  --finding <finding-id> --phase pending_escalation \
  --reason validation_failed --note "why" --json
```

`--phase` accepts `pending_fix`, `pending_re_review`, or `pending_escalation`. The round defaults to
the finding's current one.

A finding that already settled on the merits - `resolved` or `deferred` - is refused unless you pass
`--force`. The finding id is typed by hand, and without that guard one transposed character silently
undoes work an agent got right and sends another one to redo it. A `blocked` finding needs no force,
because that is the case reopen exists for. The event records the phase it came from and whether the
reopen was forced.

When later evidence proves a different defect at the current lane head, do not force-reopen the old
contract. Create a fresh recovery finding instead:

```bash
uv run --project /home/kas/dev/orkastrator orkas recover <run-id> \
  --finding <historical-finding-id> --file <recovery-finding.yaml> \
  --note "why the new current-head defect is authorized" --json
uv run --project /home/kas/dev/orkastrator orkas monitor <run-id> --watch --json
```

The file is a `ReviewFinding` YAML or JSON object and must omit `review_revision`; orkastrator binds
that revision to the lane's clean recorded integration head and records the validation baselines there.
The new finding needs its own id and an exact allowed write scope, required outcome, evidence, and
deterministic validation. A blocked historical finding becomes deferred, while all of its contracts,
attempts, stages, verdicts, and integration receipts remain in the ledger. Any live historical stage is
retired. The new finding then follows the ordinary fixer, re-review, serial integration, final-gate, and
publication path, so a successful recovery creates a same-lane integration receipt rather than weakening
`reconcile-head` or manufacturing authorization.

Recovery refuses a dirty checkout, a checkout whose HEAD differs from the recorded integration head,
a lane whose pull request has already landed, a source finding that is still in flight, a dependency
on itself or on an unknown finding, a contract with no runnable validation, or a contract that
supplies its own revision.

Acceptance freezes the proposal and the graph configuration together, so editing
`orkastrator.yaml` while a run is in flight fails every tick after it with `proposal or graph policy
changed after acceptance`. That refusal is right by default: a graph must not silently start running
under a policy nobody accepted. When only the policy moved and you meant it, re-freeze the run rather
than throwing it away:

```bash
# Read what moved. Applies nothing.
uv run --project /home/kas/dev/orkastrator orkas reauthorize <run-id>

# Authorize what that printed.
uv run --project /home/kas/dev/orkastrator orkas reauthorize <run-id> --confirm \
  --note "why this policy change is authorized"
```

The first form prints the changed leaves, field by field:

```
run 1f13dd37…: 1 policy change(s) since acceptance
  final_gate.advisory_checks: [] -> ["packages/browser Chrome conformance (advisory)"]
nothing applied; re-run with --confirm --note "..." to authorize this
```

That is a sentence you can judge. `fc391e6e… -> 9bc80b6d…` is not, which is why `--note` on its own
is no longer enough: a reason typed before seeing the diff is a claim about the change rather than a
reading of it.

Confirming carries the same lanes, findings and worktrees into the new policy and records a
`supervisor_reauthorized_policy` event with both digests, the note, and the field-level changes. If
the *proposal* changed rather than the config, this refuses, because that is a different plan and
wants a new proposal.

A run accepted before the policy payload was stored has nothing to diff against. It says so rather
than printing an empty change list, and records its policy the first time it is read while unchanged,
so the next change is readable.

A lane that went down only because one of its findings is blocked comes back on its own once that
finding resolves. Any other standing cause, including a latched one like an exhausted CI fix round
limit, keeps the lane blocked, because releasing on the newest cause alone would drop the ones
underneath it.

A lane can block with every one of its findings already settled, on a required-check query made a
second after pushing its branch, or on a pull request somebody merged out from under it. `reopen` and
`settle` both act on findings, so neither reaches that lane. `resume` does:

```bash
uv run --project /home/kas/dev/orkastrator orkas resume <run-id> \
  --lane <lane-name> --note "why" --json
```

Without `--lane` it resumes every blocked lane and every lane whose stage report could not be read. For
an unreadable report it preserves the rejected attempt, retires its scheduler key, and dispatches a
replacement in the lane checkout so a commit already on disk can be reported into the ledger. A worker
that explicitly failed, or whose result failed repository validation, remains `failed`; `resume` refuses
to turn that work failure into a report retry. For pre-KAS-686 rows without a rejection kind, resume
recognizes the legacy unreadable-report reason only when it does not match a known work-failure message.

Runs created before the lane-head invariant may have a clean checkout ahead of the recorded integration
head. Recover those without editing SQLite:

```bash
uv run --project /home/kas/dev/orkastrator orkas reconcile-head <run-id> \
  --lane <lane-name> --note "why this legacy head is trusted" --json
```

This fails closed unless the checkout head descends from the recorded head and every intervening commit
has a persisted integration receipt for that same lane. Dirty checkouts, missing receipts, commits from
another lane, and unrelated or non-descendant history are refused. Failed validation receipts stay
failed and their findings stay blocking; reconciliation only repairs the lane identity and records both
an event and a supervisor hand-action receipt.

Close out a finding no further agent round can settle. This is the owner's decision, not the graph's,
so it is a separate command from `reopen` and it records the note as evidence:

### KAS-706 pull-request content

Pull-request content comes from the accepted lane scope, worker summary and validation, independent
review, exact published head, and the latest exact-head CI rollup. The GitHub adapter receives that
evidence through a typed content boundary and reconciles the same owned draft as CI changes; absent
optional evidence is omitted rather than replaced with generic copy.

```bash
uv run --project /home/kas/dev/orkastrator orkas settle <run-id> \
  --finding <finding-id> --phase deferred --note "why" --json
```

## One driver per run

Orca binds a Task Run to the coordinator terminal that started its workers and refuses a
`worker-start` from anyone else with `consumer_fenced`. That check is correct and it is also too late:
a second ticker does not fail cleanly, it makes the *first* one fail intermittently until somebody
notices. So `accept` and `monitor` take an exclusive lock on the run before they touch Orca, and a
second one refuses by name:

```
error: run 1f13dd37-… is already being driven by pid 1833761 on kas since 2026-08-22T15:20:04+00:00;
two supervisors on one run is what produces Orca's consumer_fenced. Stop the other one, or watch it
instead of starting a second.
```

The lock is an `flock` on a file under `locks/` beside the database, which means the kernel releases
it when the holding process dies, however it dies. There is no stale lock to break and no command to
break one: a lock left behind by a dead supervisor is already free.

Everything that only reads (`show`, `report`, `questions`, `snapshot`, `doctor`) is unlocked and
safe to run against a live run. `doctor` reports what is currently being driven:

```bash
uv run --project /home/kas/dev/orkastrator orkas doctor --json | jq .driving
```

## Unread direction

`orkas mail <run-id>` lists messages sent to a stage still in flight that its agent has not read.

```
run 1f13dd37-342a-4cd1-8137-c9a06fbcaaf3

  3 unread

  seq 262  application-lifecycle-kas-580:worker  Supervisor direction: boundary corrections required
  seq 269  application-lifecycle-kas-580:worker  Correction to supervisor direction: point (1)
  seq 281  application-lifecycle-kas-580:worker  Supervisor decision: upload target is decided
```

Sending direction to a dispatch reports success the moment Orca accepts it, and nothing afterwards
says whether the agent ever looked. An agent that enters a wait loop around a subprocess stops
checking its mailbox and does not resume on its own, so every message sent after that point is
dropped in silence while both sides believe they are in contact. The run above lost three pieces of
direction that way across several hours, and the loop they were meant to settle kept running.

It reads the mailbox rather than consuming it. `orca orchestration check` marks messages read, so
running that from the supervisor would destroy the evidence this command exists to find. Stages that
have settled are skipped: nobody can read their mailbox any more, so unread there is expected.

A non-empty listing means the supervisor is talking to itself.

## Reclaiming terminals

Releasing a settled stage closes the pane orkastrator opened for it. That only covers stages
dispatched after the handle was recorded on the row, by a supervisor that lived long enough to
write it. Everything dispatched before that, or by a supervisor that died between `terminal create`
and the stage row's first update, settles with no local record that a pane was ever opened, and the
agent tree stays resident with nobody responsible for it.

Orca still knows. `orca orchestration worker-list` maps every Dispatch in a Run to the terminal it
is attached to, and orkastrator never attaches a worker to a terminal it did not create, so a handle
on one of its own dispatches is unambiguously its own pane. The owner's sessions, the coordinator
terminal and the worktree setup panes are not dispatches of this run and are never named.

```bash
uv run --project /home/kas/dev/orkastrator orkas reap <run-id>
```

```
run 1f13dd37-342a-4cd1-8137-c9a06fbcaaf3

  to close        0
  held in flight  1
  already closed  96
```

The safety rule is the stage, not the terminal. A pane is a candidate only when its stage is both
released and processed, meaning orkastrator has consumed that result and nothing is still reading
it. Everything else is held, whatever Orca reports about it being idle, which is why this is safe to
run against a live run: the stage in flight lands in `held`. It is a dry run until `--confirm`, and
it deliberately does not take the run lock, since the case it exists for is a supervisor that is
already ticking.

If Orca truncates its terminal listing, the plan says so and treats what it could not see as already
closed. It under-reaps rather than over-reaps; re-run to pick up the rest.

## Validation output

The supervisor runs each finding's `validation` commands itself and writes the result into the
contract the next agent reads. That output is then re-billed on every turn that agent takes
afterwards, so it is condensed first, deterministically, by `runners.condense`.

What survives is the part a verdict rests on. A passing `pytest` keeps its counts line and nothing
else; a failing one keeps the counts plus up to twelve failures, each trimmed to the assertion and
its immediate frames. `tsc`, `node:test` and `vitest` are handled the same way. Linters and type
checkers get one extra step, because a fixer told "214 problems" cannot act on it and a fixer handed
all 214 pays for them for the rest of its run:

```text
Found 214 errors.
E501=201  F401=11  SIM105=2
(showing 12 of 214)
src/orkastrator/execution.py:12:8: F401 [*] `os` imported but unused
...
```

The histogram is the decision - one rule at 200 is a formatting sweep, ten rules at one each is
real - and it costs a line. `ruff`, `mypy` and `pyright` are recognised; their code frames, carets
and `help:` suggestions are dropped, since they restate the diagnostic they sit under. Both of
ruff's renderers are read, including the OSC 8 hyperlinks its default one wraps rule codes in, and
both are reported in the concise shape above so the result does not depend on how the command was
invoked.

Whether a suite passed is an exit code compared against the requirement's `expect_exit`, never an
inference, and `condense` never raises: an unrecognised runner falls back to keeping both ends of
the stream rather than only its tail, so a tool that prints its fatal error first still has that
error survive.

## Measuring convergence

A run's cost is not its wall clock. It is how many agent dispatches each finding consumed, and how
much of that work was a repeat of work already attempted. Both are already on disk, so reading them
costs one SQLite query and no agent turn:

```bash
uv run --project /home/kas/dev/orkastrator orkas report <run-id>
uv run --project /home/kas/dev/orkastrator orkas report <run-id> --json
```

```text
dispatches per finding   7.667   (92 adjudication stages / 12 findings)
repeat rate              0.505   (49 of 97 stages redid attempted work)
start rejection rate     0.196   (20 of 102 starts; 7 reservations reset)
findings past round 1    7 of 12
stages past soft budget  1   (0 released for exceeding a hard budget)

still open
    455m  application-lifecycle-kas-580:worker

wall clock by role
   1675m  escalation          52 stages  median 30m
    736m  fixer               26 stages  median 11m
    559m  worker               3 stages  median 52m

contested regions
    6 findings                round 2  app/ui/src/App.tsx
    4 findings  2 from a fix  round 3  services/control-plane/jobs_control_plane/control_plane.py

overdue stages were doing
    3  exec poll loop
    1  unknown

start rejections
   11  supervisor_contract_race
    7  identity_mismatch
    2  unresolved_sha
```

Both leading numbers should fall as the graph gets stricter about what it dispatches. Run `report`
against a run from before a change and one from after: if the two numbers did not move, the change
did not converge anything, whatever else it improved.

A stage counts once against `stages past soft budget`, however many ticks observe it, and the
histogram under it says what those stages were doing rather than only that they were late. `exec
poll loop` is a stage that spent its budget making the same call over and over - a supervisor
waiting on a subprocess by burning an inference every thirty seconds - and it is the one entry
there that names waste rather than slowness. `unknown` is a stage that could not be read, which is
deliberately not folded into a tool bucket.

Every ratio above `still open` counts work that finished, so a run with one stage stuck for hours
scores exactly like a healthy one until that stage closes. `still open` is the line that tells them
apart. Its clock starts when the stage row was created rather than when the surviving worker was
dispatched, so a stage that was dispatched, lost and re-dispatched reads as one long open stage: the
supervisor has been holding it for that whole span, and a runtime-only figure would hide every
abandoned attempt inside it.

`wall clock by role` answers a different question from `dispatches per finding`, and usually gives a
different answer. The median is next to the total because those want different fixes: a role that is
expensive because it runs constantly is a graph problem, and one that is expensive because each run
takes half an hour is a prompt or a tool problem. On the run above, escalation is both - twice the
fixer's stage count at nearly three times its median.

`contested regions` names files more than one finding landed on. Every other number in the report is
per finding or per stage, so a run that spends all day arguing with itself about one file scores well
on all of them: the finding ids are all different, each one is fixed, and the next round produces
another. Grouping by the path in `evidence[].location` is what makes that visible, and it needs no
model and no similarity threshold - two findings are about the same place when they cite the same
place. `from a fix` is the sharp end. A finding whose origin is a previous fix, in a file that
already had one, is a loop that will not settle by reviewing harder, because the question underneath
it is a decision rather than a defect. That is the point to stop and ask the owner instead of
spending another round.

A rejected start is the most expensive thing that can happen: a whole dispatch billed for input,
output and wall clock, and nothing kept. So the buckets are named for who could have prevented it.
`supervisor_contract_race` is the contract we handed out no longer matching the store by the time
the result came back, and no agent could have avoided it. `identity_mismatch` is a base, head,
finding or round we assigned and then asked the agent to type back. `unresolved_sha` is an
abbreviated or truncated digest, which `git rev-parse` resolves without asking anyone. Only
`schema_violation` and `scope_escape` are the agent's.

The classifier is ordered specific-first on purpose. Every reason begins `invalid structured
result`, so matching that prefix early collapses the histogram into a single bar - which is what it
used to do, reporting 17 `malformed_result` for eight distinct causes with three different owners.

Escalations are grouped by reason the same way, so a class that recurs is visible as a count instead
of as twenty near-identical strings. They are also
attributed per lane, because a run-wide total cannot say whether one lane's reviewer is writing
findings the downstream roles cannot act on - which decides whether to fix the reviewer or the
adjudicator. A single finding
consuming a double-digit share of a run's dispatches is the shape to look for, and it is usually one
reason repeating rather than a hard problem.

## Communication boundary

The interactive supervisor can answer questions because it owns the conversation and the
Linear/Notion evidence used to make the proposal. You can challenge a dependency, ask why lanes are
independent, narrow scope, or revise the proposal before acceptance.

Execution workers do not have Linear or Notion authority. They receive bounded task prompts and
return typed results through Orca.

A running worker can ask a question. `orkas monitor` surfaces unanswered questions in its result and
stops watching when it finds one, because an agent parked on a decision the graph cannot make for it
will not move no matter how many ticks it gets. The monitor line is a status line, so it carries the
subject and a count. Read the question itself, and answer it:

```bash
orkas questions <run-id>
orkas answer <run-id> --message <message-id> --body-file answer.txt
```

`--body` takes the text inline; `--body-file` reads it from a file, which is what a direction of any
length wants. Only a question that is currently unanswered on that run can be answered: a typo must
not direct an agent in a different run, and a thread that already has an answer must not collect a
second one, because an agent reading the thread cannot tell which direction is current.

Every answer is recorded as a `supervisor_answered` event, so `orkas show` can reconstruct why a lane
changed course. Answering through `orca orchestration reply` directly is possible but awkward: it
refuses any `--from` other than the run's own `coordinator_handle`, which `orkas answer` resolves for
you.

A worker that cannot proceed at all returns blocked or escalation evidence instead, and that evidence
drives the convergence loop rather than the conversation.

### Frozen diff input

Initial reviewers receive the lane's complete frozen diff. Fixers and re-reviewers receive the same
supervisor-rendered input scoped to their finding paths, so agents do not have to re-derive what
changed. `frozen_diff_budget_bytes` controls the target chunk size and defaults to 65,536 bytes.
Larger diffs are split deterministically between file records with a complete file index. Task
specs are capped below Linux's per-argument limit; when all complete chunks cannot fit, the spec
names every file whose diff content was omitted. A single file may exceed the target, but content
is never silently truncated.

### Stage budgets

An agent that dies is easy: Orca hands its Task back to READY once the worker terminal is gone, and
the stage is dispatched again. An agent that wedges keeps its terminal, so nothing comes back and the
lane waits forever. `stage_budgets` in `orkastrator.yaml` gives each role a wall clock, measured
against the `stage_start_reserved` timestamp already in the ledger:

```yaml
stage_budgets:
  worker: {soft_minutes: 45, hard_minutes: 90}
  max_timeouts: 2
```

`soft_minutes` reports and nothing else: one `stage_overdue` event, and the stage in `monitor`'s
`overdue` list until it settles. The event and the list also carry what the stage was doing, read
from its bounded worker transcript: an agent making progress calls different things, and an agent
waiting on a subprocess by burning a turn every thirty seconds calls the same thing over and over.
That is a string comparison between consecutive turns, not a judgement, so `exec repeated 9/10 turns
unchanged` is a fact rather than a guess. A stage that is calling different things is named by its
last tool and nothing else, because a turn count moves every tick without the stage changing and a
status line that reprints on noise stops being read. A worker that cannot be read reports nothing,
which is not the same as reporting idle. `hard_minutes` releases the worker terminal, which puts the stage
back through the same path a dead agent takes. Neither records a result, because a stage that ran out
of time produced none. Blurring that would turn a slow machine into a false finding. After
`max_timeouts` releases the lane blocks instead, since a stage that wedges every time is telling you
something a third dispatch will not.

Both halves are optional and unset by default. A budget is a claim about how long this repository's
work takes, and no default knows that.

### Reclaiming a settled worker

Whoever opened a terminal closes it. When a profile is not `fast`, Orca launches the agent and
`worker-release` is enough. A `fast` profile is different: orkastrator creates the terminal itself so
it can pass provider argv Orca does not model, and Orca then reports that terminal as
`external_terminal` and answers `processAction: none`. The Dispatch is marked released and the whole
agent process tree stays resident. Left alone that is one `codex` or `claude` tree, its MCP servers,
and a `systemd-inhibit` per stage, for the life of the run.

So the handle is recorded on the stage row at dispatch time, and releasing a settled or timed-out
stage closes that exact pane and no other. A terminal Orca no longer knows about is treated as
already closed rather than retried, and a stage whose agent Orca launched carries no handle at all.

## Verification

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run orkastrator-eval
```
