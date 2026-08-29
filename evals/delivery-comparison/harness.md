# Harness contract

## Live gate and containment

Adapters are launched only as argv arrays with bounded output, hard timeouts, detached process groups, group kill, and reap. Live execution is currently impossible: the harness-owned containment-launcher allowlist is empty, `ContainmentSpec` accepts only `backend=none`, and `AdapterManifest` rejects every `ready=true` value. Editing readiness, argv, isolation booleans, or prose evidence cannot enable execution. All production manifests retain `ready=false` and empty argv.

A future implementation must add and test a harness-owned launcher that exposes only the isolated repo, copied public manifest/instruction, output path, and required executable/runtime. No manifest self-attestation can substitute for that code. This harness makes no OS-sandbox claim.

Calibration stubs are copied alone into each trial root and contain no accepted implementation. Hidden verifier, hashes, accepted source, and calibration control are absent from argv, environment, cwd, and public manifest.

## Frozen comparison contract

Every load and live preflight recomputes the SHA-256 configuration digest. Runtime drift fails before fixture creation. Peers must share comparison mode, model pool, token/cost/wall budget, and tuning budget. `matched-role-ablation` additionally requires identical role/model/thinking maps. `tuned-primary` intentionally permits each production system's best supported role routing. `sol-high-diagnostic` requires every role to use model `sol` with high thinking and remains non-primary.

The effective subprocess timeout is the smaller of the CLI timeout and frozen wall budget; crash recovery receives only the remaining time. Reported input+output tokens and cost above frozen caps fail the trial. Reports preserve mode, maps, pool, budget, digest, bundle status, and budget violations.

## Exact result and telemetry

`result.json` and `events.jsonl` reject unknown fields. Every event carries exact trial/adapter/task identity, contiguous sequence, and bounded detail. Dispatch, effect/action, crash, redelivery, commit, ack, and lost-work events require nonempty action IDs. Missing, extra, mismatched, duplicate-sequence, or whitespace-only identities fail as bounded protocol evidence. Handshakes are opened once and read at most 4,097 bytes; payloads over 4,096 bytes are rejected before parsing, avoiding stat/read replacement races.

Success requires process exit zero **and** `bundle.status=completed`. Exit-zero `failed` and `crashed` bundles cannot succeed.

## Harness-owned crash/effect observation

The crash task does not trust adapter-authored effect or commit events:

1. Initial process atomically publishes a bounded dispatch handshake and remains alive.
2. Harness validates and strips the action ID, then kills that live process group and records dispatch/crash itself.
3. Recovery process publishes a bounded redelivery checkpoint.
4. In calibration only, harness control applies the accepted external repository effect keyed by the dispatch action ID and creates a real Git commit. It independently records effect count, commit count, and commit SHA.
5. Only after the commit returns, the harness generates an unpredictable release nonce and atomically publishes it. The nonce is not present in dispatch/redelivery material.
6. Recovery ack must echo exact trial/adapter/task/action identity and that nonce. Harness validates the post-effect ack with a constant-time nonce comparison.

Success requires exactly one harness-owned effect and one commit, stable action ID, and nonce-bound ack after commit. Event-only fabricated chains, missing/duplicate effects, wrong IDs, ack-before-effect, ack raced during mutation/commit, and lost committed state fail even when adapter telemetry claims success.

The current protocol has no genuine production-independent effect/ack observer. For `calibration_scenario=None`, recovery fails closed with `production independent effect/ack contract is not implemented`. This missing production adapter contract is an explicit blocker alongside containment; the harness does not pretend calibration control proves live delivery.

## Infrastructure classification

Adapter bundle infrastructure fields and adapter stdout/stderr are one untrusted source and never determine infrastructure classification. Only harness/host observations use the closed codes:

- `adapter_launch_failure` — host could not launch the main adapter process;
- `initial_phase_launch_failure` — host could not launch the initial crash phase;
- `verifier_failure` — harness verifier launch or timeout failed.

Adapter-declared service, worktree, containment, authentication, quota, or rate-limit claims remain diagnostic. At exit zero they are scored from final state/status; at nonzero they remain adapter crashes. A future service classification requires a separate harness-owned probe.

## Calibration

Offline calibration covers visible-test discovery, correct/wrong trees, scope escape, timeout/group kill, real crash interruption/restart, fabricated chains, missing/duplicate/wrong effects, wrong and whitespace action IDs, ack-before-effect, lost work, malformed telemetry/bundles, exit-zero failed/crashed statuses, token/cost caps, host launch/verifier failures, ignored adapter infrastructure claims, and output bounds. No live/model/network command runs.
