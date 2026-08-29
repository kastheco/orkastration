from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import ValidationError

from evals.delivery_comparison.bundles import BundleReadError, read_telemetry
from evals.delivery_comparison.calibration import apply_calibration_scenario
from evals.delivery_comparison.cli import (
    ADAPTERS_ROOT,
    FIXTURES_ROOT,
    calibrate,
    validate_contracts,
)
from evals.delivery_comparison.fixtures import (
    FrozenTask,
    discover_tasks,
    prepare_fixture,
    reset_fixture,
    snapshot_tree,
)
from evals.delivery_comparison.models import (
    AdapterManifest,
    DispatchHandshake,
    TelemetryEvent,
    TrialResult,
)
from evals.delivery_comparison.process import run_process
from evals.delivery_comparison.report import build_report, write_report
from evals.delivery_comparison.runner import (
    ReadinessError,
    _adapter_argv,
    _configuration_digest,
    _read_handshake,
    load_adapter,
    run_comparison,
    run_trial,
)


def tasks_by_id() -> dict[str, FrozenTask]:
    return {task.manifest.id: task for task in discover_tasks(FIXTURES_ROOT)}


def run_fake(
    adapter_name: str, task: FrozenTask, trial_id: str, output: Path
) -> TrialResult:
    return run_trial(
        load_adapter(ADAPTERS_ROOT / f"{adapter_name}.json"),
        task,
        trial_id=trial_id,
        output_directory=output,
        require_ready=False,
    )


def test_validate_is_no_run_and_live_adapters_are_unready() -> None:
    evidence = validate_contracts()
    assert evidence["commands_executed"] == 0
    assert evidence["live_run_ready"] is False
    assert evidence["live_readiness"] == {
        "native-pi": False,
        "native-pi-matched": False,
        "orkastrator": False,
        "orkastrator-matched": False,
    }


@pytest.mark.parametrize("task", discover_tasks(FIXTURES_ROOT), ids=lambda task: task.manifest.id)
def test_visible_baseline_command_discovers_a_test_and_exposes_wrong_behavior(
    task: FrozenTask, tmp_path: Path
) -> None:
    prepared = prepare_fixture(task, tmp_path / task.manifest.id)
    baseline = run_process(
        task.manifest.visible_test_argv,
        cwd=prepared.repo,
        timeout_seconds=10,
    )
    assert baseline.exit_code != 0
    assert "Ran 1 test" in baseline.stderr

    apply_calibration_scenario(prepared, "success")
    repaired = run_process(
        task.manifest.visible_test_argv,
        cwd=prepared.repo,
        timeout_seconds=10,
    )
    assert repaired.exit_code == 0
    assert "Ran 1 test" in repaired.stderr


