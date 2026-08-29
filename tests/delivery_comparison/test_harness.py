from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

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
from evals.delivery_comparison.models import AdapterManifest
from evals.delivery_comparison.report import build_report, write_report
from evals.delivery_comparison.runner import ReadinessError, load_adapter, run_comparison, run_trial


def tasks_by_id() -> dict[str, FrozenTask]:
    return {task.manifest.id: task for task in discover_tasks(FIXTURES_ROOT)}


def test_validate_is_no_run_and_live_adapters_are_unready() -> None:
    evidence = validate_contracts()
    assert evidence["commands_executed"] == 0
    assert evidence["live_run_ready"] is False
    assert evidence["live_readiness"] == {"native-pi": False, "orkastrator": False}


def test_public_manifest_does_not_copy_hidden_truth(tmp_path: Path) -> None:
    task = discover_tasks(FIXTURES_ROOT)[0]
    prepared = prepare_fixture(task, tmp_path / "trial")
    public = prepared.public_manifest.read_text()
    assert "expected_files_sha256" not in public
    assert "verifier.py" not in public
    assert not (prepared.trial_root / "hidden_truth").exists()


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


def test_calibration_classifies_fakes_and_aggregates_telemetry(tmp_path: Path) -> None:
    results = calibrate(tmp_path / "calibration")
    classifications = [result.classification for result in results]
    assert classifications.count("success") == 4
    assert classifications.count("agent_failure") == 4
    assert "adapter_timeout" in classifications
    assert "adapter_crash" in classifications
    assert "adapter_protocol_failure" in classifications
    assert "infrastructure_failure" in classifications

    crash_recovery = next(result for result in results if result.task_id == "crash-redelivery")
    assert crash_recovery.crash_recovery
    assert crash_recovery.duplicate_action_ids == []
    assert not crash_recovery.lost_committed_work
    assert crash_recovery.model_calls == 4
    assert crash_recovery.input_tokens + crash_recovery.output_tokens == 150
    assert crash_recovery.supervisor_turns == 2
    assert crash_recovery.reviewer_calls == 1
    assert crash_recovery.fixer_calls == 1

    duplicate = next(result for result in results if result.adapter_id == "fake-duplicate")
    assert duplicate.duplicate_action_ids == ["action-1"]
    assert duplicate.classification == "agent_failure"

    lost = next(result for result in results if result.adapter_id == "fake-lost-work")
    assert lost.lost_committed_work
    assert lost.classification == "agent_failure"

    loud = next(result for result in results if result.adapter_id == "fake-loud")
    assert loud.adapter_process.stdout_truncated
    assert len(loud.adapter_process.stdout.encode()) <= 1024

    summary = json.loads((tmp_path / "calibration" / "summary.json").read_text())
    assert "weighted_score" not in summary
    assert summary["trials"]


def test_final_state_and_protected_paths_override_adapter_claims(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    success = run_trial(
        load_adapter(ADAPTERS_ROOT / "fake-success.json"),
        task,
        trial_id="success",
        output_directory=tmp_path,
    )
    assert success.success
    assert success.verifier.behavior_passed
    assert success.verifier.exact_tree_passed

    wrong = run_trial(
        load_adapter(ADAPTERS_ROOT / "fake-wrong.json"),
        task,
        trial_id="wrong",
        output_directory=tmp_path,
    )
    assert wrong.classification == "agent_failure"
    assert not wrong.verifier.behavior_passed
    assert not wrong.verifier.exact_tree_passed

    escaped = run_trial(
        load_adapter(ADAPTERS_ROOT / "fake-scope-escape.json"),
        task,
        trial_id="escaped",
        output_directory=tmp_path,
    )
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


def test_infrastructure_failure_is_not_agent_failure(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    result = run_trial(
        load_adapter(ADAPTERS_ROOT / "fake-infrastructure-failure.json"),
        task,
        trial_id="infra",
        output_directory=tmp_path,
    )
    assert result.classification == "infrastructure_failure"
    assert result.infrastructure_error
    assert not result.success


def test_report_is_deterministic_and_has_medians_and_deltas(tmp_path: Path) -> None:
    task = tasks_by_id()["clean-bugfix"]
    rows = []
    for adapter_name in ("fake-success", "fake-loud"):
        rows.append(
            run_trial(
                load_adapter(ADAPTERS_ROOT / f"{adapter_name}.json"),
                task,
                trial_id=adapter_name,
                output_directory=tmp_path / "trials",
            )
        )
    report = build_report(rows)
    assert len(report.aggregates) == 2
    assert len(report.deltas) == 1
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_report(report, first)
    write_report(report, second)
    assert (first / "summary.json").read_bytes() == (second / "summary.json").read_bytes()
    assert (first / "summary.md").read_bytes() == (second / "summary.md").read_bytes()


def test_readiness_refusal_happens_before_command_or_fixture(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    adapter = AdapterManifest(
        id="unready",
        description="must not execute",
        ready=False,
        argv=["python", "-c", f"open({str(marker)!r}, 'w').close()"],
        workflow_requirement="test",
    )
    task = discover_tasks(FIXTURES_ROOT)[0]
    with pytest.raises(ReadinessError):
        run_trial(adapter, task, trial_id="blocked", output_directory=tmp_path)
    assert not marker.exists()
    assert not (tmp_path / "blocked").exists()


def test_comparison_refuses_all_work_if_either_adapter_is_unready(tmp_path: Path) -> None:
    ready = load_adapter(ADAPTERS_ROOT / "fake-success.json")
    unready = load_adapter(ADAPTERS_ROOT / "native-pi.json")
    with pytest.raises(ReadinessError):
        run_comparison(
            [ready, unready],
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
