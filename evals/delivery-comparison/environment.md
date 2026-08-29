# Frozen environment and operations

## Local contract environment

- Python requirement and lockfile come from the repository (`pyproject.toml`, `uv.lock`).
- Each trial copies a checked-in repository template, initializes Git, and commits the baseline.
- Reset means deleting the entire trial fixture and copying it again; ignored/untracked files cannot leak between trials.
- Adapter subprocesses inherit only a small environment allowlist plus explicit manifest values. Credential and proxy variables are not inherited. Offline flags (`PIP_NO_INDEX`, `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, `NO_PROXY=*`, and `GIT_TERMINAL_PROMPT=0`) are set.
- The harness itself performs no network calls. Environment flags are not an OS network sandbox; live adapters need an owner-approved containment mechanism.
- Stdout/stderr default to 64 KiB retained per stream. Default trial timeout is 30 seconds in the API and 1800 seconds in the future live CLI.

No credentials are needed for validate or calibrate. Do not put credentials in adapter manifests or result bundles.

## Harbor state

This is a local contract harness, not a claim of Harbor execution. At build validation, `command -v harbor` returned no path; Harbor was not installed. The argv/result/event contracts are suitable control-plane specs for later Harbor adapter work, but no Harbor adapter or run has been certified.

## Exact commands

```bash
# Contract validation; executes zero adapter commands
uv run orkastrator-delivery-eval validate

# Fake-only local calibration
rm -rf /tmp/orkastrator-delivery-calibration
uv run orkastrator-delivery-eval calibrate --output /tmp/orkastrator-delivery-calibration

# Future live command; currently refuses because readiness=false
uv run orkastrator-delivery-eval run --allow-live \
  --output /tmp/orkastrator-delivery-live --repeats 3
```

Validation of this implementation can additionally use:

```bash
uv run pytest
uv run ruff check evals/delivery_comparison tests/delivery_comparison
uv run mypy evals/delivery_comparison tests/delivery_comparison
npm run test:extension
npm run typecheck
npm audit --audit-level=high
```
