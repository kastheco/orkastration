# Harness contract

## Adapter boundary

An adapter is launched as an argv array with no shell. The runner appends:

```text
--repo ABSOLUTE_ISOLATED_REPO
--task-manifest ABSOLUTE_PUBLIC_MANIFEST
--output-bundle ABSOLUTE_OUTPUT_DIRECTORY
--trial-id STABLE_TRIAL_ID
[--fault-point after-dispatch-before-ack]
```

The process starts in a detached session/process group with stdin closed. Stdout and stderr go to temporary files and only a bounded prefix is retained. A hard timeout terminates, then kills if needed, the whole process group and reaps the leader. Launch failures are infrastructure failures; timeout, nonzero exit, malformed protocol, and wrong final state remain distinct classifications.

Adapters must write `result.json` and `events.jsonl` in the output bundle. Both use schema version `1`, reject unknown fields, and require matching trial/adapter/task identities. Event sequence numbers are contiguous from zero. Action IDs are independently counted to detect duplicates. See `evals.delivery_comparison.models` for the exact types.

The adapter receives only the Harness-visible manifest copy, never `hidden_truth/truth.json` or the hidden verifier source. This is prompt separation, not a hostile OS sandbox: an adapter with arbitrary host filesystem access could inspect this checkout. A future live adapter must provide its own filesystem/network sandbox if secrecy against malicious access is required.

## Native Pi boundary

The native adapter must drive Pi's real parent session and preserve the observed parent → worker → reviewer → repair lifecycle. It must report reviewer and fixer calls and must not replace review with a benchmark script. The exact argv is intentionally absent and readiness is false.

## Orkastrator boundary

The Orkastrator adapter must invoke the production ledger, policy, and Worktrunk integration. It must surface durable dispatch/commit/ack/redelivery evidence from production paths. Benchmark-only emulation is prohibited. The exact argv is intentionally absent and readiness is false.

## Scoring and evidence

After an adapter exits, the runner snapshots all non-VCS files, compares complete expected hashes, detects writes outside allowed paths and changes to protected paths, then runs the hidden verifier. Adapter status and self-reported metrics do not decide success. A passing trial requires valid protocol, a successful process, exact accepted tree, passing behavior, no scope violation, no duplicate action, and no lost committed work.

Reports contain per-trial evidence and unweighted aggregates: success, wall time, calls, tokens, cost, supervisor turns, human interruptions, reviewer/fixer calls, duplicates, lost work, crash recovery, scope violations, and infrastructure errors. Pairwise deltas are descriptive; no vanity score exists.

## Calibration fakes

Checked-in fakes cover success, wrong result, protected-path escape, timeout (including a child process), crash/redelivery telemetry, malformed bundles, duplicate actions, lost committed work, launch infrastructure failure, and oversized output. `calibrate` asserts expected classifications and never selects a live adapter.
