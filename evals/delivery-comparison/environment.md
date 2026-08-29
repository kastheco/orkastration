# Frozen environment and operations

## Local offline contract

- Python and Node versions/dependencies are frozen by repository metadata and lockfiles.
- Every trial replaces the fixture directory, copies a checked-in template, initializes Git, and commits a baseline. Ignored/untracked state cannot leak between trials.
- Visible tests use dependency-free `unittest` discovery. Each baseline command discovers at least one test and fails against the intentionally wrong baseline; the accepted state passes it.
- Adapter subprocesses inherit a small environment allowlist plus manifest values. Credential-shaped manifest keys are rejected. Proxy/credential variables are not inherited, and standard offline flags are set.
- Stdout/stderr are bounded. Detached process groups receive termination/escalation and are reaped.
- The harness itself performs no network or model calls.

## Filesystem containment readiness

Environment filtering is not filesystem or network containment. Live readiness requires an explicitly evidenced external containment backend that exposes only the isolated repo, copied public manifest/instruction, and output path. Hidden verifiers, accepted hashes, and calibration controls must be outside the adapter namespace. No supported backend has been proven in this checkout; all native and Orkastrator manifests therefore remain `ready=false`, and `run` refuses before any command.

Calibration protocol stubs run locally without hostile sandbox claims. They contain no accepted implementation and receive no hidden path/content in argv, environment, cwd, or public manifest. Harness-side calibration control changes the final tree only after the stub process exits.

No credentials are needed for validation or calibration. Do not store secrets in manifests, digests, result bundles, or telemetry.

## Harbor state

At build validation, `command -v harbor` returned no path; Harbor was not installed. This remains a local contract harness with adapter/control-plane contracts suitable for later containment and Harbor work. No Harbor execution is claimed.

## Commands

```bash
uv run orkastrator-delivery-eval validate
rm -rf /tmp/orkastrator-delivery-calibration
uv run orkastrator-delivery-eval calibrate \
  --output /tmp/orkastrator-delivery-calibration

# Future only after owner approval and contained, frozen, ready manifests:
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
