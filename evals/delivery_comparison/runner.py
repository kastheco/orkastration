"""Adapter-neutral trial and comparison runner."""

from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from .bundles import BundleReadError, read_result_bundle, read_telemetry
from .calibration import apply_calibration_scenario
from .fixtures import FrozenTask, prepare_fixture
from .models import (
    AdapterManifest,
    AdapterMetrics,
    AdapterResultBundle,
    DispatchHandshake,
    FaultInjectionEvidence,
    ProcessEvidence,
    TelemetryEvent,
    TrialResult,
)
from .process import run_process, run_until_path
from .verifier import verify


class ReadinessError(RuntimeError):
    """A live comparison was requested without executable, contained adapters."""


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
    protocol_stub: Path,
    delivery_phase: str | None = None,
) -> list[str]:
    base_argv = [
        part.replace("{python}", sys.executable).replace(
            "{protocol_stub}", str(protocol_stub)
        )
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
    if delivery_phase:
        argv.extend(["--delivery-phase", delivery_phase])
    return argv


def _identity_event(
    *,
    trial_id: str,
    adapter_id: str,
    task_id: str,
    sequence: int,
    event: str,
    action_id: str,
) -> TelemetryEvent:
    return TelemetryEvent.model_validate(
        {
            "trial_id": trial_id,
            "adapter_id": adapter_id,
            "task_id": task_id,
            "sequence": sequence,
            "event": event,
            "action_id": action_id,
            "detail": "harness-observed process interruption",
        }
    )


def _fault_injection(
    adapter: AdapterManifest,
    task: FrozenTask,
    *,
    prepared_repo: Path,
    public_manifest: Path,
    adapter_output: Path,
    trial_id: str,
    timeout_seconds: float,
    output_limit_bytes: int,
    protocol_stub: Path,
) -> tuple[FaultInjectionEvidence, list[TelemetryEvent]]:
    signal_path = adapter_output / "dispatch-handshake.json"
    initial_argv = _adapter_argv(
        adapter,
        repo=prepared_repo,
        manifest=public_manifest,
        output=adapter_output,
        trial_id=trial_id,
        fault_point=task.manifest.fault_point,
        protocol_stub=protocol_stub,
        delivery_phase="initial",
    )
    process, interrupted = run_until_path(
        initial_argv,
        cwd=prepared_repo,
        signal_path=signal_path,
        timeout_seconds=min(timeout_seconds, 5),
        output_limit_bytes=output_limit_bytes,
        environment=adapter.environment,
    )
    handshake: DispatchHandshake | None = None
    error: str | None = None
    try:
        handshake = DispatchHandshake.model_validate_json(signal_path.read_text())
        if (handshake.trial_id, handshake.adapter_id, handshake.task_id) != (
            trial_id,
            adapter.id,
            task.manifest.id,
        ):
            raise ValueError("dispatch handshake identity mismatch")
    except (OSError, ValidationError, ValueError) as exc:
        error = f"invalid dispatch handshake: {exc}"
    valid = handshake is not None and interrupted
    if handshake is not None and not interrupted:
        error = "dispatch publisher was not alive when interruption was attempted"
    action_id = handshake.action_id if handshake else None
    evidence = FaultInjectionEvidence(
        requested=True,
        handshake_valid=handshake is not None,
        process_interrupted=interrupted,
        action_id=action_id,
        initial_process=process,
        error=error,
    )
    if not valid or action_id is None:
        return evidence, []
    return evidence, [
        _identity_event(
            trial_id=trial_id,
            adapter_id=adapter.id,
            task_id=task.manifest.id,
            sequence=0,
            event="dispatch",
            action_id=action_id,
        ),
        _identity_event(
            trial_id=trial_id,
            adapter_id=adapter.id,
            task_id=task.manifest.id,
            sequence=1,
            event="crash",
            action_id=action_id,
        ),
    ]


def _telemetry_metrics(events: list[TelemetryEvent]) -> tuple[list[str], bool]:
    action_ids = [event.action_id for event in events if event.event == "action"]
    counts = Counter(action_ids)
    duplicates = sorted(str(action_id) for action_id, count in counts.items() if count > 1)
    lost = any(event.event == "lost_committed_work" for event in events)
    return duplicates, lost


def _crash_chain_error(
    events: list[TelemetryEvent], fault: FaultInjectionEvidence
) -> str | None:
    if not (fault.handshake_valid and fault.process_interrupted and fault.action_id):
        return fault.error or "harness did not interrupt a live dispatched process"
    chain_events = {"dispatch", "crash", "redelivery", "action", "commit", "ack"}
    relevant = [event for event in events if event.event in chain_events]
    expected = ["dispatch", "crash", "redelivery", "action", "commit", "ack"]
    if [event.event for event in relevant] != expected:
        return "required crash chain is not exactly ordered: " + " -> ".join(
            event.event for event in relevant
        )
    if any(event.action_id != fault.action_id for event in relevant):
        return "crash chain action_id changed"
    if any(event.event == "lost_committed_work" for event in events):
        return "crash chain reported lost committed work"
    return None


def _empty_fault() -> FaultInjectionEvidence:
    return FaultInjectionEvidence(
        requested=False,
        handshake_valid=False,
        process_interrupted=False,
        action_id=None,
        initial_process=None,
    )


def _corroborated_infrastructure(
    adapter: AdapterManifest,
    bundle: AdapterResultBundle | None,
    process: ProcessEvidence,
) -> str | None:
    if bundle is None or bundle.infrastructure is None:
        return None
    marker = f"ORK_EVAL_INFRA:{bundle.infrastructure.code}"
    allowed_exits = adapter.infrastructure_exit_codes.get(bundle.infrastructure.code, [])
    if (
        (marker not in process.stdout and marker not in process.stderr)
        or process.exit_code not in allowed_exits
    ):
        return None
    return f"{bundle.infrastructure.code}: {bundle.infrastructure.evidence}"


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
    protocol_stub = trial_root / "protocol-stub.py"
    if "{protocol_stub}" in adapter.argv:
        shutil.copyfile(Path(__file__).with_name("protocol_stub.py"), protocol_stub)

    prefix_events: list[TelemetryEvent] = []
    fault = _empty_fault()
    if task.manifest.capability == "crash_redelivery":
        fault, prefix_events = _fault_injection(
            adapter,
            task,
            prepared_repo=prepared.repo,
            public_manifest=prepared.public_manifest,
            adapter_output=adapter_output,
            trial_id=trial_id,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
            protocol_stub=protocol_stub,
        )
    argv = _adapter_argv(
        adapter,
        repo=prepared.repo,
        manifest=prepared.public_manifest,
        output=adapter_output,
        trial_id=trial_id,
        fault_point=task.manifest.fault_point,
        protocol_stub=protocol_stub,
        delivery_phase="recovery" if task.manifest.capability == "crash_redelivery" else None,
    )
    process = run_process(
        argv,
        cwd=prepared.repo,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        environment=adapter.environment,
    )
    apply_calibration_scenario(prepared, adapter.calibration_scenario)

    bundle_error: str | None = None
    metrics = AdapterMetrics()
    events: list[TelemetryEvent] = prefix_events
    bundle: AdapterResultBundle | None = None
    try:
        bundle = read_result_bundle(adapter_output)
        if (
            bundle.trial_id != trial_id
            or bundle.adapter_id != adapter.id
            or bundle.task_id != task.manifest.id
        ):
            raise BundleReadError("bundle identity does not match invocation")
        metrics = bundle.metrics
        events.extend(
            read_telemetry(
                adapter_output,
                trial_id=trial_id,
                adapter_id=adapter.id,
                task_id=task.manifest.id,
                start_sequence=len(prefix_events),
            )
        )
    except (BundleReadError, ValidationError) as exc:
        bundle_error = str(exc)

    verifier = verify(prepared)
    duplicates, lost = _telemetry_metrics(events)
    crash_chain_error = (
        _crash_chain_error(events, fault)
        if task.manifest.capability == "crash_redelivery"
        else None
    )
    scope_violations = verifier.unexpected_paths
    infrastructure_error = (
        process.launch_error
        or verifier.infrastructure_error
        or _corroborated_infrastructure(adapter, bundle, process)
    )
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
        if (
            final_state_passed
            and not scope_violations
            and not duplicates
            and not lost
            and crash_chain_error is None
        ):
            classification = "success"
        else:
            classification = "agent_failure"
    success = classification == "success"
    total_wall_time = process.wall_time_seconds
    if fault.initial_process:
        total_wall_time += fault.initial_process.wall_time_seconds
    result = TrialResult(
        trial_id=trial_id,
        task_id=task.manifest.id,
        adapter_id=adapter.id,
        comparison_mode=adapter.comparison_mode,
        model_role_map=adapter.model_role_map,
        allowed_model_pool=adapter.allowed_model_pool,
        budget=adapter.budget,
        tuning_budget_hours=adapter.tuning_budget_hours,
        config_digest=adapter.config_digest,
        success=success,
        classification=classification,
        verifier=verifier,
        wall_time_seconds=round(total_wall_time, 6),
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
        crash_recovery=crash_chain_error is None and success and fault.requested,
        crash_chain_error=crash_chain_error,
        fault_injection=fault,
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
    first, second = adapters
    if (
        first.comparison_mode != second.comparison_mode
        or first.allowed_model_pool != second.allowed_model_pool
        or first.budget != second.budget
        or first.tuning_budget_hours != second.tuning_budget_hours
    ):
        raise ReadinessError(
            "comparison adapters do not share mode, pool, budget, and tuning budget"
        )
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
