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

## Communication boundary

The interactive supervisor can answer questions because it owns the conversation and the
Linear/Notion evidence used to make the proposal. You can challenge a dependency, ask why lanes are
independent, narrow scope, or revise the proposal before acceptance.

Execution workers do not have Linear or Notion authority. They receive bounded task prompts and
return typed results through Orca.

A running worker can ask a question. `orkas monitor` surfaces unanswered questions in its result and
stops watching when it finds one, because an agent parked on a decision the graph cannot make for it
will not move no matter how many ticks it gets. The supervisor reads the question, explains it, and
answers through Orca's own reply channel:

```bash
orca orchestration reply --id <message-id> --body <text> --json
```

A worker that cannot proceed at all returns blocked or escalation evidence instead, and that evidence
drives the convergence loop rather than the conversation.

## Verification

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run orkastrator-eval
```
