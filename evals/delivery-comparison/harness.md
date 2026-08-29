# Harness contract

## Adapter invocation and containment

Adapters are launched as argv arrays without a shell. The runner appends absolute isolated repo, copied public manifest, output bundle, trial ID, optional fault point, and crash delivery phase arguments. Processes start in detached process groups with closed stdin, bounded stdout/stderr, hard timeout, group kill, and leader reap.

A live-ready manifest must declare `external-verified` filesystem containment, `filesystem_isolation=true`, and bounded evidence. No checked-in live manifest does so. The harness does not implement or claim an OS sandbox today, and all live manifests remain unready. Calibration protocol stubs are non-live, copied alone into the trial root, and have no accepted implementations; accepted calibration mutations stay in harness-side control and their path/content is absent from adapter argv, environment, cwd, and public manifest.

## Frozen configuration contract

Every manifest records:

- comparison mode (`tuned-primary`, `matched-role-ablation`, or diagnostic);
- role→model map and thinking level (`off`, `low`, `medium`, `high`);
- allowed model pool;
- total token, USD cost, and wall-clock ceilings;
- equal pre-evaluation tuning budget;
- SHA-256 digest over the manifest configuration;
- containment capability and evidence.

The runner requires peers to share mode, model pool, resource budget, and tuning budget. Their tuned primary role maps may differ. Reports retain the mode, routes, pool, budget, and digest for every trial and aggregate.

## Exact result and telemetry protocol

`result.json` and `events.jsonl` use schema version `1` and reject unknown fields. Every telemetry event carries exact trial, adapter, and task identity. Sequence numbers must be contiguous and monotonic from the required phase start. Dispatch, action/effect, crash, redelivery, commit, and acknowledgement events require a nonempty action ID. Missing, extra, or mismatched identities; missing/duplicate sequences; or absent action IDs are protocol failures. Missing IDs are never discarded from duplicate analysis because schema validation fails first.

Adapter infrastructure reporting is closed and bounded. `infrastructure.code` is one of service unavailable, rate limited, authentication unavailable, quota exhausted, containment failure, or worktree failure; evidence is nonempty and at most 500 characters. It is classified as infrastructure only when process stdout/stderr contains the exact marker `ORK_EVAL_INFRA:<code>` **and** the independently observed exit code is in that adapter configuration's frozen per-code allowlist. All three facts must agree. Allowlists may explicitly cover zero or nonzero exits. Free-text summaries and uncorroborated structured claims cannot erase candidate failures. Runner launch and verifier launch failures remain independently observed infrastructure failures.

## Harness-controlled crash/redelivery

For the crash task the runner starts an initial delivery phase. The adapter must publish an exact identity/action handshake while remaining alive. The harness observes that live state, kills the detached process group, and records dispatch and crash events itself. It then launches a separate recovery process. Success requires exactly this ordered action-stable chain:

```text
dispatch → harness kill/crash → restart/redelivery → one external action/effect → one commit → ack
```

All six events use the same nonempty action ID. Missing crash, missing redelivery, changed ID, duplicate effect, lost work, or unordered events fail even if repository behavior and hashes pass. Crash recovery cannot be self-awarded by writing fabricated crash event names.

## Correctness and evidence

After process completion, the runner compares the complete accepted tree, detects protected/out-of-scope writes, and invokes a hidden behavior verifier. Adapter claims, model metrics, and summaries are diagnostic. A success requires valid protocol, successful independently observed final behavior/tree, no scope violation, no duplicate/lost work, and—on the crash task—the validated chain.

Calibration covers correct/wrong trees, protected escape, timeout/process-group kill, real harness fault injection/restart, six invalid crash chains, malformed protocol, duplicate/lost actions, launch infrastructure, corroborated service infrastructure at zero and nonzero exits, false infrastructure claims, and bounded output. No live adapter or model is selected.
