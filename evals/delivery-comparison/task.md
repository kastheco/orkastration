# Frozen task contract

Each task contains a public manifest, intentionally wrong repo baseline, and separate hidden truth. Adapters receive only the copied public manifest. Hidden verifier, hashes, accepted source, and calibration control are absent from argv, environment, cwd, and public manifest.

| task | capability | visible command | hidden/harness evidence |
|---|---|---|---|
| `clean-bugfix` | whitespace bugfix | `python -m unittest discover -v` | mixed whitespace and complete tree hashes |
| `hidden-edge` | equality repair | `python -m unittest discover -v` | nested unhashable equality and complete tree hashes |
| `crash-redelivery` | at-most-once recovery | `python -m unittest discover -v` | hidden behavior plus harness-owned crash/effect/commit ordering |

Every baseline command discovers one test and fails. Accepted calibration state passes. Hidden verifiers add held-out behavior.

## Crash task

Repository correctness alone cannot pass. Initial delivery must publish a bounded non-whitespace action ID while alive; harness kills that process. Recovery must publish the same redelivery ID. In calibration, harness—not the stub—then writes accepted state and creates a real Git commit keyed to that ID before releasing recovery. Ack must be observed only afterward.

Exactly one effect and one commit are required. Event-only fabricated commit/effect claims, missing or duplicate effects, changed/whitespace IDs, ack-before-effect, and lost committed state fail. Adapter-authored action/commit telemetry is never accepted as independent effect evidence.

Current production adapters have no independent effect/ack observer. Live crash evaluation therefore fails closed until that adapter contract and containment launcher are implemented. Calibration evidence must not be represented as production capability.

Exact accepted tree bytes remove subjective scoring. Adding an equivalent tree requires an owner-approved fixture revision and updated hashes/configuration.
