# Frozen task contract

Each task has a public `manifest.json`, intentionally wrong `repo/` baseline, and separate `hidden_truth/`. Only a copied public manifest is passed to adapters. Hidden verifier source and complete accepted SHA-256 tree are absent from argv, environment, cwd, and public manifest.

| task | capability | dependency-free visible command | hidden evidence |
|---|---|---|---|
| `clean-bugfix` | whitespace bugfix | `python -m unittest discover -v` | mixed whitespace normalization and complete tree hashes |
| `hidden-edge` | equality repair | `python -m unittest discover -v` | nested unhashable JSON-like equality and complete tree hashes |
| `crash-redelivery` | at-most-once recovery | `python -m unittest discover -v` | repeated stable action applies once, distinct action applies, complete tree, and harness fault evidence |

Every baseline visible command discovers exactly one test and fails on the wrong implementation. Accepted calibration state passes. The hidden verifier adds held-out behavior not present in the visible test.

Each manifest freezes instruction, setup/test argv, replacement reset strategy, write/protected paths, behavior, accepted-equivalence description, and optional fault point. Current accepted trees use exact bytes to remove subjective scoring; adding an equivalent tree requires an owner-approved fixture revision.

For crash redelivery, repository correctness is necessary but insufficient. The harness must observe a live dispatch handshake, kill that process group, restart recovery, and validate one stable action ID through redelivery, one effect/action, commit, and ack in order. Missing or fabricated crash evidence, missing redelivery, ID changes, duplicate action, lost committed work, and unordered chains all fail.

Calibration accepted source lives in harness-side control, not in the protocol-stub process. This supports scoring calibration without placing accepted implementations in trial invocation material. It is not represented as production-agent behavior and cannot make a live adapter ready.