def test_public_invocation_material_excludes_hidden_and_accepted_content(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    prepared = prepare_fixture(task, tmp_path / "trial")
    adapter = load_adapter(ADAPTERS_ROOT / "fake-success.json")
    output = tmp_path / "output"
    protocol_stub_path = tmp_path / "protocol-stub.py"
    shutil.copyfile(Path("evals/delivery_comparison/protocol_stub.py"), protocol_stub_path)
    argv = _adapter_argv(
        adapter,
        repo=prepared.repo,
        manifest=prepared.public_manifest,
        output=output,
        trial_id="material-test",
        fault_point=None,
        protocol_stub=protocol_stub_path,
    )
    material = "\n".join([*argv, str(prepared.repo), json.dumps(adapter.environment)])
    public = prepared.public_manifest.read_text()
    protocol_stub = Path(argv[1]).read_text()
    accepted_source_path = task.root / "hidden_truth" / "accepted_source.py"
    accepted_source = accepted_source_path.read_text()
    assert "hidden_truth" not in material
    assert "truth.json" not in material
    assert "calibration.py" not in material
    assert "accepted_source.py" not in material
    assert "expected_files_sha256" not in public
    assert "verifier.py" not in public
    assert "return \" \".join(value.split())" not in protocol_stub
    assert "return \" \".join(value.split())" in accepted_source
    assert not (prepared.trial_root / "hidden_truth").exists()


def test_primary_and_matched_modes_freeze_fair_configuration_fields() -> None:
    native = load_adapter(ADAPTERS_ROOT / "native-pi.json")
    orkastrator = load_adapter(ADAPTERS_ROOT / "orkastrator.json")
    assert native.comparison_mode == orkastrator.comparison_mode == "tuned-primary"
    assert native.allowed_model_pool == orkastrator.allowed_model_pool
    assert native.budget == orkastrator.budget
    assert native.tuning_budget_hours == orkastrator.tuning_budget_hours
    assert all(route.thinking == "high" for route in native.model_role_map.values())

    native_matched = load_adapter(ADAPTERS_ROOT / "native-pi-matched.json")
    orkastrator_matched = load_adapter(ADAPTERS_ROOT / "orkastrator-matched.json")
    assert native_matched.comparison_mode == "matched-role-ablation"
    assert orkastrator_matched.comparison_mode == "matched-role-ablation"
    assert native_matched.model_role_map == orkastrator_matched.model_role_map
    assert native_matched.allowed_model_pool == orkastrator_matched.allowed_model_pool
    assert native_matched.budget == orkastrator_matched.budget
    assert native_matched.tuning_budget_hours == orkastrator_matched.tuning_budget_hours


def _refreeze(adapter: AdapterManifest) -> AdapterManifest:
    adapter.config_digest = _configuration_digest(adapter.model_dump(mode="json"))
    return adapter


def test_live_preflight_enforces_digest_modes_and_matched_roles(tmp_path: Path) -> None:
    native = load_adapter(ADAPTERS_ROOT / "native-pi-matched.json")
    orkastrator = load_adapter(ADAPTERS_ROOT / "orkastrator-matched.json")
    orkastrator.model_role_map = {
        **orkastrator.model_role_map,
        "worker": orkastrator.model_role_map["worker"].model_copy(
            update={"thinking": "low"}
        ),
    }
    _refreeze(orkastrator)
    with pytest.raises(ReadinessError, match="identical role maps"):
        run_comparison(
            [native, orkastrator],
            discover_tasks(FIXTURES_ROOT),
            repeats=1,
            output_directory=tmp_path,
        )

    native = load_adapter(ADAPTERS_ROOT / "native-pi.json")
    native.description = "digest drift"
    with pytest.raises(ReadinessError, match="digest mismatch"):
        run_comparison(
            [native, load_adapter(ADAPTERS_ROOT / "orkastrator.json")],
            discover_tasks(FIXTURES_ROOT),
            repeats=1,
            output_directory=tmp_path,
        )

    diagnostic = load_adapter(ADAPTERS_ROOT / "fake-success.json")
    diagnostic.comparison_mode = "sol-high-diagnostic"
    _refreeze(diagnostic)
    with pytest.raises(ReadinessError, match="route every role to sol"):
        run_comparison(
            [diagnostic, diagnostic.model_copy(deep=True)],
            discover_tasks(FIXTURES_ROOT),
            repeats=1,
            output_directory=tmp_path,
        )

    sol_route = diagnostic.model_role_map["calibration"].model_copy(
        update={"model": "sol", "thinking": "high"}
    )
    diagnostic.model_role_map = {"calibration": sol_route}
    diagnostic.allowed_model_pool = ["sol"]
    _refreeze(diagnostic)
    peer = diagnostic.model_copy(deep=True)
    with pytest.raises(ReadinessError, match="comparison adapters not ready"):
        run_comparison(
            [diagnostic, peer],
            discover_tasks(FIXTURES_ROOT),
            repeats=1,
            output_directory=tmp_path,
        )

    tuned = load_adapter(ADAPTERS_ROOT / "fake-success.json")
    tuned_peer = tuned.model_copy(deep=True)
    tuned_peer.model_role_map = {
        "calibration": tuned_peer.model_role_map["calibration"].model_copy(
            update={"thinking": "high"}
        )
    }
    _refreeze(tuned_peer)
    with pytest.raises(ReadinessError, match="comparison adapters not ready"):
        run_comparison(
            [tuned, tuned_peer],
            discover_tasks(FIXTURES_ROOT),
            repeats=1,
            output_directory=tmp_path,
        )


def test_effective_timeout_is_bounded_by_frozen_wall_budget(tmp_path: Path) -> None:
    adapter = load_adapter(ADAPTERS_ROOT / "fake-timeout.json")
    adapter.budget = adapter.budget.model_copy(update={"max_wall_seconds": 1})
    _refreeze(adapter)
    result = run_trial(
        adapter,
        tasks_by_id()["clean-bugfix"],
        trial_id="wall-cap",
        output_directory=tmp_path,
        timeout_seconds=10,
        require_ready=False,
    )
    assert result.classification == "adapter_timeout"
    assert result.wall_time_seconds < 2


def test_manifest_fields_cannot_self_attest_containment_or_enable_execution(
    tmp_path: Path,
) -> None:
    raw = json.loads((ADAPTERS_ROOT / "native-pi.json").read_text())
    raw["ready"] = True
    raw["argv"] = ["native-pi-adapter"]
    with pytest.raises(ValidationError, match="live readiness is disabled"):
        AdapterManifest.model_validate(raw)
    raw["containment"] = {
        "backend": "external-verified",
        "filesystem_isolation": True,
        "evidence": "self-attested prose",
    }
    with pytest.raises(ValidationError):
        AdapterManifest.model_validate(raw)

    adapter = load_adapter(ADAPTERS_ROOT / "fake-success.json")
    adapter.ready = True
    adapter.config_digest = _configuration_digest(adapter.model_dump(mode="json"))
    with pytest.raises(ReadinessError, match="no harness-owned containment launcher"):
        run_trial(
            adapter,
            tasks_by_id()["clean-bugfix"],
            trial_id="must-not-run",
            output_directory=tmp_path,
        )
    assert not (tmp_path / "must-not-run").exists()
    for name in ("native-pi", "orkastrator"):
        manifest = load_adapter(ADAPTERS_ROOT / f"{name}.json")
        assert not manifest.ready
        assert manifest.argv == []
        assert manifest.containment.backend == "none"


def test_reset_replaces_untracked_and_modified_state(tmp_path: Path) -> None:
    task = discover_tasks(FIXTURES_ROOT)[0]
    prepared = prepare_fixture(task, tmp_path / "trial")
    original = snapshot_tree(prepared.repo)
    (prepared.repo / "untracked.txt").write_text("leak")
    first_file = next(path for path in prepared.repo.iterdir() if path.suffix == ".py")
    first_file.write_text("changed")
    reset = reset_fixture(prepared)
    assert snapshot_tree(reset.repo) == original
    assert not (reset.repo / "untracked.txt").exists()


def test_calibration_classifies_crash_chains_and_telemetry(tmp_path: Path) -> None:
    results = calibrate(tmp_path / "calibration")
    classifications = [result.classification for result in results]
    assert classifications.count("success") == 4
    assert classifications.count("agent_failure") == 21
    assert classifications.count("infrastructure_failure") == 2
    assert classifications.count("adapter_timeout") == 1
    assert classifications.count("adapter_crash") == 2
    assert classifications.count("adapter_protocol_failure") == 2

    by_adapter = {result.adapter_id: result for result in results}
    recovered = by_adapter["fake-crash-redelivery"]
    assert recovered.crash_recovery
    assert recovered.fault_injection.handshake_valid
    assert recovered.fault_injection.process_interrupted
    assert recovered.fault_injection.initial_process is not None
    assert recovered.fault_injection.initial_process.exit_code == -9
    assert recovered.fault_injection.initial_process.argv[-2:] == [
        "--delivery-phase",
        "initial",
    ]
    assert recovered.adapter_process.argv[-2:] == ["--delivery-phase", "recovery"]
    assert recovered.external_effect.redelivery_observed
    assert recovered.external_effect.effect_count == 1
    assert recovered.external_effect.commit_count == 1
    assert recovered.external_effect.action_id == recovered.fault_injection.action_id
    assert recovered.external_effect.commit_sha
    assert recovered.external_effect.release_nonce_published
    assert recovered.external_effect.ack_observed
    assert recovered.duplicate_action_ids == []
    assert not recovered.lost_committed_work

    expected_errors = {
        "fake-crash-missing-crash": "dispatch handshake",
        "fake-crash-missing-redelivery": "redelivery checkpoint",
        "fake-crash-wrong-action": "action_id changed",
        "fake-crash-whitespace-action": "dispatch handshake",
        "fake-crash-event-claims-before-effect": "claimed effect/commit/ack before",
        "fake-crash-missing-effect": "exactly one harness-owned effect",
        "fake-crash-duplicate-effect": "exactly one harness-owned effect",
        "fake-crash-wrong-effect-action": "effect action_id changed",
        "fake-crash-ack-before-effect": "ack was published before",
        "fake-crash-ack-during-commit": "ack release nonce mismatch",
        "fake-crash-lost-work": "lost committed work",
    }
    for adapter_id, message in expected_errors.items():
        result = by_adapter[adapter_id]
        assert not result.success
        assert result.classification == "agent_failure"
        assert result.crash_chain_error and message in result.crash_chain_error
    fabricated = by_adapter["fake-crash-fabricated-chain"]
    assert fabricated.classification == "adapter_protocol_failure"
    assert fabricated.external_effect.effect_count == 0
    assert by_adapter["fake-crash-duplicate-effect"].external_effect.effect_count == 2
    race = by_adapter["fake-crash-ack-during-commit"]
    assert race.external_effect.effect_count == 1
    assert race.external_effect.commit_count == 1
    assert not race.external_effect.ack_observed
    race_output = (
        tmp_path
        / "calibration"
        / "trials"
        / race.trial_id
        / "adapter-output"
    )
    assert (race_output / "premature-ack-attempted").is_file()

    recovered_output = (
        tmp_path
        / "calibration"
        / "trials"
        / recovered.trial_id
        / "adapter-output"
    )
    release = json.loads((recovered_output / "effect-observed.json").read_text())
    ack = json.loads((recovered_output / "ack-handshake.json").read_text())
    assert len(release["release_nonce"]) >= 32
    assert ack["release_nonce"] == release["release_nonce"]
    assert "release_nonce" not in json.loads(
        (recovered_output / "dispatch-handshake.json").read_text()
    )

    assert by_adapter["fake-crash-lost-work"].lost_committed_work
    whitespace = by_adapter["fake-crash-whitespace-action"]
    assert whitespace.fault_injection.action_id is None
    assert whitespace.fault_injection.error
    assert len(whitespace.fault_injection.error) <= 1000

    assert recovered.model_calls == 4
    assert recovered.input_tokens + recovered.output_tokens == 150
    assert recovered.supervisor_turns == 2
    assert recovered.reviewer_calls == 1
    assert recovered.fixer_calls == 1
    assert recovered.comparison_mode == "tuned-primary"
    assert recovered.model_role_map
    assert recovered.allowed_model_pool == ["offline-fake"]
    assert len(recovered.config_digest) == 64

    loud = by_adapter["fake-loud"]
    assert loud.adapter_process.stdout_truncated
    assert len(loud.adapter_process.stdout.encode()) <= 1024
    summary = json.loads((tmp_path / "calibration" / "summary.json").read_text())
    assert summary["comparison_mode"] == "tuned-primary"
    assert "weighted_score" not in summary


def test_only_harness_owned_faults_are_infrastructure(tmp_path: Path) -> None:
    clean = tasks_by_id()["clean-bugfix"]
    crash_task = tasks_by_id()["crash-redelivery"]
    launch = run_fake("fake-infrastructure-failure", clean, "launch", tmp_path)
    initial = run_fake(
        "fake-initial-infrastructure-failure", crash_task, "initial", tmp_path
    )
    zero = run_fake("fake-service-infra-zero", clean, "zero", tmp_path)
    nonzero = run_fake("fake-service-infra-nonzero", clean, "nonzero", tmp_path)
    false_claim = run_fake("fake-false-infra", clean, "false", tmp_path)
    assert launch.classification == "infrastructure_failure"
    assert launch.infrastructure_code == "adapter_launch_failure"
    assert initial.classification == "infrastructure_failure"
    assert initial.infrastructure_code == "initial_phase_launch_failure"
    assert zero.classification == "agent_failure"
    assert nonzero.classification == "adapter_crash"
    assert false_claim.classification == "agent_failure"
    assert zero.infrastructure_error is None
    assert nonzero.infrastructure_error is None
    assert false_claim.infrastructure_error is None


def test_verifier_launch_failure_is_host_observed_infrastructure(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    broken = FrozenTask(
        root=task.root,
        manifest=task.manifest,
        hidden=task.hidden.model_copy(update={"verifier_argv": ["/not/a/verifier"]}),
    )
    result = run_fake("fake-success", broken, "verifier-infra", tmp_path)
    assert result.classification == "infrastructure_failure"
    assert result.infrastructure_code == "verifier_failure"


def test_noncompleted_status_and_reported_budget_overages_cannot_succeed(
    tmp_path: Path,
) -> None:
    task = tasks_by_id()["clean-bugfix"]
    failed = run_fake("fake-status-failed", task, "failed-status", tmp_path)
    crashed = run_fake("fake-status-crashed", task, "crashed-status", tmp_path)
    over_token = run_fake("fake-over-token", task, "over-token", tmp_path)
    over_cost = run_fake("fake-over-cost", task, "over-cost", tmp_path)
    assert failed.classification == "agent_failure"
    assert failed.bundle_status == "failed"
    assert crashed.classification == "agent_failure"
    assert crashed.bundle_status == "crashed"
    assert over_token.classification == "agent_failure"
    assert over_token.budget_violations == ["tokens 250030 exceed cap 200000"]
    assert over_cost.classification == "agent_failure"
    assert over_cost.budget_violations == ["cost 30.0 exceeds cap 25.0"]


def test_handshake_read_is_single_open_and_strictly_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "trial_id": "trial",
                "adapter_id": "adapter",
                "task_id": "task",
                "action_id": "action-1",
            }
        )
    )

    def forbidden_stat(self: Path, *, follow_symlinks: bool = True) -> NoReturn:
        del self, follow_symlinks
        raise AssertionError("handshake reader must not stat before reading")

    monkeypatch.setattr(Path, "stat", forbidden_stat)
    parsed = _read_handshake(
        valid,
        DispatchHandshake,
        trial_id="trial",
        adapter_id="adapter",
        task_id="task",
    )
    assert parsed.action_id == "action-1"
    monkeypatch.undo()

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * 4096)
    with pytest.raises(ValueError, match="exceeds 4096-byte limit"):
        _read_handshake(
            oversized,
            DispatchHandshake,
            trial_id="trial",
            adapter_id="adapter",
            task_id="task",
        )


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.mkdir()
    (path / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))


