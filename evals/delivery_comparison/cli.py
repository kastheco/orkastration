"""Command-line entry point for offline validation, fake calibration, and live runs."""

from __future__ import annotations

import argparse
import hashlib
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
    adapter_paths = sorted(ADAPTERS_ROOT.glob("*.json"))
    adapters = [load_adapter(path) for path in adapter_paths]
    task_ids = [task.manifest.id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("duplicate task IDs")
    adapter_ids = [adapter.id for adapter in adapters]
    if len(adapter_ids) != len(set(adapter_ids)):
        raise ValueError("duplicate adapter IDs")
    for path, adapter in zip(adapter_paths, adapters, strict=True):
        raw = json.loads(path.read_text())
        frozen = {key: value for key, value in raw.items() if key != "config_digest"}
        digest = hashlib.sha256(
            json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if digest != adapter.config_digest:
            raise ValueError(f"config digest mismatch for {adapter.id}")
    for task in tasks:
        if not task.repo_template.is_dir():
            raise ValueError(f"missing repository template for {task.manifest.id}")
        public_text = (task.root / "manifest.json").read_text()
        hidden_text = (task.root / "hidden_truth" / "truth.json").read_text()
        if hidden_text in public_text:
            raise ValueError(f"hidden truth leaked into manifest for {task.manifest.id}")
    live_ids = {
        "native-pi",
        "orkastrator",
        "native-pi-matched",
        "orkastrator-matched",
    }
    live = {adapter.id: adapter.ready for adapter in adapters if adapter.id in live_ids}
    if set(live) != live_ids:
        raise ValueError("primary and matched native-pi/orkastrator manifests are required")
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
        ("crash-redelivery", "crash-redelivery", "success"),
        ("crash-missing-crash", "crash-redelivery", "agent_failure"),
        ("crash-missing-redelivery", "crash-redelivery", "agent_failure"),
        ("crash-wrong-action", "crash-redelivery", "agent_failure"),
        ("crash-whitespace-action", "crash-redelivery", "agent_failure"),
        ("crash-fabricated-chain", "crash-redelivery", "adapter_protocol_failure"),
        ("crash-event-claims-before-effect", "crash-redelivery", "agent_failure"),
        ("crash-missing-effect", "crash-redelivery", "agent_failure"),
        ("crash-duplicate-effect", "crash-redelivery", "agent_failure"),
        ("crash-wrong-effect-action", "crash-redelivery", "agent_failure"),
        ("crash-ack-before-effect", "crash-redelivery", "agent_failure"),
        ("crash-ack-during-commit", "crash-redelivery", "agent_failure"),
        ("crash-lost-work", "crash-redelivery", "agent_failure"),
        ("wrong", "clean-bugfix", "agent_failure"),
        ("scope-escape", "clean-bugfix", "agent_failure"),
        ("timeout", "clean-bugfix", "adapter_timeout"),
        ("crash", "clean-bugfix", "adapter_crash"),
        ("malformed", "clean-bugfix", "adapter_protocol_failure"),
        ("duplicate", "clean-bugfix", "agent_failure"),
        ("lost-work", "clean-bugfix", "agent_failure"),
        ("infrastructure-failure", "clean-bugfix", "infrastructure_failure"),
        ("initial-infrastructure-failure", "crash-redelivery", "infrastructure_failure"),
        ("service-infra-zero", "clean-bugfix", "agent_failure"),
        ("service-infra-nonzero", "clean-bugfix", "adapter_crash"),
        ("false-infra", "clean-bugfix", "agent_failure"),
        ("status-failed", "clean-bugfix", "agent_failure"),
        ("status-crashed", "clean-bugfix", "agent_failure"),
        ("over-token", "clean-bugfix", "agent_failure"),
        ("over-cost", "clean-bugfix", "agent_failure"),
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
            require_ready=False,
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
    run_parser.add_argument(
        "--comparison-mode",
        choices=("tuned-primary", "matched-role-ablation"),
        default="tuned-primary",
    )
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
    adapter_files = {
        "tuned-primary": ("native-pi.json", "orkastrator.json"),
        "matched-role-ablation": ("native-pi-matched.json", "orkastrator-matched.json"),
    }
    adapters = [
        load_adapter(ADAPTERS_ROOT / filename)
        for filename in adapter_files[args.comparison_mode]
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
