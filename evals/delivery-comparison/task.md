# Frozen task contract

Each task directory contains a public `manifest.json`, a small `repo/` template, and `hidden_truth/`. The runner copies only the public manifest for adapter use. Hidden verifier source and complete accepted file hashes remain outside the isolated repository and are not included in adapter prompts.

| task | single capability | Harness-visible test | hidden evidence |
|---|---|---|---|
| `clean-bugfix` | clean bugfix | `python -m unittest discover -v` | repeated mixed whitespace normalization and complete tree hashes |
| `hidden-edge` | hidden-edge repair | `python -m unittest discover -v` | equality dedupe for unhashable JSON-like values and complete tree hashes |
| `crash-redelivery` | after-dispatch/before-ack redelivery | `python -m unittest discover -v` | repeated stable action ID applies once, distinct ID applies, and complete tree hashes |

Every manifest freezes:

- Harness-visible instruction and setup/test argv;
- reset strategy (`fresh_copy_and_git_init`);
- allowed write paths and protected paths;
- expected final behavior and accepted behaviorally equivalent outcomes;
- optional fault point.

`hidden_truth/truth.json` freezes the independent verifier argv and complete accepted SHA-256 tree. Exact bytes are used in this initial matrix to eliminate subjective review; the manifest still records behaviorally equivalent outcomes so a later owner-approved fixture revision can add multiple accepted hashes without changing the behavioral contract. Current code intentionally supports one frozen accepted tree per task.

The clean and hidden-edge tasks permit writing only their implementation module. The crash task permits writing only `worker.py`; `test_worker.py` is protected. Any extra file, changed test, deleted expected file, symlink, or write outside the allowlist fails exact-tree/scope evidence.

The crash task's telemetry must distinguish dispatch, crash, redelivery, action, commit, and acknowledgement. Repeating an `action` ID is a duplicate even if the final behavior happens to pass. A `lost_committed_work` event is a hard failure. Crash recovery is credited only when crash and redelivery are observed, the final trial succeeds, and neither duplicate nor lost-work evidence exists.
