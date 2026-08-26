# orkastrator monitors for Pi

This Pi extension reads background-task metadata from the current project's
`.pi/tasks/session-*/*.json` files. For live `orkas monitor <run-id> --watch` tasks it sets a
compact persistent footer through Pi's public `ctx.ui.setStatus()` API. It never reads task output,
terminal transcripts, or Orca messages, and it never mutates task or run state.

Project trust is required. Project-local extensions load only after Pi trusts the project, and a
globally installed copy also checks `ctx.isProjectTrusted()` before inspecting `.pi/tasks`. Use
`/trust` and restart Pi if the project has not been trusted yet.

## project-local development

Symlink or copy the extension directory into the project's auto-discovered extension directory:

```bash
mkdir -p .pi/extensions
ln -s ../../extensions/orkastrator-monitors .pi/extensions/orkastrator-monitors
pi
```

For a one-session check without installing it:

```bash
pi -e ./extensions/orkastrator-monitors/index.ts
```

Run `/reload` after editing an auto-discovered extension. CLI `-e` is intended for quick tests;
restart Pi to reload a `-e` path.

## global installation

Install this repository as a Pi package through Pi's supported package settings:

```bash
pi install /absolute/path/to/orkastrator
```

For a pinned Git checkout, use the repository and commit or tag:

```bash
pi install git:github.com/kastheco/orkastration@<commit-or-tag>
```

Both commands add the package to `~/.pi/agent/settings.json`. `pi config` can enable or disable the
extension. Review package source before installation because Pi extensions run with the user's full
permissions.

## usage

The footer includes only records whose command is a narrow `orkas monitor <uuid> --watch`
invocation, whose recorded status is `running`, and whose recorded PID is alive. Multiple monitors
are summarized to stay within a 72-column status budget. Terminal and stale records do not remain in
the footer.

`/orkastrator-monitors` reads the current metadata and reports each recognized task's exact task ID,
run ID, PID, recorded status, elapsed time when it can be computed, and output path. It does not
infer or display a worker phase.

Run the fixture and lifecycle tests with:

```bash
npm run test:extension
```
