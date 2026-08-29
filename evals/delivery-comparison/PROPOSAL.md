# Native Pi vs Pi-native Orkastrator delivery comparison

## Primary question and fairness contract

The primary experiment is **tuned native Pi vs tuned production Orkastrator**. Fairness does not mean crippling both systems to identical internals. Before held-out trials, both receive the same held-out tasks, allowed model pool, total token/cost/wall limits, hidden verifier, and equal pre-evaluation tuning budget. Within those constraints each system may use its best production-supported orchestration, per-role model and thinking routes, and fallbacks.

Native Pi may use native subagent per-run or per-agent model overrides. Orkastrator may use its production role policy. If native Pi can express the same role matrix, it may. If either system cannot express a capability, that remains a measured system difference rather than removing the capability from its peer. Every manifest freezes the named comparison mode, complete role→model/thinking map, allowed model pool, budget, tuning budget, and SHA-256 configuration digest. Secrets are forbidden.

The candidate boundaries remain strict:

- **native-pi** preserves the observed parent → worker → reviewer → repair workflow.
- **orkastrator** uses the production ledger, policy, and Worktrunk adapters. A benchmark-only scheduler, ledger, or redelivery emulation is not acceptable.

No production Orkastrator source is changed.

## Experiment modes

1. `tuned-primary` is the decision experiment and primary gate.
2. `matched-role-ablation` is a secondary experiment using the same frozen role routes on both systems, when both can express them, to isolate orchestration effects. It does not replace or veto the primary result.
3. `sol-high-diagnostic` may be added later as an all-Sol/high-thinking diagnostic baseline. It is explicitly non-primary and has no continuation gate unless separately approved.

All task assignments, role maps, thinking settings, fallback rules, limits, and adapter commands must be frozen and digested before any held-out trial. Tuning stops before held-out tasks are disclosed to live adapters.

## Initial matrix and bounded cost

Subject to owner approval, the initial primary matrix remains 3 tasks × 2 systems × 3 repeats (18 trials). The proposed shared per-trial ceiling currently recorded in manifests is 200,000 total tokens, USD 25, and 1,800 seconds, with equal two-hour pre-evaluation tuning budgets. These are planning hypotheses, not authorization. The matched-role ablation is a separately approved 18-trial matrix; it must not run automatically after primary trials. The diagnostic baseline is omitted initially.

## Continuation and kill gates (owner-approval hypotheses)

1. Primary Orkastrator correctness is no lower than tuned native Pi across the 9 trials per system.
2. Every crash repeat has zero duplicate action IDs, zero lost committed work, and a harness-observed ordered crash→restart/redelivery→single effect/commit→ack chain.
3. Continue only if primary Orkastrator has either at least 25% fewer median supervisor turns or uniquely successful crash recovery.
4. On the clean task, primary Orkastrator median wall time is at most 25% slower.
5. Stop and investigate any infrastructure classification, scope violation, malformed protocol, fixture drift, or human interruption rather than silently charging it to a candidate.

These thresholds require owner approval and are not facts. No weighted score is computed.

## Readiness

**No live run is ready or authorized.** Native Pi and Orkastrator manifests, including matched-role variants, have `ready=false`, empty argv, and `containment.backend=none`. Live readiness requires an explicitly evidenced supported filesystem-containment declaration. The process must see only its isolated repository, copied public manifest/instruction, and output path; hidden verifiers, accepted hashes, and calibration controls must remain outside that namespace. No such backend is currently proven, so the runner refuses before creating a fixture or invoking a command.

Calibration is offline and uses protocol stubs only. Accepted file mutations are applied by harness calibration control after the stub exits and are absent from stub argv, environment, cwd, and public manifest. This separation is calibration evidence, not a claim of hostile OS sandboxing.

## Commands

```bash
uv run orkastrator-delivery-eval validate
uv run orkastrator-delivery-eval calibrate --output /tmp/orkastrator-delivery-calibration
# Future primary only after owner approval, frozen configs, containment, and readiness:
uv run orkastrator-delivery-eval run --allow-live --comparison-mode tuned-primary \
  --output /tmp/orkastrator-delivery-live-primary --repeats 3
# Separate future secondary approval:
uv run orkastrator-delivery-eval run --allow-live --comparison-mode matched-role-ablation \
  --output /tmp/orkastrator-delivery-live-matched --repeats 3
```

Harbor is not required for this local contract harness. No Harbor or live model execution is claimed.
