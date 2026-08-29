# Production model routing for Pi and Orkastrator

## Decision status

This note recommends the configuration to freeze for the first production-faithful comparison. It is not a claim that the configuration is empirically best. Orkastrator has not yet completed a held-out live evaluation, and its full role dispatcher is not wired at the current production entry point. The recommendation is a testable starting point based on official model guidance, the installed Pi contract, the current v1 policy, and the intent recorded in the legacy Orkastrator history.

The main decision is to keep the same semantic role matrix in both systems wherever both can express it. The comparison should measure the delivery systems, not give one system a weaker model pool. The systems differ only in how they enforce routing, fallback, evidence, isolation, and recovery.

## Recommended frozen configuration

### Shared model pool

Freeze these exact provider/model IDs for the first comparison:

- `openai-codex/gpt-5.6-sol`
- `openai-codex/gpt-5.6-terra`
- `anthropic/claude-opus-5`
- `anthropic/claude-sonnet-5`

All four appear in the locally installed Pi 0.84.4 catalog. Catalog presence is a point-in-time environment fact, not a permanent availability guarantee. Pi documents model selection through `--model provider/id` and thinking selection through `--thinking`; Orkastrator's owned attempt runner passes both explicitly in `extensions/orkastrator/rpc/pi-attempt.ts`.

Do not add Luna, Fable, or Haiku to the first frozen matrix. They are credible later ablations, but adding them now changes more than one variable. OpenAI positions Luna for high-volume, lower-cost work, and Anthropic positions Fable for the hardest long-horizon work and Haiku for scoped, checkable work. The current v1 role schema has no utility-worker or exceptional-escalation role for those models. [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model) [Anthropic model selection](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model)

### Tuned native Pi

Native Pi should use fresh role-specific agents or per-run model overrides. Its parent remains the supervisor and explicitly starts worker, reviewer, repair, and re-review agents.

```yaml
native_pi:
  supervisor:
    primary: openai-codex/gpt-5.6-sol
    thinking: high
    fast: true
    responsibility: task framing, decomposition, semantic escalation, final delivery decision
    fallback: none

  worker:
    primary: openai-codex/gpt-5.6-sol
    thinking: medium
    fast: true
    responsibility: initial repository implementation and deterministic validation
    fallback: none

  initial_reviewer:
    primary: anthropic/claude-opus-5
    thinking: medium
    fast: false
    responsibility: fresh-context semantic review of requirements, diff, and validation evidence
    fallback: none

  fixer:
    primary: openai-codex/gpt-5.6-terra
    thinking: medium
    fast: true
    responsibility: bounded repair of one frozen finding contract
    fallback:
      model: anthropic/claude-opus-5
      thinking: low
      fast: false
      activation: parent-supervised eligible replacement only

  re_reviewer:
    primary: anthropic/claude-sonnet-5
    thinking: medium
    fast: false
    responsibility: fresh-context review of the exact fixer diff and finding contract
    fallback:
      model: openai-codex/gpt-5.6-sol
      thinking: low
      fast: true
      activation: parent-supervised eligible replacement only
```

The native Pi fallback entries are an evaluation procedure, not a native durable fallback engine. Pi supports selecting a model and thinking level for each fresh agent process. It does not itself provide Orkastrator's attempt tokens, policy reducer, verified partial-work handoff, or durable fallback eligibility. The parent may launch the listed alternate once, but the adapter must record why it did so and must not claim crash-safe continuation it cannot prove.

### Tuned Orkastrator

Use the same semantic role assignments. Keep automatic alternate-model fallback disabled in the first production comparison until the verified handoff feature is implemented and independently reviewed.

