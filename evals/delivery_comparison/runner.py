"""Adapter-neutral trial and comparison runner."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .bundles import BundleReadError, read_result_bundle, read_telemetry
from .calibration import apply_calibration_scenario, apply_crash_effect
from .fixtures import FrozenTask, prepare_fixture
from .models import (
    AckHandshake,
    AdapterManifest,
    AdapterMetrics,
    AdapterResultBundle,
    DispatchHandshake,
    ExternalEffectEvidence,
    FaultInjectionEvidence,
    HostInfrastructureCode,
    ProcessEvidence,
    RecoveryHandshake,
    TelemetryEvent,
    TrialResult,
)
from .process import run_process, run_until_path, run_with_checkpoint
from .verifier import verify


class ReadinessError(RuntimeError):
    """A live comparison was requested without a harness-owned containment launcher."""


_CONTAINMENT_LAUNCHER_ALLOWLIST: frozenset[str] = frozenset()


def _configuration_digest(value: dict[str, object]) -> str:
    frozen = {key: item for key, item in value.items() if key != "config_digest"}
    return hashlib.sha256(
        json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_adapter_digest(adapter: AdapterManifest) -> None:
    value = adapter.model_dump(mode="json")
    if _configuration_digest(value) != adapter.config_digest:
        raise ReadinessError(f"adapter {adapter.id!r} configuration digest mismatch")


def load_adapter(path: Path) -> AdapterManifest:
    raw = json.loads(path.read_text())
    adapter = AdapterManifest.model_validate(raw)
    if _configuration_digest(raw) != adapter.config_digest:
        raise ReadinessError(f"adapter {adapter.id!r} configuration digest mismatch")
    return adapter


def _preflight_semantics(adapter: AdapterManifest) -> None:
    verify_adapter_digest(adapter)
    if adapter.comparison_mode == "sol-high-diagnostic":
        routes = list(adapter.model_role_map.values())
        if not routes or {route.model for route in routes} != {"sol"}:
            raise ReadinessError("sol-high diagnostic must route every role to sol")
        if any(route.thinking != "high" for route in routes):
            raise ReadinessError("sol-high diagnostic requires high thinking for every role")


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
    detail: str,
) -> TelemetryEvent:
    return TelemetryEvent.model_validate(
        {
            "trial_id": trial_id,
            "adapter_id": adapter_id,
            "task_id": task_id,
            "sequence": sequence,
            "event": event,
            "action_id": action_id,
            "detail": detail,
        }
    )


def _read_handshake(
    path: Path,
    expected_type: type[DispatchHandshake] | type[RecoveryHandshake] | type[AckHandshake],
    *,
    trial_id: str,
    adapter_id: str,
    task_id: str,
) -> DispatchHandshake | RecoveryHandshake | AckHandshake:
    with path.open("rb") as stream:
        payload = stream.read(4097)
    if len(payload) > 4096:
        raise ValueError("handshake exceeds 4096-byte limit")
    handshake = expected_type.model_validate_json(payload)
    if (handshake.trial_id, handshake.adapter_id, handshake.task_id) != (
        trial_id,
        adapter_id,
        task_id,
    ):
        raise ValueError("handshake identity mismatch")
    return handshake


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
        parsed = _read_handshake(
            signal_path,
            DispatchHandshake,
            trial_id=trial_id,
            adapter_id=adapter.id,
            task_id=task.manifest.id,
        )
        assert isinstance(parsed, DispatchHandshake)
        handshake = parsed
    except (OSError, ValidationError, ValueError) as exc:
        error = f"invalid dispatch handshake: {exc}"[:1000]
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
            detail="harness observed live dispatch",
        ),
        _identity_event(
            trial_id=trial_id,
            adapter_id=adapter.id,
            task_id=task.manifest.id,
            sequence=1,
            event="crash",
            action_id=action_id,
            detail="harness killed initial process group",
        ),
    ]


def _empty_effect() -> ExternalEffectEvidence:
    return ExternalEffectEvidence(
        redelivery_observed=False,
        effect_count=0,
        commit_count=0,
        action_id=None,
        commit_sha=None,
        release_nonce_published=False,
        ack_observed=False,
    )


@dataclass
class _RecoveryState:
    redelivery: bool = False
    effect_count: int = 0
    commit_count: int = 0
    action_id: str | None = None
    commit_sha: str | None = None
    release_nonce: str | None = None
    error: str | None = None
    ack_preexisting: bool = False


def _run_crash_recovery(
    adapter: AdapterManifest,
    task: FrozenTask,
    *,
    prepared: object,
    public_manifest: Path,
    adapter_output: Path,
    trial_id: str,
    timeout_seconds: float,
    output_limit_bytes: int,
    protocol_stub: Path,
    fault: FaultInjectionEvidence,
    owned_events: list[TelemetryEvent],
) -> tuple[ProcessEvidence, ExternalEffectEvidence, list[TelemetryEvent]]:
    # Kept local to avoid exposing hidden fixture types in adapter-facing protocol code.
    from .fixtures import PreparedFixture

    assert isinstance(prepared, PreparedFixture)
    redelivery_path = adapter_output / "redelivery-handshake.json"
    ack_path = adapter_output / "ack-handshake.json"
    continue_path = adapter_output / "effect-observed.json"
    state = _RecoveryState()

    def observe_and_apply() -> None:
        try:
            parsed = _read_handshake(
                redelivery_path,
                RecoveryHandshake,
                trial_id=trial_id,
                adapter_id=adapter.id,
                task_id=task.manifest.id,
            )
            assert isinstance(parsed, RecoveryHandshake)
            state.redelivery = True
            state.action_id = parsed.action_id
            telemetry_path = adapter_output / "events.jsonl"
            if telemetry_path.is_file():
                if telemetry_path.stat().st_size > 64 * 1024:
                    state.error = "adapter telemetry exceeded pre-effect inspection bound"
                else:
                    pre_effect_events = telemetry_path.read_text().splitlines()
                    claimed = {
                        json.loads(line).get("event")
                        for line in pre_effect_events
                        if line.strip()
                    }
                    if claimed & {"action", "commit", "ack"}:
                        state.error = "adapter claimed effect/commit/ack before harness observation"
            if ack_path.exists():
                state.ack_preexisting = True
                state.error = "ack was published before harness-owned effect"
            if parsed.action_id != fault.action_id:
                state.error = "redelivery action_id changed"
                mutation = None
            elif state.ack_preexisting or state.error:
                mutation = None
            else:
                mutation = apply_crash_effect(
                    prepared, adapter.calibration_scenario, parsed.action_id
                )
            if mutation is not None:
                state.effect_count = mutation.effect_count
                state.commit_count = mutation.commit_count
                state.action_id = mutation.action_id
                state.commit_sha = mutation.commit_sha
                if (
                    mutation.effect_count == 1
                    and mutation.commit_count == 1
                    and mutation.action_id == fault.action_id
                ):
                    # Generated only after apply_crash_effect returns from its Git commit.
                    state.release_nonce = secrets.token_urlsafe(32)
        except (OSError, ValidationError, ValueError) as exc:
            state.error = f"invalid redelivery handshake: {exc}"
        release_payload = json.dumps(
            {
                "schema_version": "1",
                "action_id": fault.action_id,
                "effect_count": state.effect_count,
                "commit_count": state.commit_count,
                "release_nonce": state.release_nonce,
            },
            sort_keys=True,
        ) + "\n"
        release_temporary = continue_path.with_suffix(".tmp")
        release_temporary.write_text(release_payload)
        release_temporary.replace(continue_path)

    argv = _adapter_argv(
        adapter,
        repo=prepared.repo,
        manifest=public_manifest,
        output=adapter_output,
        trial_id=trial_id,
        fault_point=task.manifest.fault_point,
        protocol_stub=protocol_stub,
        delivery_phase="recovery",
    )
    process, checkpoint_seen, callback_error = run_with_checkpoint(
        argv,
        cwd=prepared.repo,
        checkpoint_path=redelivery_path,
        on_checkpoint=observe_and_apply,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
        environment=adapter.environment,
    )
    if not checkpoint_seen and state.error is None:
        state.error = "no harness-observed redelivery checkpoint"
    if callback_error:
        state.error = callback_error

    ack_observed = False
    if (
        not state.ack_preexisting
        and state.effect_count == 1
        and state.commit_count == 1
        and state.release_nonce
    ):
        try:
            parsed_ack = _read_handshake(
                ack_path,
                AckHandshake,
                trial_id=trial_id,
                adapter_id=adapter.id,
                task_id=task.manifest.id,
            )
            assert isinstance(parsed_ack, AckHandshake)
            if parsed_ack.action_id != fault.action_id:
                raise ValueError("ack action_id changed")
            if not secrets.compare_digest(parsed_ack.release_nonce, state.release_nonce):
                raise ValueError("ack release nonce mismatch")
            ack_observed = True
        except (OSError, ValidationError, ValueError) as exc:
            state.error = f"invalid post-effect ack: {exc}"

    effect = ExternalEffectEvidence(
        redelivery_observed=state.redelivery,
        effect_count=state.effect_count,
        commit_count=state.commit_count,
        action_id=state.action_id,
        commit_sha=state.commit_sha,
        release_nonce_published=state.release_nonce is not None,
        ack_observed=ack_observed,
        error=state.error[:1000] if state.error else None,
    )
    sequence = len(owned_events)
    if effect.redelivery_observed and fault.action_id:
        owned_events.append(
            _identity_event(
                trial_id=trial_id,
                adapter_id=adapter.id,
                task_id=task.manifest.id,
                sequence=sequence,
                event="redelivery",
                action_id=fault.action_id,
                detail="harness validated recovery checkpoint",
            )
        )
        sequence += 1
    if effect.effect_count == 1 and effect.commit_count == 1 and effect.action_id:
        for kind in ("action", "commit"):
            owned_events.append(
                _identity_event(
                    trial_id=trial_id,
                    adapter_id=adapter.id,
                    task_id=task.manifest.id,
                    sequence=sequence,
                    event=kind,
                    action_id=effect.action_id,
                    detail="harness applied and Git-observed external effect",
                )
            )
            sequence += 1
    if effect.ack_observed and fault.action_id:
        owned_events.append(
            _identity_event(
                trial_id=trial_id,
                adapter_id=adapter.id,
                task_id=task.manifest.id,
                sequence=sequence,
                event="ack",
                action_id=fault.action_id,
                detail="harness observed ack after committed effect",
            )
        )
    return process, effect, owned_events


def _telemetry_metrics(events: list[TelemetryEvent]) -> tuple[list[str], bool]:
    action_ids = [event.action_id for event in events if event.event == "action"]
    counts = Counter(action_ids)
    duplicates = sorted(str(action_id) for action_id, count in counts.items() if count > 1)
    lost = any(event.event == "lost_committed_work" for event in events)
    return duplicates, lost


def _crash_chain_error(
    fault: FaultInjectionEvidence, effect: ExternalEffectEvidence, lost: bool
) -> str | None:
    if not (fault.handshake_valid and fault.process_interrupted and fault.action_id):
        return fault.error or "harness did not interrupt a live dispatched process"
    if effect.error:
        return effect.error
    if not effect.redelivery_observed:
        return "no harness-observed redelivery"
    if effect.effect_count != 1 or effect.commit_count != 1:
        return (
            "expected exactly one harness-owned effect and commit; observed "
            f"{effect.effect_count} effect(s), {effect.commit_count} commit(s)"
        )
    if effect.action_id != fault.action_id:
        return "harness-owned effect action_id changed"
    if not effect.ack_observed:
        return "no harness-observed post-effect ack"
    if lost:
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


def _host_infrastructure(
    fault: FaultInjectionEvidence,
    process: ProcessEvidence,
    verifier_error: str | None,
) -> tuple[HostInfrastructureCode | None, str | None]:
    if fault.initial_process and fault.initial_process.launch_error:
        return "initial_phase_launch_failure", fault.initial_process.launch_error
    if process.launch_error:
        return "adapter_launch_failure", process.launch_error
    if verifier_error:
        return "verifier_failure", verifier_error
    return None, None


def _budget_violations(
    adapter: AdapterManifest, metrics: AdapterMetrics, wall_time: float
) -> list[str]:
    violations: list[str] = []
    total_tokens = metrics.input_tokens + metrics.output_tokens
    if total_tokens > adapter.budget.max_total_tokens:
        violations.append(
            f"tokens {total_tokens} exceed cap {adapter.budget.max_total_tokens}"
        )
    if metrics.cost_usd > adapter.budget.max_cost_usd:
        violations.append(
            f"cost {metrics.cost_usd} exceeds cap {adapter.budget.max_cost_usd}"
        )
    if wall_time > adapter.budget.max_wall_seconds:
        violations.append(
            f"wall time {wall_time} exceeds cap {adapter.budget.max_wall_seconds}"
        )
    return violations


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
    """Run one fresh trial. Live readiness is checked before fixture creation."""

    verify_adapter_digest(adapter)
    if require_ready:
        _preflight_semantics(adapter)
        raise ReadinessError("no harness-owned containment launcher is allowlisted")
    effective_timeout = min(timeout_seconds, float(adapter.budget.max_wall_seconds))
    trial_root = output_directory / trial_id
    prepared = prepare_fixture(task, trial_root / "fixture")
    adapter_output = trial_root / "adapter-output"
    adapter_output.mkdir(parents=True)
    protocol_stub = trial_root / "protocol-stub.py"
    if "{protocol_stub}" in adapter.argv:
        shutil.copyfile(Path(__file__).with_name("protocol_stub.py"), protocol_stub)

    owned_events: list[TelemetryEvent] = []
    fault = _empty_fault()
    effect = _empty_effect()
    if task.manifest.capability == "crash_redelivery":
        fault, owned_events = _fault_injection(
            adapter,
            task,
            prepared_repo=prepared.repo,
            public_manifest=prepared.public_manifest,
            adapter_output=adapter_output,
            trial_id=trial_id,
            timeout_seconds=effective_timeout,
            output_limit_bytes=output_limit_bytes,
            protocol_stub=protocol_stub,
        )
        initial_wall = fault.initial_process.wall_time_seconds if fault.initial_process else 0.0
        remaining = max(0.001, effective_timeout - initial_wall)
        process, effect, owned_events = _run_crash_recovery(
            adapter,
            task,
            prepared=prepared,
            public_manifest=prepared.public_manifest,
            adapter_output=adapter_output,
            trial_id=trial_id,
            timeout_seconds=remaining,
            output_limit_bytes=output_limit_bytes,
            protocol_stub=protocol_stub,
            fault=fault,
            owned_events=owned_events,
        )
        if adapter.calibration_scenario == "crash-lost-work":
            target = task.manifest.allowed_write_paths[0]
            shutil.copyfile(task.repo_template / target, prepared.repo / target)
            if fault.action_id:
                owned_events.append(
                    _identity_event(
                        trial_id=trial_id,
                        adapter_id=adapter.id,
                        task_id=task.manifest.id,
                        sequence=len(owned_events),
                        event="lost_committed_work",
                        action_id=fault.action_id,
                        detail="harness observed committed effect disappear",
                    )
                )
    else:
        argv = _adapter_argv(
            adapter,
            repo=prepared.repo,
            manifest=prepared.public_manifest,
            output=adapter_output,
            trial_id=trial_id,
            fault_point=task.manifest.fault_point,
            protocol_stub=protocol_stub,
        )
        process = run_process(
            argv,
            cwd=prepared.repo,
            timeout_seconds=effective_timeout,
            output_limit_bytes=output_limit_bytes,
            environment=adapter.environment,
        )
        apply_calibration_scenario(prepared, adapter.calibration_scenario)

    bundle_error: str | None = None
    metrics = AdapterMetrics()
    adapter_events: list[TelemetryEvent] = []
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
        adapter_events = read_telemetry(
            adapter_output,
            trial_id=trial_id,
            adapter_id=adapter.id,
            task_id=task.manifest.id,
            start_sequence=len(owned_events) if not owned_events else 0,
        )
    except (BundleReadError, ValidationError) as exc:
        bundle_error = str(exc)

    verifier = verify(prepared)
    all_events = [*owned_events, *adapter_events]
    duplicates, lost = _telemetry_metrics(all_events)
    crash_chain_error = (
        _crash_chain_error(fault, effect, lost)
        if task.manifest.capability == "crash_redelivery"
        else None
    )
    scope_violations = verifier.unexpected_paths
    infrastructure_code, infrastructure_error = _host_infrastructure(
        fault, process, verifier.infrastructure_error
    )
    total_wall_time = process.wall_time_seconds
    if fault.initial_process:
        total_wall_time += fault.initial_process.wall_time_seconds
    total_wall_time = round(total_wall_time, 6)
    budget_violations = _budget_violations(adapter, metrics, total_wall_time)
    status_completed = bundle is not None and bundle.status == "completed"

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
            and status_completed
            and not scope_violations
            and not duplicates
            and not lost
            and not budget_violations
            and crash_chain_error is None
        ):
            classification = "success"
        else:
            classification = "agent_failure"
    success = classification == "success"
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
        wall_time_seconds=total_wall_time,
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
        external_effect=effect,
        scope_violations=scope_violations,
        budget_violations=budget_violations,
        infrastructure_code=infrastructure_code,
        infrastructure_error=infrastructure_error,
        adapter_process=process,
        bundle_status=bundle.status if bundle else None,
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
    for adapter in adapters:
        _preflight_semantics(adapter)
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
    if (
        first.comparison_mode == "matched-role-ablation"
        and first.model_role_map != second.model_role_map
    ):
        raise ReadinessError("matched-role ablation requires identical role maps")
    unavailable = [adapter.id for adapter in adapters if not adapter.ready or not adapter.argv]
    if unavailable:
        raise ReadinessError("comparison adapters not ready: " + ", ".join(unavailable))
    if any(adapter.id not in _CONTAINMENT_LAUNCHER_ALLOWLIST for adapter in adapters):
        raise ReadinessError("no harness-owned containment launcher is allowlisted")
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
                        timeout_seconds=min(
                            timeout_seconds, float(adapter.budget.max_wall_seconds)
                        ),
                    )
                )
    return results
