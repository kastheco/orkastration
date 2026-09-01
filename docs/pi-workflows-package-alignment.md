# Pi Workflows package alignment

## Orkastrator dependency and composition

Orkastrator pins `@osolmaz/pi-workflows` to `0.15.2`. `package.json` and `package-lock.json` must remain on the same exact version.

Custom Orkastrator workflows mount package workflows through canonical references such as `builtin:autoimplement`. Imported workflow objects are contracts for TypeScript inference and runtime contract checks, not source identity. Orkastrator's plan-change composition follows the same rule for `builtin:autoplan`, `builtin:autodoc`, and `builtin:plan-approval`.

This boundary matters because Pi can load the custom workflow in one package context while its host worker loads it in another. Direct object identity is process-local. A canonical built-in reference persists the built-in ID and revision instead of either package installation path.

Project workflow files remain file-backed. Their path and SHA-256 hash are still checked on resume, so editing an Orkastrator workflow continues to trigger normal source-change protection.

## Remaining npm 0.15.2 composition defect

Canonical references fix the process-local object-identity mismatch for registered built-ins. They do not make npm 0.15.2 safe for custom composition of `autoimplement` or `autodoc`.

Both registered built-ins directly include unregistered internal workflows. The resolver launched from the Pi extension records those children from the installed `src/builtins/*.workflow.ts` tree. The worker resolves the same children from `dist/builtins/*.workflow.js`. `sourceForDirectDefinition()` falls back to file path and hash because the internal definitions are absent from `builtinWorkflowCatalog`.

Fresh diagnostic run `20260901T030232861Z-orkastrator-cook-52edb722` reproduced the remaining defect after commit `887a0f7`. Its top-level `implementation` mount was correctly recorded as `builtin:autoimplement` revision 11, but nested mounts such as `implementation/redesign`, `implementation/workspace`, and `implementation/localVerification` were recorded from the global TypeScript source tree. The worker parked the run with `workflowSourceChanged` before executing a node.

This cannot be corrected safely inside Orkastrator 0.15.2 without importing unpublished package internals, pinning installation-specific paths, or copying the full Autoimplement and Autodoc implementations. Those are unsupported source-identity hacks and were not applied.

The upstream npm fix should give every directly included package workflow a canonical identity independent of `src` versus `dist`. Either of these designs is sufficient:

1. Register internal compositions such as `plan-change`, `workspace-preparation`, and `change-verification` in the package catalog with explicit revisions.
2. Treat nested definitions owned by a registered built-in as part of that built-in's revision and omit file-backed package paths from persisted mounted-source metadata.

The release needs a regression test that launches a custom file workflow through the source extension context and resolves it in the built worker context, then compares the complete mounted-source map and definition digest. Orkastrator can update its exact dependency pin after that release. Until then, the failed diagnostic run should remain parked as evidence and custom Cook/Implement workflows that include Autoimplement should not be forced to resume.

## Rust `piw` 0.15.2 packaging defect

The crates.io `pi-workflows` 0.15.2 package and npm `@osolmaz/pi-workflows` 0.15.2 package do not describe the same durable schema.

| Distribution | Declared app version | Schema digest |
| --- | --- | --- |
| crates.io `pi-workflows` 0.15.2 | `0.13.3` | `b5bcda1034e2ee3013d8ee2cc65dcfe8a2ad6e6ad8ab1eebab7f890de60031ed` |
| npm `@osolmaz/pi-workflows` 0.15.2 | `0.14.0` | `27923e72ec95306d20a5cea087d8d5821dee9b3c36ebd099471e28202d9f51f5` |

The installed Rust source confirms the stale constants in `pi-workflows-0.15.2/src/state/reader.rs`. The npm host owns and can read the active database. The Rust viewer rejects it before opening it for normal read-only use.

This is an upstream release packaging defect, not an Orkastrator schema defect. Orkastrator must not edit, migrate, replace, or patch `~/.pi/agent/workflows/state.sqlite` to satisfy the stale viewer.

Until a corrected Rust release is published:

1. Use the npm viewer from the same package release as the host:
   `node ~/.pi/agent/npm/node_modules/@osolmaz/pi-workflows/dist/viewer/cli.js view`
2. Do not use `~/.cargo/bin/piw` against the active database.
3. After upstream updates the Rust reader to app version `0.14.0` and digest `27923e72ec95306d20a5cea087d8d5821dee9b3c36ebd099471e28202d9f51f5`, install the corrected crates.io release with `cargo install --locked pi-workflows --version <corrected-version> --force`.
4. If upstream provides a reviewed fix commit before publishing, an interim installation may use `cargo install --locked --git https://github.com/osolmaz/pi-workflows.git --rev <reviewed-fix-commit> pi-workflows --force`. Pin the exact commit. Do not install from an unpinned branch.