def _event(sequence: int = 0) -> dict[str, object]:
    return {
        "schema_version": "1",
        "trial_id": "trial",
        "adapter_id": "adapter",
        "task_id": "task",
        "sequence": sequence,
        "event": "action",
        "action_id": "action-1",
        "detail": "",
    }


@pytest.mark.parametrize("mutation", ["missing-identity", "extra-identity", "wrong-identity"])
def test_telemetry_rejects_missing_extra_or_mismatched_identity(
    mutation: str, tmp_path: Path
) -> None:
    item = _event()
    if mutation == "missing-identity":
        del item["trial_id"]
    elif mutation == "extra-identity":
        item["run_id"] = "extra"
    else:
        item["adapter_id"] = "other"
    _write_events(tmp_path / mutation, [item])
    with pytest.raises(BundleReadError):
        read_telemetry(
            tmp_path / mutation,
            trial_id="trial",
            adapter_id="adapter",
            task_id="task",
        )


@pytest.mark.parametrize("sequences", [[0, 0], [0, 2], [1]])
def test_telemetry_rejects_duplicate_missing_or_nonzero_start_sequences(
    sequences: list[int], tmp_path: Path
) -> None:
    events = [_event(sequence) for sequence in sequences]
    _write_events(tmp_path / ("-".join(map(str, sequences))), events)
    with pytest.raises(BundleReadError, match="contiguous"):
        read_telemetry(
            tmp_path / ("-".join(map(str, sequences))),
            trial_id="trial",
            adapter_id="adapter",
            task_id="task",
        )


