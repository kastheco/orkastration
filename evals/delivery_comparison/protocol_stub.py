"""Protocol-only fake adapter; final-state mutations stay in harness calibration control."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

CRASH_MODES = {
    "crash-redelivery",
    "crash-missing-crash",
    "crash-missing-redelivery",
    "crash-wrong-action",
    "crash-whitespace-action",
    "crash-fabricated-chain",
    "crash-event-claims-before-effect",
    "crash-missing-effect",
    "crash-duplicate-effect",
    "crash-wrong-effect-action",
    "crash-ack-before-effect",
    "crash-lost-work",
}


def event(
    identity: tuple[str, str, str], sequence: int, kind: str, action: str | None
) -> dict[str, object]:
    trial, adapter, task = identity
    return {
        "schema_version": "1",
        "trial_id": trial,
        "adapter_id": adapter,
        "task_id": task,
        "sequence": sequence,
        "event": kind,
        "action_id": action,
        "detail": "calibration protocol stub",
    }


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def _handshake(
    identity: tuple[str, str, str], action_id: str
) -> dict[str, object]:
    trial, adapter, task = identity
    return {
        "schema_version": "1",
        "trial_id": trial,
        "adapter_id": adapter,
        "task_id": task,
        "action_id": action_id,
    }


def _result(identity: tuple[str, str, str], mode: str) -> dict[str, object]:
    trial, adapter, task = identity
    status = "completed"
    if mode == "status-failed":
        status = "failed"
    elif mode in {"status-crashed", "crash"}:
        status = "crashed"
    input_tokens = 250_000 if mode == "over-token" else 120
    cost = 30.0 if mode == "over-cost" else 0.0125
    infrastructure = None
    if mode in {"service-infra-zero", "service-infra-nonzero", "false-infra"}:
        infrastructure = {
            "code": "service_unavailable",
            "evidence": "adapter-authored service claim",
        }
    return {
        "schema_version": "1",
        "trial_id": trial,
        "adapter_id": adapter,
        "task_id": task,
        "status": status,
        "summary": f"deterministic fake {mode}",
        "metrics": {
            "model_calls": 4,
            "input_tokens": input_tokens,
            "output_tokens": 30,
            "cost_usd": cost,
            "supervisor_turns": 2,
            "human_interruptions": 0,
            "reviewer_calls": 1,
            "fixer_calls": 1,
        },
        "infrastructure": infrastructure,
    }


def write_protocol(
    output: Path,
    *,
    identity: tuple[str, str, str],
    mode: str,
    events: list[dict[str, object]] | None = None,
) -> None:
    if mode == "malformed":
        (output / "result.json").write_text("{not-json\n")
        (output / "events.jsonl").write_text("")
        return
    (output / "result.json").write_text(json.dumps(_result(identity, mode), sort_keys=True) + "\n")
    (output / "events.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in (events or []))
    )


def _run_crash_recovery(
    output: Path, identity: tuple[str, str, str], mode: str
) -> None:
    if mode == "crash-fabricated-chain":
        fabricated = [
            event(identity, index + 2, kind, "action-1")
            for index, kind in enumerate(["redelivery", "action", "commit", "ack"])
        ]
        write_protocol(output, identity=identity, mode=mode, events=fabricated)
        return
    if mode == "crash-missing-redelivery":
        write_protocol(output, identity=identity, mode=mode)
        return

    action_id = "action-1"
    if mode == "crash-wrong-action":
        action_id = "wrong-action"
    if mode == "crash-event-claims-before-effect":
        claims = [
            event(identity, index + 2, kind, action_id)
            for index, kind in enumerate(["redelivery", "action", "commit", "ack"])
        ]
        (output / "events.jsonl").write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in claims)
        )
    _atomic_json(output / "redelivery-handshake.json", _handshake(identity, action_id))
    if mode == "crash-ack-before-effect":
        _atomic_json(output / "ack-handshake.json", _handshake(identity, action_id))
    deadline = time.monotonic() + 10
    while not (output / "effect-observed.json").is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    if mode != "crash-ack-before-effect" and (output / "effect-observed.json").is_file():
        _atomic_json(output / "ack-handshake.json", _handshake(identity, action_id))
    write_protocol(output, identity=identity, mode=mode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--adapter-id", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--task-manifest", type=Path, required=True)
    parser.add_argument("--output-bundle", type=Path, required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--fault-point")
    parser.add_argument("--delivery-phase", choices=("initial", "recovery"), default="recovery")
    args = parser.parse_args()
    manifest = json.loads(args.task_manifest.read_text())
    task = str(manifest["id"])
    identity = (args.trial_id, args.adapter_id, task)
    args.output_bundle.mkdir(parents=True, exist_ok=True)

    if args.delivery_phase == "initial":
        if args.mode == "crash-missing-crash":
            return 0
        action_id = "   " if args.mode == "crash-whitespace-action" else "action-1"
        _atomic_json(
            args.output_bundle / "dispatch-handshake.json",
            _handshake(identity, action_id),
        )
        time.sleep(60)
        return 0
    if args.mode == "timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (args.output_bundle / "child.pid").write_text(str(child.pid))
        print("timeout fake started", flush=True)
        time.sleep(60)
        return 0
    if args.mode == "loud":
        print("x" * 200_000)
    if args.mode in CRASH_MODES:
        _run_crash_recovery(args.output_bundle, identity, args.mode)
    else:
        standard = [
            event(identity, index, kind, "action-1")
            for index, kind in enumerate(["dispatch", "action", "commit", "ack"])
        ]
        if args.mode == "duplicate":
            standard.append(event(identity, len(standard), "action", "action-1"))
        if args.mode == "lost-work":
            standard.append(
                event(identity, len(standard), "lost_committed_work", "action-1")
            )
        write_protocol(args.output_bundle, identity=identity, mode=args.mode, events=standard)
    if args.mode in {"crash", "service-infra-nonzero"}:
        return 17
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
