# Native Pi vs Pi-native Orkastrator delivery comparison

## Decision and boundary

This is a local, adapter-neutral contract harness for comparing delivery outcomes on identical fresh Git fixtures. Correctness is decided only by the final repository snapshot, protected/write-scope checks, and an independent hidden verifier. Adapter-reported calls, tokens, cost, turns, and workflow events are diagnostic; they cannot turn a wrong tree into a success.

The candidate boundaries are deliberately strict:

- **native-pi** must preserve the observed parent → worker → reviewer → repair workflow. Capturing an exact command without that workflow is not an acceptable adapter.
- **orkastrator** must use the production ledger, policy, and Worktrunk adapters. A benchmark-only scheduler, ledger, or redelivery emulation is not acceptable.

No production Orkastrator source is changed by this harness.

## Initial matrix

The initial precommitted matrix is 3 frozen tasks × 2 harnesses × 3 repeats (18 trials): one clean bugfix, one hidden-edge repair, and one injected crash after dispatch but before acknowledgement. Every trial starts from a replacement copy and a new baseline Git commit. Task order and trial IDs are deterministic.

## Continuation and kill gates (hypotheses for owner approval)

These thresholds are hypotheses, not established facts, and require owner approval before a live run:

1. Orkastrator correctness must be no lower than native Pi across the 9 trials per harness.
2. The crash task must show zero duplicate action IDs and zero lost committed work in every repeat.
3. Continue only if Orkastrator has either (a) at least 25% fewer median supervisor turns or (b) a successful crash recovery that native Pi does not achieve.
4. On the clean task, Orkastrator's median wall-clock time may be at most 25% slower than native Pi.
5. Stop and investigate on any infrastructure classification, scope violation, malformed bundle, fixture drift, or human interruption; do not silently count those as a candidate loss.

There is no weighted score. Reports expose correctness and each operational measure separately, with medians and pairwise deltas.

## Readiness

**No live run is ready or authorized.** Both checked-in live adapter manifests have `ready=false` and no command. `run` requires `--allow-live`, an explicit output directory, and two ready adapters; readiness is checked before any adapter command executes. Calibration invokes deterministic fakes only and spends no model/API usage.

Before setting readiness true, freeze owner-approved executable argv, verify native workflow fidelity, verify production Orkastrator integration, pin the model/configuration, and record credential/network policy. Harbor is not required for this local contract harness. Harbor control-plane adapter specs can be added later; this repository does not claim Harbor execution.

## Commands

```bash
uv run orkastrator-delivery-eval validate
uv run orkastrator-delivery-eval calibrate --output /tmp/orkastrator-delivery-calibration
# Future only, after owner approval and both manifests are ready:
uv run orkastrator-delivery-eval run --allow-live --output /tmp/orkastrator-delivery-live --repeats 3
```
