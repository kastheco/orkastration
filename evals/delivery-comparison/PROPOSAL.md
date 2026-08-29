# Native Pi vs Pi-native Orkastrator delivery comparison

## Primary fairness design

The primary experiment is **tuned native Pi vs tuned production Orkastrator**, not two systems crippled to identical internals. Both receive the same held-out tasks, allowed model pool, total token/cost/wall limits, hidden verifier, and equal pre-evaluation tuning budget. Each may use its best production-supported orchestration, per-role model/thinking routes, and fallbacks.

Native Pi may use native subagent per-run or per-agent model overrides. Orkastrator may use its production role policy. If native Pi can express the same role matrix it may; inability to express a capability is a measured system difference, not a reason to remove it from Orkastrator.

The production boundaries remain:

- **native-pi** preserves the observed parent → worker → reviewer → repair workflow.
- **orkastrator** uses production ledger, policy, and Worktrunk adapters, never benchmark-only emulation.

No production Orkastrator source is changed.

## Modes

1. `tuned-primary` is the decision experiment. Role maps may differ while pool and limits remain equal.
2. `matched-role-ablation` is secondary and requires exactly equal role/model/thinking maps to isolate orchestration.
3. `sol-high-diagnostic` is optional, non-primary, and valid only when every role routes to `sol` with high thinking.

Every manifest freezes mode, role map, thinking, pool, token/cost/wall budget, equal tuning budget, and configuration SHA-256. Digests are recomputed at load and live preflight. The runner bounds effective timeout by frozen wall budget and fails reported token/cost overages. All configurations and commands must be frozen before held-out trials.

## Proposed bounded matrices

Subject to owner approval, initial primary is 3 tasks × 2 systems × 3 repeats (18 trials). Current planning ceilings are 200,000 total tokens, USD 25, and 1,800 seconds per trial, plus equal two-hour tuning budgets. These are hypotheses, not authorization. The matched-role 18-trial matrix requires separate approval and does not automatically follow primary. The all-Sol/high diagnostic is omitted initially.

## Owner-approval continuation hypotheses

1. Primary Orkastrator correctness is no lower than tuned native Pi.
2. Every crash repeat has zero duplicate actions, zero lost committed work, and exactly one independently observed effect/commit between crash/redelivery and ack.
3. Continue only if Orkastrator has at least 25% fewer median supervisor turns or uniquely successful crash recovery.
4. Clean-task median wall time is at most 25% slower.
5. Stop on host infrastructure failures, scope violations, malformed protocol, fixture drift, or human interruption.

No weighted score is computed.

## No-live readiness

**No live run is ready or authorized.** The harness-owned containment-launcher allowlist is empty; schema accepts no self-attested containment backend and rejects all `ready=true` manifests. All primary and matched production manifests have `ready=false` and empty argv. Field edits or prose cannot enable execution.

A second blocker is the missing production-independent crash effect/ack contract. Calibration can apply and Git-commit an effect under harness ownership, then generate an unpredictable release nonce that a later identity-bound ack must echo. The nonce is unavailable before commit, so an ack raced during mutation/commit fails. A production adapter cannot use that calibration control. Until a harness-owned observer can independently establish the real production effect/commit and nonce-bound post-effect ack, the live crash trial fails closed.

Before live readiness, implement and test both a harness-owned filesystem-containment launcher and production-independent effect/ack observation; freeze exact commands/models; re-digest configs; and obtain owner approval. No model/API/network or Harbor execution is claimed.

## Offline and future commands

```bash
uv run orkastrator-delivery-eval validate
uv run orkastrator-delivery-eval calibrate --output /tmp/orkastrator-delivery-calibration

# Future only after both blockers and owner approval:
uv run orkastrator-delivery-eval run --allow-live --comparison-mode tuned-primary \
  --output /tmp/orkastrator-delivery-live-primary --repeats 3
uv run orkastrator-delivery-eval run --allow-live --comparison-mode matched-role-ablation \
  --output /tmp/orkastrator-delivery-live-matched --repeats 3
```
