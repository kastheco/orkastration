"""Adapter-neutral trial and comparison runner."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from .bundles import BundleReadError, read_result_bundle, read_telemetry
from .fixtures import FrozenTask, prepare_fixture
from .models import AdapterManifest, AdapterMetrics, TelemetryEvent, TrialResult
from .process import run_process
from .verifier import verify


class ReadinessError(RuntimeError):
    """A live comparison was requested without two executable adapters."""


def load_adapter(path: Path) -> AdapterManifest:
    return AdapterManifest.model_validate_json(path.read_text())


def _adapter_argv(
    adapter: AdapterManifest,
    *,
    repo: Path,
    manifest: Path,
    output: Path,
    trial_id: str,
    fault_point: str | None,
) -> list[str]:
    harness_root = Path(__file__).resolve().parents[1] / "delivery-comparison"
    base_argv = [
        part.replace("{python}", sys.executable).replace("{harness_root}", str(harness_root))
        for part in adapter.argv
    ]
    argv = [
        *base_argv,
        "--repo",
        str(repo),
        "--task-manifest",
        str(manifest),
        "--output-bundle",
        str(output),
        "--trial-id",
        trial_id,
    ]
    if fault_point:
        argv.extend(["--fault-point", fault_point])
    return argv


def _telemetry_metrics(events: list[TelemetryEvent]) -> tuple[list[str], bool, bool]:
    action_counts = Counter(
        event.action_id for event in events if event.event == "action" and event.action_id
    )
    duplicates = sorted(action_id for action_id, count in action_counts.items() if count > 1)
    lost = any(event.event == "lost_committed_work" for event in events)
    kinds = {event.event for event in events}
    recovered = "crash" in kinds and "redelivery" in kinds and not lost and not duplicates
    return duplicates, lost, recovered


def run_trial(
    adapter: AdapterManifest,
    task: FrozenTask,
    *,
    trial_id: str,
    output_directory: Path,
    timeout_seconds: float = 30,
    output_limit_bytes: int = 64 * 1024,
    require_ready: bool = True,
) -> TrialResult:
    """Run one fresh trial. Readiness is checked before any command or fixture setup."""

    if require_ready and not adapter.ready:
        raise ReadinessError(f"adapter {adapter.id!r} is not ready")
    trial_root = output_directory / trial_id
    prepared = prepare_fixture(task, trial_root / "fixture")
    adapter_output = trial_root / "adapter-output"
    adapter_output.mkdir(parents=True)
    argv = _adapter_argv(
        adapter,
        repo=prepared.repo,
        manifest=prepared.public_manifest,
        output=adapter_output,
        trial_id=trial_id,
        fault_point=task.manifest.fault_point,
    )
    process = run_process(
        argv,
        cwd=prepared.repo,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        environment=adapter.environment,
    )

    bundle_error: str | None = None
    metrics = AdapterMetrics()
    events: list[TelemetryEvent] = []
    try:
        bundle = read_result_bundle(adapter_output)
        if (
            bundle.trial_id != trial_id
            or bundle.adapter_id != adapter.id
            or bundle.task_id != task.manifest.id
        ):
            raise BundleReadError("bundle identity does not match invocation")
        metrics = bundle.metrics
        events = read_telemetry(adapter_output)
    except (BundleReadError, ValidationError) as exc:
        bundle_error = str(exc)

    verifier = verify(prepared)
    duplicates, lost, recovered = _telemetry_metrics(events)
    scope_violations = verifier.unexpected_paths
    infrastructure_error = process.launch_error or verifier.infrastructure_error
    if infrastructure_error:
        classification = "infrastructure_failure"
    elif process.timed_out:
        classification = "adapter_timeout"
    elif process.exit_code != 0:
        classification = "adapter_crash"
    elif bundle_error:
        classification = "adapter_protocol_failure"
    else:
        final_state_passed = verifier.behavior_passed and verifier.exact_tree_passed
        if final_state_passed and not scope_violations and not duplicates and not lost:
            classification = "success"
        else:
            classification = "agent_failure"
    success = classification == "success"
    result = TrialResult(
        trial_id=trial_id,
        task_id=task.manifest.id,
        adapter_id=adapter.id,
        success=success,
        classification=classification,
        verifier=verifier,
        wall_time_seconds=process.wall_time_seconds,
        model_calls=metrics.model_calls,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        cost_usd=metrics.cost_usd,
        supervisor_turns=metrics.supervisor_turns,
        human_interruptions=metrics.human_interruptions,
        reviewer_calls=metrics.reviewer_calls,
        fixer_calls=metrics.fixer_calls,
        duplicate_action_ids=duplicates,
        lost_committed_work=lost,
        crash_recovery=recovered and success,
        scope_violations=scope_violations,
        infrastructure_error=infrastructure_error,
        adapter_process=process,
        bundle_error=bundle_error,
    )
    (trial_root / "trial-result.json").write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    return result


def run_comparison(
    adapters: list[AdapterManifest],
    tasks: list[FrozenTask],
    *,
    repeats: int,
    output_directory: Path,
    timeout_seconds: float = 30,
) -> list[TrialResult]:
    """Run a frozen matrix, refusing the whole comparison before partial execution."""

    if len(adapters) != 2:
        raise ReadinessError("comparison requires exactly two adapters")
    unavailable = [adapter.id for adapter in adapters if not adapter.ready or not adapter.argv]
    if unavailable:
        raise ReadinessError("comparison adapters not ready: " + ", ".join(unavailable))
    results: list[TrialResult] = []
    for task in tasks:
        for adapter in adapters:
            for repeat in range(1, repeats + 1):
                trial_id = f"{task.manifest.id}--{adapter.id}--r{repeat}"
                results.append(
                    run_trial(
                        adapter,
                        task,
                        trial_id=trial_id,
                        output_directory=output_directory,
                        timeout_seconds=timeout_seconds,
                    )
                )
    return results
