# kasgraph

`kasgraph` is a deliberately small supervisor for agents that already run under
Orca. Orca owns worktrees, terminals, and agent processes. Linear and GitHub
remain authoritative for tickets, pull requests, and CI. Kasgraph proposes one
typed next action, validates it, records the decision in SQLite, and executes at
most one Orca mutation per cycle.

It does not provide another shell, filesystem, subagent tree, workflow engine,
or source of project truth.

## Setup

```bash
uv sync --extra dev
export KASGRAPH_MODEL=openai:gpt-5.2
export OPENAI_API_KEY=...
```

Inside an Orca-managed terminal, `orca` is resolved automatically. On Linux
outside Orca, the default is `orca-ide`. Override it explicitly when needed:

```bash
export ORCA_CLI_COMMAND=orca-ide
```

Kasgraph does not load `.env` files. This keeps credentials under the caller's
existing environment and secret-management boundary.

## Commands

```bash
uv run kasgraph --help
uv run kasgraph doctor
uv run kasgraph snapshot --json
uv run kasgraph plan --objective "prepare two independent issues" --json
uv run kasgraph run --objective "prepare the next safe issue" --json
uv run kasgraph reconcile --json
```

`run` is a dry run by default. `--execute` is required to create one Orca
worktree and launch its agent. A cycle never performs more than one mutation.

The planner can return `wait`, `needs_owner`, or `complete` without selecting a
lane. A `start_lane` plan names one lane. Deterministic validation rejects
duplicate lanes, an unknown selected lane, or a start that would exceed the
configured concurrency limit.

## Verification

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run kasgraph-eval
```

The eval is intentionally local and deterministic. Add real model-backed cases
only after their prompts, providers, budgets, and data handling are explicit.

## State and recovery

SQLite stores supervisor runs, proposed lanes, observed Orca worktree IDs, and
an append-only event ledger. It is correlation state, not authority.

After a restart, run `kasgraph reconcile`. It re-reads Orca and updates local
lane phases. A missing worktree becomes `blocked`; a completed Orca workspace
becomes `complete`; an in-review workspace becomes `review`.

If the project later needs several supervisor processes, replace SQLite with
PostgreSQL before adding a workflow runtime. Add DBOS, Restate, or Temporal only
after crash injection proves that reconciliation and idempotency are not enough.
