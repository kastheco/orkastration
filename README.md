# kasgraph

Kasgraph is a small execution-graph controller for supervised Orca workers.

The supervisor is the interactive agent you talk to. It can use its installed
Linear and Notion connectors directly, or invoke Kasgraph's subscription-backed
`codex exec` planner, to discover work, resolve dependencies, and propose
parallel lanes. Kasgraph creates Orca Tasks only after explicit acceptance and
monitors each accepted lane through a fixed review loop.

```text
interactive supervisor (Linear + Notion)
              |
              | optional typed planning turn
              v
  codex exec --ephemeral --sandbox read-only
              |
              | reviewed proposal
              v
        Kasgraph acceptance
              |
              +-- lane A: worker -> initial reviewer -> fixer -> re-reviewer
              +-- lane B: worker -> initial reviewer -> fixer -> re-reviewer
              `-- lane C: worker -> initial reviewer -> fixer -> re-reviewer
                                  |
                                  v
                         Orca Tasks/Dispatches
```

Kasgraph does not call the OpenAI API and does not own API credentials. Its
planner invokes the installed Codex CLI, which reuses saved ChatGPT
authentication, supplies `SupervisorPlan.model_json_schema()` via
`--output-schema`, and validates the final JSON with Pydantic. It is one
ephemeral subprocess per planning cycle. Orca remains the only execution and
lifecycle authority. SQLite stores proposal and correlation state.

## Configuration

All planner and execution model/strength choices are in
[`kasgraph.yaml`](kasgraph.yaml):

```yaml
version: 1
max_parallel_lanes: 3

supervisor:
  agent: codex
  model: gpt-5.6-sol
  strength: high

roles:
  worker:
    agent: codex
    model: gpt-5.6-sol
    strength: high
  initial_reviewer:
    agent: codex
    model: gpt-5.6-sol
    strength: high
  fixer:
    agent: codex
    model: gpt-5.6-sol
    strength: high
  re_reviewer:
    agent: codex
    model: gpt-5.6-sol
    strength: xhigh
```

The execution-role values map directly to Orca's supervised worker launch
flags: `--agent`, `--model`, and `--effort`. The top-level `supervisor` profile
maps to `codex exec --model` and `model_reasoning_effort`. Set
`KASGRAPH_CONFIG` to use a different YAML file. The surrounding interactive
session still controls the model you are talking to; this supervisor profile
controls only Kasgraph's optional typed planning subprocess.

## Setup

```bash
uv sync --extra dev
export KASGRAPH_CONFIG=kasgraph.yaml
codex login status
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

   The planner runs read-only and does not mutate connector data, git, files, or
   Orca. If the interactive supervisor already assembled the plan, create a
   proposal shaped like [`proposal.example.yaml`](proposal.example.yaml) and
   record it directly:

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

Every lane uses one independent top-level Orca worktree. Later review/fix stages
run as fresh supervised agents in that same worktree. The fixer always runs; it
performs a verified no-op when the initial review has no findings. This keeps the
DAG deterministic and gives every lane a fresh final review.

## Verification

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run kasgraph-eval
```

`pydantic-evals` remains a dependency for deterministic and later model-backed
planner evaluations. App-server is intentionally deferred until persistent
threads, turn steering, streaming events, or approval handling justify its
larger protocol surface.
