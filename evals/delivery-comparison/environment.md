# Frozen environment and operations

## Offline contract

- Repository metadata and lockfiles freeze Python/Node dependencies.
- Every trial replaces the fixture, copies a checked-in template, initializes Git, and commits a baseline.
- Dependency-free `unittest` commands each discover at least one test, fail on the wrong baseline, and pass accepted state.
- Subprocesses inherit a small environment allowlist plus non-secret manifest values. Proxy/credential variables are removed and offline flags are set.
- Stdout/stderr and handshakes are bounded. Detached groups are timed out, killed, and reaped.
- Validate/calibrate perform no model, API, or network calls.

## Live execution is structurally disabled

Environment filtering is not containment. The harness-owned containment-launcher allowlist is empty. Schema permits only `backend=none`, fixes `filesystem_isolation=false`, and rejects every `ready=true` manifest. Production manifests have empty argv. Editing fields or adding prose evidence cannot enable a process.

Future work must implement an allowlisted harness-owned launcher that exposes only isolated repo, public manifest/instruction, output, and necessary runtime. It must also implement production-independent effect/commit and post-effect ack observation for crash recovery. Calibration's harness-owned mutation/Git commit is not a production contract; production crash scoring fails closed without the missing observer.

No credentials belong in manifests, digests, bundles, or telemetry.

## Harbor

`command -v harbor` returned no path during initial build validation; Harbor was not installed. This remains a local contract harness, not a claim of Harbor execution.

## Commands

```bash
uv run orkastrator-delivery-eval validate
rm -rf /tmp/orkastrator-delivery-calibration
uv run orkastrator-delivery-eval calibrate \
  --output /tmp/orkastrator-delivery-calibration

# Documented future commands; currently refused before fixture creation:
uv run orkastrator-delivery-eval run --allow-live \
  --comparison-mode tuned-primary \
  --output /tmp/orkastrator-delivery-live-primary --repeats 3
uv run orkastrator-delivery-eval run --allow-live \
  --comparison-mode matched-role-ablation \
  --output /tmp/orkastrator-delivery-live-matched --repeats 3
```

Validation suite:

```bash
uv run pytest
uv run ruff check evals/delivery_comparison tests/delivery_comparison
uv run mypy evals/delivery_comparison tests/delivery_comparison
npm run test:extension
npm run typecheck
npm audit --audit-level=high
```