```yaml
orkastrator:
  supervisor:
    primary: openai-codex/gpt-5.6-sol
    thinking: high
    fast: true
    responsibility: semantic escalation and final reconciliation
    fallback: none

  worker:
    primary: openai-codex/gpt-5.6-sol
    thinking: medium
    fast: true
    responsibility: initial repository implementation
    fallback: none

  initial_reviewer:
    primary: anthropic/claude-opus-5
    thinking: medium
    fast: false
    responsibility: frozen initial semantic review
    fallback: none

  fixer:
    primary: openai-codex/gpt-5.6-terra
    thinking: medium
    fast: true
    responsibility: isolated finding-scoped repair
    fallback:
      model: anthropic/claude-opus-5
      thinking: low
      fast: false
      status: deferred until verified handoff is implemented

  re_reviewer:
    primary: anthropic/claude-sonnet-5
    thinking: medium
    fast: false
    responsibility: commit-bound scoped re-review
    fallback:
      model: openai-codex/gpt-5.6-sol
      thinking: low
      fast: true
      status: deferred until verified handoff is implemented
```

The four primary role entries match `orkastrator.v1.yaml`. The reducer carries the exact role settings in its `run_worker`, `run_initial_review`, `run_fixers`, and `run_re_review` actions. The opposite-family fallback choices match the legacy policy in `orkastrator.yaml` and commit `b9ac1bc`, but fallback was deliberately removed from the reduced v1 policy until the basic reviewed path is measured.

## Why this routing is the starting point

### Put flagship capability at ambiguity boundaries

