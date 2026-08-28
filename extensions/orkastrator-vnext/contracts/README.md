# Orkastrator vNext external contracts

These fixtures pin the external behavior used by the vNext implementation. They are evidence from live local runs, not guessed API descriptions.

## Baseline

- repository baseline: `d4deae9` (`fix: support re-reviewer fallback configuration`)
- Python baseline: 435 passed, two existing warnings, 91.08% coverage
- Node: 22.22.2
- Git: 2.55.0
- platform: x86_64 Linux

## Pi RPC

The supported runtime is the repository-local `@earendil-works/pi-coding-agent` 0.84.3 package. Use `./node_modules/.bin/pi`; the independently installed global `pi` is not part of this contract.

The package is pinned exactly in `package.json` and `package-lock.json`. The live probe launched:

```text
./node_modules/.bin/pi \
  --mode rpc \
  --no-session \
  --no-extensions \
  --extension <shutdown-probe-extension> \
  --model openai-codex/gpt-5.6-sol:low
```

Observed behavior is recorded in `fixtures/pi-v0.84.3.json`:

- commands and responses correlate through request IDs;
- prompt acceptance is not completion;
- `agent_settled` is the full-idle boundary;
- `get_session_stats` returns message, token, cost, and context statistics;
- abort returns a successful correlated response and is followed by `agent_end` and `agent_settled`;
- SIGTERM runs the extension `session_shutdown` handler with reason `quit` and exits with code 143;
- stdout is strict LF-delimited JSONL. The live probe inspects raw stdout, rejects CRLF records, and requires the final record to end with LF.

`../rpc/jsonl.ts` deliberately does not use Node `readline`. Its unit tests cover compatible CRLF input, chunk-split UTF-8, final unterminated input records, rejected non-JSON values, and U+2028/U+2029 inside JSON strings.

## Worktrunk

The supported binary is Worktrunk 0.75.0 from the official x86_64 MUSL release asset.

```text
path: /home/kas/.local/bin/wt
version: wt v0.75.0
sha256: d1e561fb68e060d48a6829b9308d58521e45674584f0771d5cc0ebc7d74bb152
```

The release archive checksum passed before installation. The live fixture used temporary Git repositories with these exact command shapes:

```text
wt -C <repo> switch --create <branch> --base main --no-hooks --no-cd --format=json
wt -C <repo> --config-set list.json-schema=2 list --format=json
wt -C <feature-worktree> --yes merge --no-remove --format=json
wt -C <feature-worktree> merge --no-remove --no-hooks --format=json
wt -C <repo> remove <branch> --foreground --no-hooks --format=json
wt -C <repo> remove <branch> --reap --force-delete --foreground --no-hooks --format=json
```

The hook failure fixture used a project `pre-merge` hook that printed to both streams and exited 23. The merge conflict fixture committed different content to the same file on `main` and the feature branch. Observed behavior:

- `wt switch --create ... --no-hooks --no-cd --format=json` returns a JSON create envelope;
- `wt --config-set list.json-schema=2 list --format=json` returns repository, exact head, worktree identity, dirty/conflicted state, and operation state;
- a blocking hook exits with the hook's code and writes no JSON to stdout even when `--format=json` is requested; a fresh schema-2 list read then recovers branch, head, worktree, and clean operation identity;
- a merge conflict exits 1, writes no JSON to stdout, leaves rebase open, and is visible as `operation: rebase` plus `changes.conflicted: true` in list schema 2;
- `wt remove --foreground --format=json` returns an array with `branch_outcome`;
- `wt remove --reap --foreground` terminated a detached process whose cwd was inside the worktree before removing it.

Failures therefore become typed Orkastrator observations from exit code, stdout, stderr, and a fresh schema-2 identity read. JSON output alone is not a valid failure contract.

### Known Worktrunk reporting gap

The reap probe's detached `sleep` exited on SIGTERM, but Worktrunk stderr said `Reaped 0 of 1 process; 1 ignored SIGTERM & SIGKILL`. Orkastrator must verify process liveness and worktree identity rather than trusting that summary line.

## Repository validation profile boundary

KAS-739 proves the Worktrunk hook and command envelopes. KAS-742 owns the repository schema that resolves the public `repo-default` name to exact argv, working directories, timeouts, expected exits, bounded output, and Worktrunk hook IDs. Until that schema exists, `repo-default` is intentionally not executable and must not be inferred from package scripts.

## Re-running live contracts

Default tests validate the pinned fixtures without mutating Git or spending model tokens. Live probes are explicit:

```text
ORKASTRATOR_LIVE_WORKTRUNK=1 npm run test:vnext-contracts -- --test-name-pattern='Worktrunk 0.75.0 exposes'
ORKASTRATOR_LIVE_PI_RPC=1 npm run test:vnext-contracts -- --test-name-pattern='Pi 0.84.3 accepts'
```

The Worktrunk probe creates and destroys temporary repositories and detached processes. The Pi probe makes a small real model call, then starts and aborts a second call. Neither runs in ordinary CI.

## Fixture policy

The fixtures retain stable fields and normalized paths. Dynamic temporary paths, Git SHAs, session IDs, timestamps, and process IDs are excluded unless their shape is the contract. Changing either pinned version requires a fresh live capture and review of every assertion in `tests/contracts.test.ts`.
