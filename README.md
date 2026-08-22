# kasgraph

Kasgraph is a small execution-graph controller for supervised Orca workers.

The supervisor is the interactive agent you talk to. It can use its installed
Linear and Notion connectors directly, or invoke Kasgraph's subscription-backed
`codex exec` or `claude -p` planner, to discover work, resolve dependencies, and
propose parallel lanes. Kasgraph creates Orca Tasks only after explicit
acceptance and monitors each accepted lane through a persisted convergence loop.

```text
interactive supervisor (Linear + Notion)
              |
              | optional typed planning turn
              v
  codex exec (read-only) or claude -p (plan mode)
              |
              | reviewed proposal
              v
        Kasgraph acceptance
              |
              +-- worker -> one full initial review
              |                    |
              |                    +-- no findings -> complete
              |                    `-- frozen findings
              |                              |
              |                        fix -> scoped re-review
              |                              |
              |                      resolve, retry, or escalate
              v
      persisted Orca Tasks/Dispatches
```

Kasgraph does not call model APIs directly and does not own API credentials. Its
planner invokes either the installed Codex CLI or Claude Code CLI using their
saved authentication, supplies `SupervisorPlan.model_json_schema()`, and
validates the final JSON with Pydantic. It is one non-persistent subprocess per
planning cycle. Orca remains the execution and worker-lifecycle authority.
SQLite stores proposals, immutable finding contracts, attempts, verdicts,
escalation decisions, validated lifecycle receipts, and transition evidence.

## Configuration

The strict v2 policy and every planner/execution profile are in
[`kasgraph.yaml`](kasgraph.yaml). Its main sections are:

```yaml
version: 2
max_parallel_lanes: 3
max_parallel_workers: 6

planner:
  agent: codex
  model: gpt-5.6-terra
  strength: medium
  fast: false

review_cycle:
  initial_scope: lane_changeset
  freeze_findings_after_initial_review: true
  max_fix_rounds_per_finding: 2
  # See kasgraph.yaml for scope, isolation, integration, and escalation policy.

roles:
  worker:
    agent: codex
    model: gpt-5.6-sol
    strength: medium
  initial_reviewer:
    agent: claude
    model: opus
    strength: high
    fast: false
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
    fast: false

# publication and final_gate define the accepted-run external-write boundary.
```

Every profile accepts an optional `fast` value that defaults to `false`. The
execution-role values map to Orca's supervised worker launch. A fast role uses
Orca's custom-argv path so the provider-native fast setting is active before the
terminal is attached to its Task Dispatch. The top-level `planner` profile
selects the planner backend. For Codex, `fast: true` selects the `priority`
service tier; for Claude, it requests Claude Code fast mode. Claude may decline
the request when fast mode is unavailable for the account or model.
`agent: codex` maps to `codex exec --model` and `model_reasoning_effort`;
`agent: claude` maps to `claude -p --model --effort`. Set `KASGRAPH_CONFIG` to
use a different YAML file. The surrounding interactive session still controls
the model you are talking to.

The v2 controller materializes stages dynamically from persisted finding state.
A capability mismatch uses the configured fallback without consuming a fix
round. A finding can consume at most two semantic rounds; invalid output, scope
escape, and exhausted rounds route to escalation. Unrelated re-review findings
are recorded as deferred instead of reopening the full review. Version 1 YAML is
intentionally unsupported. An accepted database from the fixed-stage scheduler
fails with an explicit unsupported-state error instead of resuming incorrectly.

Fixer worktree isolation, deterministic path-scope rejection, serial commit
integration, publication, and remote CI belong to the remaining delivery slices
and are not live yet. The scheduler reserves capacity atomically and currently
starts at most one fixer per lane; KAS-571 can raise that toward the configured
ceiling after it supplies isolated worktrees and overlap checks.

Inspect the generated agent-result contracts with:

```bash
uv run kasgraph schemas --json
```

An all-Claude example is provided in
[`kasgraph.claude.yaml`](kasgraph.claude.yaml):

```bash
KASGRAPH_CONFIG=kasgraph.claude.yaml uv run kasgraph plan \
  --objective "find the currently unblocked lanes" --json
```

## Setup

```bash
uv sync --extra dev
export KASGRAPH_CONFIG=kasgraph.yaml
codex login status
claude --version
```

Inside an Orca-managed terminal, `orca` is resolved automatically. On Linux
outside Orca, the default is `orca-ide`. Override it when required:

```bash
export ORCA_CLI_COMMAND=orca-ide
```

Kasgraph does not load `.env` files or own connector credentials.

## Operator workflow

The repository includes an explicit operator skill at
[`skills/kasgraph/SKILL.md`](skills/kasgraph/SKILL.md). It tells the interactive
supervisor how to use its existing Linear/Notion connectors, produce a typed
proposal, stop at the owner decision, and monitor accepted Orca graphs.

1. Generate and record a typed proposal using saved Codex authentication:

   ```bash
   uv run kasgraph plan --objective "find the unblocked assistant lanes" --json
   ```

   The Codex planner runs in a read-only sandbox. The Claude planner runs with
   `--permission-mode plan`. Neither planner may mutate connector data, git,
   files, or Orca. If the interactive supervisor already assembled the plan,
   create a proposal shaped like [`proposal.example.yaml`](proposal.example.yaml)
   and record it directly:

   ```bash
   uv run kasgraph propose --file proposal.yaml --json
   ```

2. Review the returned proposal and run ID. Nothing has been created in Orca.
3. After the owner explicitly accepts that run:

   ```bash
   uv run kasgraph accept <run-id> --json
   ```

4. Advance and reconcile once:

   ```bash
   uv run kasgraph monitor <run-id> --json
   ```

   Or keep the controller attached until the graph completes, fails, or blocks:

   ```bash
   uv run kasgraph monitor <run-id> --watch --interval 5 --json
   ```

Other useful commands:

```bash
uv run kasgraph doctor --json
uv run kasgraph snapshot --json
uv run kasgraph show <run-id> --json
```

Every lane begins in one independent top-level Orca worktree. The worker result
starts one full changeset review. A clean review completes the lane without a
fixer. Otherwise, each frozen finding receives its own bounded fixer/re-review
loop with stable IDs and persisted evidence. Every agent reports through Orca
`worker_done`; structured stages put only their JSON contract in the report body,
which Kasgraph validates before advancing.

## Verification

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run kasgraph-eval
```

`pydantic-evals` remains a dependency for deterministic and later model-backed
planner evaluations. Persistent Codex app-server or Claude sessions are
intentionally deferred until thread steering, streaming events, or approval
handling justify their larger protocol surfaces.
