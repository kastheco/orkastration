"""Deterministic fake adapter used only to calibrate the local harness."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

FIXES = {
    "clean-bugfix": '''"""Small text helpers."""\n\n\ndef normalize_label(value: str) -> str:\n    """Trim a label and collapse internal whitespace to one space."""\n    return " ".join(value.split())\n''',
    "hidden-edge": '''"""Collection helpers."""\n\nfrom typing import Any\n\n\ndef dedupe_stable(values: list[Any]) -> list[Any]:\n    """Return equal values once while preserving first-seen order."""\n    result: list[Any] = []\n    for value in values:\n        if not any(value == existing for existing in result):\n            result.append(value)\n    return result\n''',
    "crash-redelivery": '''"""Delivery application logic."""\n\nfrom typing import Any\n\n\ndef apply_delivery(state: dict[str, Any], action_id: str, amount: int) -> dict[str, Any]:\n    """Apply an increment at most once for a stable action identifier."""\n    applied = state.setdefault("applied", [])\n    if action_id in applied:\n        return state\n    state["total"] = int(state.get("total", 0)) + amount\n    applied.append(action_id)\n    return state\n''',
}
TARGETS = {
    "clean-bugfix": "text_utils.py",
    "hidden-edge": "collections_ext.py",
    "crash-redelivery": "worker.py",
}
PROTECTED = {
    "clean-bugfix": "test_text_utils.py",
    "hidden-edge": "test_collections_ext.py",
    "crash-redelivery": "test_worker.py",
}


def write_protocol(output: Path, *, trial: str, adapter: str, task: str, mode: str) -> None:
    if mode == "malformed":
        (output / "result.json").write_text("{not-json\n")
        (output / "events.jsonl").write_text("")
        return
    status = "crashed" if mode == "crash" else "completed"
    result = {
        "schema_version": "1",
        "trial_id": trial,
        "adapter_id": adapter,
        "task_id": task,
        "status": status,
        "summary": f"deterministic fake {mode}",
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
        "infrastructure_error": None,
    }
    (output / "result.json").write_text(json.dumps(result, sort_keys=True) + "\n")
    if task == "crash-redelivery" or mode == "crash-redelivery":
        kinds = ["dispatch", "crash", "redelivery", "action", "commit", "ack"]
    else:
        kinds = ["dispatch", "action", "commit", "ack"]
    events = []
    for sequence, kind in enumerate(kinds):
        events.append(
            {
                "schema_version": "1",
                "sequence": sequence,
                "event": kind,
                "action_id": "action-1" if kind in {"action", "commit", "ack"} else None,
                "detail": mode,
            }
        )
    if mode == "duplicate":
        duplicate = dict(events[-3])
        duplicate["sequence"] = len(events)
        duplicate["event"] = "action"
        events.append(duplicate)
    if mode == "lost-work":
        events.append(
            {
                "schema_version": "1",
                "sequence": len(events),
                "event": "lost_committed_work",
                "action_id": "action-1",
                "detail": "calibration evidence",
            }
        )
    (output / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
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
    args = parser.parse_args()
    manifest = json.loads(args.task_manifest.read_text())
    task = str(manifest["id"])
    args.output_bundle.mkdir(parents=True, exist_ok=True)
    if args.mode == "timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (args.output_bundle / "child.pid").write_text(str(child.pid))
        print("timeout fake started", flush=True)
        time.sleep(60)
        return 0
    if args.mode == "loud":
        print("x" * 200_000)
    if args.mode not in {"wrong", "malformed"}:
        (args.repo / TARGETS[task]).write_text(FIXES[task])
    if args.mode == "scope-escape":
        protected = args.repo / PROTECTED[task]
        protected.write_text(protected.read_text() + "\n# adapter changed protected test\n")
    write_protocol(
        args.output_bundle,
        trial=args.trial_id,
        adapter=args.adapter_id,
        task=task,
        mode=args.mode,
    )
    return 17 if args.mode == "crash" else 0


if __name__ == "__main__":
    raise SystemExit(main())
