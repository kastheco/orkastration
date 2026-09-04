# Pi Workflows package alignment

## Orkastrator dependency and composition

Orkastrator pins `@osolmaz/pi-workflows` to `0.16.0`. `package.json` and `package-lock.json` must remain on the same exact version. Orkastrator loads that dependency's extension from its own package closure, so users must not install a second standalone `@osolmaz/pi-workflows` Pi package beside it.

Custom Orkastrator workflows mount package workflows through canonical references such as `builtin:autoimplement`. Imported workflow objects are contracts for TypeScript inference and runtime contract checks, not source identity. Orkastrator's plan-change composition follows the same rule for `builtin:autoplan`, `builtin:autodoc`, and `builtin:plan-approval`.

This boundary matters because Pi can load the custom workflow in one package context while its host worker loads it in another. Direct object identity is process-local. A canonical built-in reference persists the built-in ID and revision instead of either package installation path.

Project workflow files remain file-backed. Their path and SHA-256 hash are still checked on resume, so editing an Orkastrator workflow continues to trigger normal source-change protection.

## Maintained 0.16.0 compatibility patch

The exact 0.16.0 dependency is patched during installation by `scripts/apply-patches.mjs`. The patch is part of Orkastrator's runtime contract, not an optional local modification. It currently covers two integration gaps:

1. It exposes process-local human-decision presenter registration so Orkastrator can render a protected decision through Pi's trusted UI boundary without exporting a model-callable answer API.
2. It replaces a continuation row's reserved empty `input_hash` with the parent run's carried input when the continuation worker initializes. Without that update, every post-decision continuation reads `{}` and `/kas:cook` eventually loses its task and repository input.

Canonical nested workflow sources are provided separately by the shipped 0.16.0 resolver and Orkastrator's explicit built-in references. `extensions/orkastrator-workflows/tests/pi-workflows-host-regressions.test.ts` pins the patched host behavior, while `extensions/orkastrator-workflows/tests/workflow-source-identity.test.ts` covers source identity across package instances. `scripts/test-decision-questionnaire-runtime.mjs` proves the protected decision and continuation path through a real RPC client. `scripts/test-cook-lifecycle.mjs` runs a disposable `/kas:cook` fixture through the real host, continuation, desktop `pi-subagents` broker, reviewer child, implementation, and verification.

The compatibility patch should shrink or disappear when an upstream release provides equivalent contracts. Until then, upgrading `@osolmaz/pi-workflows` requires regenerating the patch against the exact new version and rerunning the complete Orkastrator suite.

## Verification

A clean installation must allow the reviewed postinstall hook to apply the package patch:

```bash
npm ci
npm run typecheck
npm run test:extension
npm run test:decision-runtime
```

The disposable lifecycle proof additionally requires Worktrunk's `wt` executable:

```bash
npm run test:cook-lifecycle -- --runs 2
```

Do not use `npm ci --ignore-scripts` unless `node scripts/apply-patches.mjs` is run explicitly before typechecking or testing.

## Rust `piw` compatibility

The Rust `piw` viewer must describe the same durable schema as the active npm host. A viewer built for an older schema can reject the active database before opening it for normal read-only use. This is an upstream release-packaging concern, not a reason to edit, migrate, replace, or patch `~/.pi/agent/workflows/state.sqlite`.

Use the npm viewer from the same package release as the host when the installed Rust viewer reports an app-version or schema-digest mismatch:

```bash
node ~/.pi/agent/npm/node_modules/@osolmaz/pi-workflows/dist/viewer/cli.js view
```

Install a corrected Rust release only from an exact published version or reviewed commit. Do not install an unpinned branch against active workflow state.
