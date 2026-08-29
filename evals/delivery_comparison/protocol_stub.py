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
    "crash-duplicate",
    "crash-lost-work",
    "crash-unordered",
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


def write_protocol(
    output: Path,
    *,
    identity: tuple[str, str, str],
    mode: str,
    phase: str,
) -> None:
    trial, adapter, task = identity
    if mode == "malformed":
        (output / "result.json").write_text("{not-json\n")
        (output / "events.jsonl").write_text("")
        return
    infrastructure = None
    if mode in {"service-infra-zero", "service-infra-nonzero", "false-infra"}:
        infrastructure = {
            "code": "service_unavailable",
            "evidence": "calibration service probe unavailable",
        }
    result = {
        "schema_version": "1",
        "trial_id": trial,
        "adapter_id": adapter,
        "task_id": task,
        "status": "crashed" if mode == "crash" else "completed",
        "summary": (
            "infrastructure maybe" if mode == "false-infra" else f"deterministic fake {mode}"
        ),
        "metrics": {
            "model_calls": 4,
            "input_tokens": 120,
            "output_tokens": 30,
            "cost_usd": 0.0125,
            "supervisor_turns": 2,
            "human_interruptions": 0,
            "reviewer_calls": 1,
            "fixer_calls": 1,
        },
        "infrastructure": infrastructure,
    }
    (output / "result.json").write_text(json.dumps(result, sort_keys=True) + "\n")
    if mode in CRASH_MODES:
        start = 0 if mode == "crash-missing-crash" else 2
        chain: list[tuple[str, str | None]] = [
            ("redelivery", "action-1"),
            ("action", "action-1"),
            ("commit", "action-1"),
            ("ack", "action-1"),
        ]
        if mode == "crash-missing-redelivery":
            chain = chain[1:]
        elif mode == "crash-wrong-action":
            chain[0] = ("redelivery", "wrong-action")
        elif mode == "crash-duplicate":
            chain.insert(2, ("action", "action-1"))
        elif mode == "crash-lost-work":
            chain.append(("lost_committed_work", "action-1"))
        elif mode == "crash-unordered":
            chain = [chain[0], chain[2], chain[1], chain[3]]
        events = [
            event(identity, start + index, kind, action)
            for index, (kind, action) in enumerate(chain)
        ]
    else:
        standard_chain = ["dispatch", "action", "commit", "ack"]
        events = [
            event(identity, index, kind, "action-1")
            for index, kind in enumerate(standard_chain)
        ]
        if mode == "duplicate":
            events.append(event(identity, len(events), "action", "action-1"))
        if mode == "lost-work":
            events.append(event(identity, len(events), "lost_committed_work", "action-1"))
    (output / "events.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events)
    )


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
        handshake = {
            "schema_version": "1",
            "trial_id": args.trial_id,
            "adapter_id": args.adapter_id,
            "task_id": task,
            "action_id": "action-1",
        }
        handshake_temporary = args.output_bundle / "dispatch-handshake.tmp"
        handshake_temporary.write_text(json.dumps(handshake, sort_keys=True) + "\n")
        handshake_temporary.replace(args.output_bundle / "dispatch-handshake.json")
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
    if args.mode in {"service-infra-zero", "service-infra-nonzero"}:
        print("ORK_EVAL_INFRA:service_unavailable", flush=True)
    write_protocol(args.output_bundle, identity=identity, mode=args.mode, phase=args.delivery_phase)
    if args.mode in {"crash", "service-infra-nonzero"}:
        return 17
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