@pytest.mark.parametrize(
    "kind",
    ["dispatch", "action", "crash", "redelivery", "commit", "ack", "lost_committed_work"],
)
def test_action_bearing_telemetry_requires_nonempty_action_id(kind: str) -> None:
    item = _event()
    item["event"] = kind
    item["action_id"] = ""
    with pytest.raises(ValidationError, match="nonempty action_id"):
        TelemetryEvent.model_validate(item)


def test_final_state_and_protected_paths_override_adapter_claims(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    success = run_fake("fake-success", task, "success", tmp_path)
    wrong = run_fake("fake-wrong", task, "wrong", tmp_path)
    escaped = run_fake("fake-scope-escape", task, "escaped", tmp_path)
    assert success.success
    assert success.verifier.behavior_passed
    assert success.verifier.exact_tree_passed
    assert wrong.classification == "agent_failure"
    assert not wrong.verifier.behavior_passed
    assert not wrong.verifier.exact_tree_passed
    assert escaped.classification == "agent_failure"
    assert escaped.scope_violations == ["test_text_utils.py"]
    assert "test_text_utils.py" in escaped.verifier.hash_mismatches


def test_timeout_kills_process_group_and_bounds_output(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    result = run_trial(
        load_adapter(ADAPTERS_ROOT / "fake-timeout.json"),
        task,
        trial_id="timeout",
        output_directory=tmp_path,
        timeout_seconds=0.2,
        output_limit_bytes=64,
        require_ready=False,
    )
    assert result.classification == "adapter_timeout"
    assert "timeout fake started" in result.adapter_process.stdout
    pid = int((tmp_path / "timeout" / "adapter-output" / "child.pid").read_text())
    for _ in range(50):
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.02)
    else:
        pytest.fail("adapter child process remained alive after process-group timeout")


def test_report_is_deterministic_and_includes_mode_and_deltas(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    rows = [
        run_fake("fake-success", task, "fake-success", tmp_path / "trials"),
        run_fake("fake-loud", task, "fake-loud", tmp_path / "trials"),
    ]
    report = build_report(rows)
    assert report.comparison_mode == "tuned-primary"
    assert len(report.aggregates) == 2
    assert len(report.deltas) == 1
    assert report.deltas[0].comparison_mode == "tuned-primary"
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_report(report, first)
    write_report(report, second)
    assert (first / "summary.json").read_bytes() == (second / "summary.json").read_bytes()
    assert (first / "summary.md").read_bytes() == (second / "summary.md").read_bytes()


def test_readiness_refusal_happens_before_command_or_fixture(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    raw = json.loads((ADAPTERS_ROOT / "native-pi.json").read_text())
    raw["id"] = "unready"
    raw["argv"] = ["python", "-c", f"open({str(marker)!r}, 'w').close()"]
    raw["config_digest"] = "a" * 64
    adapter = AdapterManifest.model_validate(raw)
    task = discover_tasks(FIXTURES_ROOT)[0]
    with pytest.raises(ReadinessError):
        run_trial(adapter, task, trial_id="blocked", output_directory=tmp_path)
    assert not marker.exists()
    assert not (tmp_path / "blocked").exists()


def test_comparison_refuses_all_work_if_either_adapter_is_unready(tmp_path: Path) -> None:
    fake = load_adapter(ADAPTERS_ROOT / "fake-success.json")
    native = load_adapter(ADAPTERS_ROOT / "native-pi.json")
    with pytest.raises(ReadinessError):
        run_comparison(
            [fake, native],
            discover_tasks(FIXTURES_ROOT),
            repeats=1,
            output_directory=tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


def test_offline_environment_does_not_inherit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from evals.delivery_comparison.process import offline_environment

    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    environment = offline_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["PIP_NO_INDEX"] == "1"