OpenAI describes Sol as the quality-first flagship for complex professional work and difficult coding. It also gives a software-delivery example in which Sol resolves uncertainty and defines the plan before a lower tier performs well-specified implementation and testing. That supports Sol for the supervisor and the initial worker, where the objective and repository may still be ambiguous. Start at medium effort for the worker and high effort for the supervisor rather than maximizing effort for every call. OpenAI recommends representative evaluation and says higher effort should be retained only where it yields a measured gain. [Using GPT-5.6](https://developers.openai.com/api/docs/guides/latest-model) [GPT-5.6 workflow example](https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/)

### Reduce strength after the failure is frozen

A fixer receives a narrower contract, implicated paths, and validation evidence. Terra at medium effort is the documented balanced tier and is a better starting point for this bounded work than another Sol call. This is the safe place to test the owner's legacy idea of reducing model strength without weakening the ambiguous initial implementation stage. `orkastrator.yaml` and historical commits `7d31486` and `7c23154` show that ordinary fixer and review effort was intentionally reduced rather than maximized.

### Cross provider families at review boundaries

The initial implementation uses OpenAI Codex, while the initial semantic review uses Claude Opus. The fixer returns to a smaller OpenAI tier, and the scoped re-review uses Claude Sonnet. This alternation reduces correlated prompt and model-family behavior. It does not make the review independent by itself. Practical independence also requires a fresh context containing the requirements, exact diff or commit, and validation evidence, without the implementer's hidden reasoning or self-justification.

Anthropic positions Opus 5 for complex agentic coding, root-cause analysis, verification, and long-horizon work. Sonnet 5 is positioned as the faster scalable implementation model and is suitable for a narrower, commit-bound re-review at medium effort. Anthropic does not prescribe these Orkastrator role names, so the assignment remains a routing inference that the held-out evaluation must test. [Claude models overview](https://platform.claude.com/docs/en/about-claude/models/overview) [Claude Opus 5](https://www.anthropic.com/news/claude-opus-5) [Claude Sonnet 5](https://www.anthropic.com/news/claude-sonnet-5)

### Fast mode is a latency choice

Use `fast: true` only for the OpenAI Codex roles in this matrix. OpenAI states that Fast changes serving latency and price, not model intelligence. Orkastrator's concrete implementation keeps `--no-extensions` and loads only `extensions/orkastrator/rpc/openai-fast.ts`, which sets `service_tier: "priority"`. The runtime rejects fast mode for non-Codex roles. Do not interpret fast mode as an escalation in model strength. Record both the requested and observed service tier because the service may report or fall back to a default tier. [OpenAI Fast mode](https://developers.openai.com/api/docs/guides/fast-mode)

## Convergence policy

Model routing alone does not force convergence. Convergence comes from immutable evidence, bounded rounds, and deterministic terminal paths.

1. Run one initial implementation from an attested base.
2. Freeze one initial review revision and typed finding set.
3. Group findings by overlapping writable paths.
4. Give each fixer only its finding contract, allowed paths, relevant original context, and validation evidence.
5. Re-review every fixer result in a fresh context against the exact resulting commit or diff.
6. Allow at most two semantic fix rounds per finding and three disjoint fixer groups in parallel.
7. Integrate accepted fixes serially.
8. Escalate or stop at the bound. Do not create an unlimited same-model conversation.

This preserves the useful intent in the legacy controller without assuming that its old Orca execution path is the active v1 contract. The current reduced v1 policy already records the two-round and three-group bounds in `orkastrator.v1.yaml`.

## Restart and partial-work rules

### Continue verified partial work only when all proof is present

An alternate model may continue in the same isolated worktree only after the delivery system has:

- captured and hashed the complete diff;
- proved every changed path is inside the stage write scope;
- recorded the stage base, expected HEAD, source attempt token, and replacement attempt token;
- terminated and reaped the original writer;
- proved there is no concurrent writer;
- preserved a clean, exact identity chain for the repository and worktree.

This verified handoff is an approved Orkastrator design but is not implemented in the reduced v1 path. Native Pi must not claim it merely because a second agent can see the same directory.

### Restart from a fresh attested head when proof is absent

Do not replay a stale finding against an old fail base. Preserve the failed attempt and its evidence, then create a new finding contract against the current clean, recorded integration head. Use a new finding ID, scope, expected outcome, validation contract, and attempt identity.

Restart from the fresh attested head when any of these is true:

- partial work is absent or cannot be hashed completely;
- ownership, process reap, repository identity, or worktree identity is uncertain;
- changed paths escape the declared write scope;
- the expected base or HEAD no longer matches;
- integration or rebase changed the commit under review;
- the previous diagnosis is stale after other accepted fixes;
- validation failed for a semantic reason that needs a new diagnosis rather than a model substitution.

Historical commit `8878707` records this correction in the legacy design: recovery creates a fresh current-head contract instead of redispatching stale findings. The phrase "restart from a fail base" should therefore mean preserve the failure as evidence while restarting from an attested current state, not write again on an obsolete base.

## Bounded fallback

Fallback is one alternate-model replacement, not a semantic retry and not a response to every rejection.

### Eligible causes

Allow at most one alternate-model use per stage attempt for:

- `capability_mismatch`;
- model or provider runtime unavailability;
- per-attempt token exhaustion while total run budgets still have room;
- per-attempt wall-clock exhaustion while total run budgets still have room;
- an explicit terminal model declaration that it cannot proceed;
- a model refusal when the task remains allowed by owner and Orkastrator policy.

A partial-work continuation additionally requires every verified handoff proof listed above. Without that proof, restart clean or wake the supervisor.

### Excluded causes

Do not use model fallback for:

- review rejection;
- deterministic validation failure;
- integration or merge conflict;
- dirty worktree;
- repository or worktree identity mismatch;
- scope escape;
- missing owner approval;
- unresolved cleanup or process ownership;
- exhausted total token, cost, or wall-clock limits.

These conditions survive a model swap and must follow their own supervisor, owner, or terminal path. A confidently wrong result also will not naturally trigger fallback, which is why independent review and deterministic validation remain mandatory.

## What is supported now

The support boundary is important at commit `73da2da`.

| Capability | Native Pi | Pi-native Orkastrator |
| --- | --- | --- |
| Exact per-process model and thinking override | Supported by Pi CLI and agent definitions | Supported by `runOwnedPiAttempt` |
| Fresh worker/reviewer agents | Supported procedurally by parent delegation | Runner and role-bearing reducer actions exist |
| Four-role policy parsing | Parent configuration, not a native policy type | Implemented and strict |
| Durable policy reducer actions | Not native | Implemented |
| Codex priority hook | Available through configured extensions | Repository-pinned implementation exists |
| First worker uses frozen policy role | Parent can select it explicitly | **Not yet wired** at `orkastrator_run_create` |
| Reviewer/fixer/re-review action dispatch | Parent can launch agents procedurally | **Not yet proven through the production entry point** |
| Worktrunk isolation and identity inspection | Not native | Identity adapter exists; full reviewed lifecycle remains downstream work |
| Automatic cross-family fallback | Not native | Deferred from reduced v1 |
| Verified partial-work handoff | Not native | Deferred |
| Held-out evidence that this route is best | None | None |

The current production entry point in `extensions/orkastrator/index.ts` launches the immediate worker with the interactive supervisor's model and thinking level and forces `fast: false`. Its adjacent comment says a later stage will resolve the validated worker role. Therefore the checked-in role matrix is the correct target configuration, but the full Orkastrator adapter must remain unready for live comparison until it proves that the exact frozen role reaches every dispatched Pi process.

## First live comparison approval matrix

Run a six-trial smoke phase before the full repeated matrix. No live run should start until both adapters declare filesystem containment and exact command readiness.

| Item | Proposed freeze | Owner approval required |
| --- | --- | --- |
| Comparison mode | tuned native Pi vs tuned Orkastrator | Yes |
| Tasks | three frozen tasks: clean bugfix, hidden-edge repair, crash/redelivery | Yes |
| Smoke repeats | one per task per system, six trials total | Yes |
| Full repeats | three per task per system, eighteen trials total | Yes, after smoke gate |
| Allowed model pool | the four exact IDs listed above | Yes |
| Role maps | exact YAML-like maps in this note | Yes |
| Per-trial wall cap | 60 minutes | Yes |
| Per-trial token cap | 120,000 total tokens | Yes |
| Per-trial currency cap | $10 maximum, treated as a cap rather than a price forecast | Yes |
| Maximum full-matrix currency exposure | $180 if every trial reaches the cap | Yes |
| Network and credentials | only the frozen provider paths needed by each adapter | Yes |
| Automatic fallback | disabled for first matrix | Yes |
| Partial-work continuation | disabled unless verified handoff is implemented and reviewed | Yes |

Do not estimate trial cost by multiplying public API token prices unless the authenticated production provider bills under those exact terms. Record the provider-observed billed cost when available, adapter-reported cost as diagnostic evidence, and the approved hard currency cap independently. Official pricing pages are mutable and Fast service can change billing, so snapshot the applicable billing source at run authorization. [OpenAI pricing](https://developers.openai.com/api/docs/pricing) [Anthropic models overview](https://platform.claude.com/docs/en/about-claude/models/overview)

## Configuration and telemetry to freeze

Every trial should record:

- comparison mode and configuration digest;
- task ID, fixture digest, instruction digest, and verifier digest;
- adapter version and exact argv digest;
- provider/model ID, thinking level, fast request, and observed service tier for every role call;
- role, stage, attempt ordinal, action ID, and fallback cause;
- supervisor, worker, reviewer, fixer, and re-review call counts;
- prompt/context digest and bounded input description, without secrets;
- initial base SHA, expected HEAD, produced commit, diff hash, and changed paths;
- finding IDs, finding contracts, fix rounds, and re-review verdicts;
- input, cached-input, output, and total tokens when the provider exposes them;
- provider-observed cost, adapter-reported diagnostic cost, and wall time;
- crash, restart, redelivery, commit, and acknowledgement sequence with one stable action ID;
- duplicate action IDs, lost committed work, scope violations, infrastructure classification, and final verifier evidence;
- human interruptions and supervisor turns.

Freeze the configuration and digests before looking at held-out results. A later route change creates a new experiment configuration rather than silently replacing a trial.

## Smoke and continuation gates

Stop before the full matrix if any smoke trial shows:

- an adapter escaping filesystem containment or seeing hidden verifier truth;
- a role call that does not match its frozen model, thinking, or fast setting;
- duplicate action delivery or lost committed work;
- malformed or mismatched telemetry identity;
- a crash task without an ordered crash, restart/redelivery, single external effect, and acknowledgement chain;
- an infrastructure error that cannot be independently corroborated;
- a non-infrastructure verifier failure on either system;
- Orkastrator dispatching the first worker from the supervisor context instead of the frozen worker role.

Continue to the full eighteen-trial matrix only when all six smoke trials pass their final-state verifier, the crash invariant holds for both systems, no protected-path or identity violation occurs, and the cost and wall-time observations remain within the approved caps.

After the full matrix, continue Orkastrator development only if correctness is not lower on the frozen tasks, duplicate and lost-work counts remain zero, and Orkastrator either demonstrates uniquely successful crash recovery or materially reduces supervisor turns without an owner-rejected wall-time penalty. Treat the wall-time threshold and what counts as a material turn reduction as owner-approved hypotheses before the run, not conclusions chosen after results are visible.

## Later ablations

Do not change the primary frozen comparison to run these. Add them as separately named configurations after the first matrix:

1. Replace the initial Sol worker with Terra medium to test whether a cheaper balanced worker preserves final correctness.
2. Route well-specified, mechanically verifiable subtasks to Luna low or medium.
3. Raise Opus or Sonnet effort from medium to high only for task classes where defect recall improves.
4. Add Fable as a rare architecture or hardest-tail escalation after retention, refusal, cost, and availability requirements are accepted.
5. Run a matched-role ablation in which both delivery systems use identical role models to isolate orchestration effects.
6. Keep an all-Sol-high run only as a diagnostic baseline for repeated same-model convergence, not as the strongest native Pi configuration.

## Sources and repository evidence

### Official sources

- [OpenAI: Using GPT-5.6](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI: GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [OpenAI: GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra)
- [OpenAI: GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [OpenAI: Fast mode](https://developers.openai.com/api/docs/guides/fast-mode)
- [OpenAI: API pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI: price-performance workflow example](https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/)
- [Anthropic: Models overview](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Anthropic: Choosing the right model](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model)
- [Anthropic: Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Anthropic: Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence)
- [Anthropic: Claude Opus 5](https://www.anthropic.com/news/claude-opus-5)
- [Anthropic: Claude Sonnet 5](https://www.anthropic.com/news/claude-sonnet-5)

### Local implementation and history

- `orkastrator.v1.yaml`: current reduced primary role matrix and limits.
- `extensions/orkastrator/policy.ts`: strict role schema and Codex-only fast validation.
- `extensions/orkastrator/reducer.ts`: role-bearing actions and bounded policy transitions.
- `extensions/orkastrator/rpc/pi-attempt.ts`: exact Pi model/thinking argv and owned process execution.
- `extensions/orkastrator/rpc/openai-fast.ts`: pinned priority service hook.
- `extensions/orkastrator/index.ts`: current first-worker bypass of the frozen worker role.
- `extensions/orkastrator/README.md`: implemented and deferred v1 boundaries.
- `orkastrator.yaml`: legacy bounded convergence, family-switch fallback, and role-strength intent.
- Commit `7d31486`: reduced ordinary fixer strength.
- Commit `7c23154`: reduced reviewer effort and disabled review fast mode.
- Commit `b9ac1bc`: switched fixer and re-review fallback to the opposite provider family.
- Commit `699993f`: added one supervised ceiling retry with unique attempts.
- Commit `8878707`: replaced stale-finding replay with fresh current-head recovery contracts.
- Commit `73da2da`: repository checkpoint used for the current support assessment.

## Recommendation

Freeze the shared Sol/Opus/Terra/Sonnet matrix above, disable automatic fallback for the first live matrix, and keep both production adapters unready until containment and exact role dispatch are proven. This configuration expresses the owner's original goal without assuming its outcome: use stronger models at ambiguity and judgment boundaries, reduce strength for scoped repair, alternate provider families across implementation and review, and bound every attempt with deterministic evidence. The held-out comparison then decides whether that route is better than its ablations.
