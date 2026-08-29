"""Command-line entry point for offline validation, fake calibration, and live runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .fixtures import discover_tasks
from .models import TrialResult
from .report import build_report, write_report
from .runner import ReadinessError, load_adapter, run_comparison, run_trial

HARNESS_ROOT = Path(__file__).resolve().parents[1] / "delivery-comparison"
ADAPTERS_ROOT = HARNESS_ROOT / "adapters"
FIXTURES_ROOT = HARNESS_ROOT / "fixtures"


def validate_contracts() -> dict[str, object]:
    tasks = discover_tasks(FIXTURES_ROOT)
    adapters = [load_adapter(path) for path in sorted(ADAPTERS_ROOT.glob("*.json"))]
    task_ids = [task.manifest.id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("duplicate task IDs")
    adapter_ids = [adapter.id for adapter in adapters]
    if len(adapter_ids) != len(set(adapter_ids)):
        raise ValueError("duplicate adapter IDs")
    for task in tasks:
        if not task.repo_template.is_dir():
            raise ValueError(f"missing repository template for {task.manifest.id}")
        public_text = (task.root / "manifest.json").read_text()
        hidden_text = (task.root / "hidden_truth" / "truth.json").read_text()
        if hidden_text in public_text:
            raise ValueError(f"hidden truth leaked into manifest for {task.manifest.id}")
    live = {
        adapter.id: adapter.ready
        for adapter in adapters
        if adapter.id in {"native-pi", "orkastrator"}
    }
    if set(live) != {"native-pi", "orkastrator"}:
        raise ValueError("native-pi and orkastrator manifests are required")
    return {
        "status": "valid",
        "tasks": task_ids,
        "adapter_count": len(adapters),
        "live_readiness": live,
        "live_run_ready": all(live.values()),
        "commands_executed": 0,
    }


def calibrate(output: Path) -> list[TrialResult]:
    tasks = {task.manifest.id: task for task in discover_tasks(FIXTURES_ROOT)}
    cases = [
        ("success", "clean-bugfix", "success"),
        ("success", "hidden-edge", "success"),
        ("success", "crash-redelivery", "success"),
        ("wrong", "clean-bugfix", "agent_failure"),
        ("scope-escape", "clean-bugfix", "agent_failure"),
        ("timeout", "clean-bugfix", "adapter_timeout"),
        ("crash", "clean-bugfix", "adapter_crash"),
        ("malformed", "clean-bugfix", "adapter_protocol_failure"),
        ("duplicate", "clean-bugfix", "agent_failure"),
        ("lost-work", "clean-bugfix", "agent_failure"),
        ("infrastructure-failure", "clean-bugfix", "infrastructure_failure"),
        ("loud", "clean-bugfix", "success"),
    ]
    results = []
    for index, (fake, task_id, expected) in enumerate(cases, start=1):
        adapter = load_adapter(ADAPTERS_ROOT / f"fake-{fake}.json")
        result = run_trial(
            adapter,
            tasks[task_id],
            trial_id=f"calibration-{index:02d}-{fake}-{task_id}",
            output_directory=output / "trials",
            timeout_seconds=0.3 if fake == "timeout" else 10,
            output_limit_bytes=1024,
        )
        if result.classification != expected:
            raise RuntimeError(
                f"calibration mismatch for {fake}: {result.classification}, expected {expected}"
            )
        results.append(result)
    write_report(build_report(results), output)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orkastrator-delivery-eval")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate contracts without invoking adapters")
    calibrate_parser = commands.add_parser("calibrate", help="run deterministic fake adapters only")
    calibrate_parser.add_argument("--output", type=Path, required=True)
    run_parser = commands.add_parser("run", help="run the live comparison matrix")
    run_parser.add_argument("--allow-live", action="store_true")
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--repeats", type=int, default=3)
    run_parser.add_argument("--timeout-seconds", type=float, default=1800)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        print(json.dumps(validate_contracts(), indent=2, sort_keys=True))
        return 0
    if args.command == "calibrate":
        results = calibrate(args.output)
        counts: dict[str, int] = {}
        for result in results:
            counts[result.classification] = counts.get(result.classification, 0) + 1
        print(json.dumps({"trials": len(results), "classifications": counts}, sort_keys=True))
        return 0
    if not args.allow_live:
        raise SystemExit("run requires explicit --allow-live")
    adapters = [
        load_adapter(ADAPTERS_ROOT / "native-pi.json"),
        load_adapter(ADAPTERS_ROOT / "orkastrator.json"),
    ]
    tasks = discover_tasks(FIXTURES_ROOT)
    try:
        results = run_comparison(
            adapters,
            tasks,
            repeats=args.repeats,
            output_directory=args.output / "trials",
            timeout_seconds=args.timeout_seconds,
        )
    except ReadinessError as exc:
        raise SystemExit(str(exc)) from exc
    write_report(build_report(results), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
