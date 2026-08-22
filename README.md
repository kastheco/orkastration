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
                                          |           `-> merge commit  (target, manual today)
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
anything lands.

A pull request has three states and each one gets its own answer. `OPEN` is the working case.
`MERGED` means the lane's branch reached the base branch, which is the outcome the lane exists to
produce: the receipt records `landed`, the lane completes, and it is never published again. `CLOSED`
means somebody rejected the branch and blocks the lane, saying so. Publication reads the pull request
before it pushes anything, because GitHub deletes the head branch on merge and pushing first would
recreate it.

**Landing is the target state, not the shipped one.** Today the controller stops at a ready PR and
the merge is manual. When landing ships it will use a merge commit, never a squash and never a
fast-forward, so each lane keeps its own history in the base branch, and a lane that conflicts with
a head that moved under it will raise an integration conflict rather than fail the run.
orkastrator does not deploy.

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

Acceptance freezes the proposal and the graph configuration together, so editing
`orkastrator.yaml` while a run is in flight fails every tick after it with `proposal or graph policy
changed after acceptance`. That refusal is right by default: a graph must not silently start running
under a policy nobody accepted. When only the policy moved and you meant it, re-freeze the run rather
than throwing it away:

```bash
uv run --project /home/kas/dev/orkastrator orkas reauthorize <run-id> \
  --note "why this policy change is authorized" --json
```

The same lanes, findings and worktrees continue under the new policy, and the change is recorded as a
`supervisor_reauthorized_policy` event carrying both digests and the note. If the *proposal* changed
rather than the config, this refuses, because that is a different plan and wants a new proposal.

A lane can block with every one of its findings already settled — on a required-check query made a
second after pushing its branch, or on a pull request somebody merged out from under it. `reopen` and
`settle` both act on findings, so neither reaches that lane. `resume` does:

```bash
uv run --project /home/kas/dev/orkastrator orkas resume <run-id> \
  --lane <lane-name> --note "why" --json
```

Without `--lane` it resumes every blocked lane in the run. It clears the lane block and the run status
the block wrote, because a run row left `blocked` reads as stopped even once every lane is healthy. A
blocked lane no longer stops the others in the first place: the run reports terminal only when nothing
is left that could still move, and the per-lane phases carry the block until then.

Close out a finding no further agent round can settle. This is the owner's decision, not the graph's,
so it is a separate command from `reopen` and it records the note as evidence:

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

Everything that only reads — `show`, `report`, `questions`, `snapshot`, `doctor` — is unlocked and
safe to run against a live run. `doctor` reports what is currently being driven:

```bash
uv run --project /home/kas/dev/orkastrator orkas doctor --json | jq .driving
```

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
```

Both leading numbers should fall as the graph gets stricter about what it dispatches. Run `report`
against a run from before a change and one from after: if the two numbers did not move, the change
did not converge anything, whatever else it improved.

Escalations are grouped by reason and start rejections by cause rather than by wording, so a class
that recurs is visible as a count instead of as twenty near-identical strings. A single finding
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
`overdue` list until it settles. `hard_minutes` releases the worker terminal, which puts the stage
back through the same path a dead agent takes. Neither records a result, because a stage that ran out
of time produced none — blurring that would turn a slow machine into a false finding. After
`max_timeouts` releases the lane blocks instead, since a stage that wedges every time is telling you
something a third dispatch will not.

Both halves are optional and unset by default. A budget is a claim about how long this repository's
work takes, and no default knows that.

## Verification

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run orkastrator-eval
```
