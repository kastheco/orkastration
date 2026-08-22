"""CLI contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
import yaml
from pydantic import BaseModel
from typer.testing import CliRunner

from orkastrator import __version__, cli
from orkastrator.config import GraphConfig
from orkastrator.locks import run_lock
from orkastrator.models import (
    AcceptanceAuthorization,
    ConfigChange,
    OrcaSnapshot,
    PolicyReauthorization,
    ProposalReceipt,
    SupervisorPlan,
)
from orkastrator.orca import OrcaError
from orkastrator.store import StateStore
from tests.factories import graph_config_data

runner = CliRunner()


def graph_config() -> GraphConfig:
    return GraphConfig.model_validate(graph_config_data())


def proposal() -> SupervisorPlan:
    return SupervisorPlan.model_validate(
        {
            "objective": "coordinate this",
            "rationale": "Ready.",
            "next_action": "propose_lanes",
            "owner_question": None,
            "lanes": [
                {
                    "name": "issue-123",
                    "issue_id": "ISSUE-123",
                    "repo_selector": "id:repo",
                    "base_ref": "main",
                    "dependencies": [],
                    "prompt": "Implement it.",
                    "stop_condition": "Tests pass.",
                }
            ],
        }
    )


class FakeStore:
    def counts(self) -> dict[str, int]:
        return {"runs": 1, "lanes": 2, "stages": 8}


class FakeOrca:
    async def status(self) -> dict[str, object]:
        return {"ok": True}

    async def snapshot(self) -> OrcaSnapshot:
        return OrcaSnapshot(worktrees=[])


class FakeController:
    parked_question: ClassVar[list[str]] = []
    monitor_status: ClassVar[str] = "complete"

    def propose(self, value: SupervisorPlan) -> ProposalReceipt:
        return ProposalReceipt(run_id="run-1", proposal=value)

    async def accept(self, run_id: str) -> StatusResult:
        return StatusResult(run_id=run_id, status="active")

    answered: ClassVar[list[tuple[str, str, str]]] = []
    pending: ClassVar[list[object]] = []

    async def questions(self, run_id: str) -> list[object]:
        return list(type(self).pending)

    async def answer(self, run_id: str, message_id: str, body: str) -> object:
        type(self).answered.append((run_id, message_id, body))
        return SimpleNamespace(lane="issue-123", role="worker")

    async def monitor(self, run_id: str) -> StatusResult:
        return StatusResult(
            run_id=run_id,
            status=type(self).monitor_status,
            questions=list(type(self).parked_question),
        )


class FakeReauthorizingController(FakeController):
    calls: ClassVar[list[tuple[str, str, bool]]] = []

    def reauthorize(self, run_id: str, note: str, *, apply: bool = True) -> object:
        type(self).calls.append((run_id, note, apply))
        return PolicyReauthorization(
            authorization=AcceptanceAuthorization(
                run_id=run_id, proposal_sha256="a" * 64, config_sha256="b" * 64
            ),
            changes=[
                ConfigChange(
                    path="final_gate.advisory_checks", before="[]", after='["conformance"]'
                )
            ],
            applied=apply,
        )


class StatusResult(BaseModel):
    run_id: str
    status: str
    questions: list[str] = []


@pytest.fixture
def fake_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = SimpleNamespace(
        config_path=tmp_path / "orkastrator.yaml",
        graph=graph_config(),
        database_path=tmp_path / "state.sqlite3",
        orca_command=("orca-ide",),
        github_command=("gh",),
    )
    monkeypatch.setattr(cli, "_components", lambda: (settings, FakeStore(), FakeOrca()))
    monkeypatch.setattr(cli, "_controller", FakeController)


def test_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_doctor_and_snapshot_emit_json(fake_wiring: None) -> None:
    doctor = runner.invoke(cli.app, ["doctor", "--json"])
    snapshot = runner.invoke(cli.app, ["snapshot", "--json"])
    assert doctor.exit_code == 0
    assert json.loads(doctor.stdout)["roles"]["worker"]["model"] == "gpt-test"
    assert json.loads(snapshot.stdout)["worktrees"] == []


def test_schemas_emit_strict_workflow_contracts() -> None:
    result = runner.invoke(cli.app, ["schemas", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["initial_review_report"]["additionalProperties"] is False
    assert payload["fix_attempt"]["title"] == "FixAttempt"


def test_propose_accept_and_monitor(fake_wiring: None, tmp_path: Path) -> None:
    proposal_path = tmp_path / "proposal.yaml"
    proposal_path.write_text(
        """
objective: coordinate this
rationale: Ready.
next_action: propose_lanes
owner_question: null
lanes:
  - name: issue-123
    issue_id: ISSUE-123
    repo_selector: id:repo
    base_ref: main
    dependencies: []
    prompt: Implement it.
    stop_condition: Tests pass.
""".strip()
    )
    proposed = runner.invoke(cli.app, ["propose", "-f", str(proposal_path), "--json"])
    accepted = runner.invoke(cli.app, ["accept", "run-1", "--json"])
    monitored = runner.invoke(cli.app, ["monitor", "run-1", "--watch", "--json"])
    assert json.loads(proposed.stdout)["status"] == "proposed"
    assert json.loads(accepted.stdout)["status"] == "active"
    assert json.loads(monitored.stdout)["status"] == "complete"


def test_a_second_driver_on_one_run_is_refused_by_name(fake_wiring: None, tmp_path: Path) -> None:
    """Orca punishes two supervisors on one run with `consumer_fenced`, which
    fails the *first* one intermittently rather than the second one cleanly. So
    refuse in front of it, and say which process already has the run."""

    with run_lock(tmp_path / "state.sqlite3", "run-1"):
        refused = runner.invoke(cli.app, ["monitor", "run-1", "--json"])

    assert refused.exit_code != 0
    assert "already being driven by" in json.loads(refused.stderr)["error"]

    # And the moment it is released, the same command works.
    assert json.loads(runner.invoke(cli.app, ["monitor", "run-1", "--json"]).stdout)["status"]


def test_doctor_names_the_run_a_live_supervisor_is_driving(
    fake_wiring: None, tmp_path: Path
) -> None:
    with run_lock(tmp_path / "state.sqlite3", "run-1"):
        result = runner.invoke(cli.app, ["doctor", "--json"])

    assert [entry["run_id"] for entry in json.loads(result.stdout)["driving"]] == ["run-1"]
    assert json.loads(runner.invoke(cli.app, ["doctor", "--json"]).stdout)["driving"] == []


def test_propose_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    proposal_path = tmp_path / "proposal.yaml"
    proposal_path.write_text("[]")

    result = runner.invoke(cli.app, ["propose", "-f", str(proposal_path), "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["ok"] is False


def test_show_reads_local_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.setup()
    run_id = store.record_proposal(proposal())
    settings = SimpleNamespace(graph=graph_config())
    monkeypatch.setattr(cli, "_components", lambda: (settings, store, FakeOrca()))
    result = runner.invoke(cli.app, ["show", run_id, "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["run"]["run_id"] == run_id


def test_runtime_error_is_clean_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenOrca(FakeOrca):
        async def snapshot(self) -> OrcaSnapshot:
            raise OrcaError("offline")

    monkeypatch.setattr(
        cli,
        "_components",
        lambda: (SimpleNamespace(), FakeStore(), BrokenOrca()),
    )
    result = runner.invoke(cli.app, ["snapshot", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stderr) == {"ok": False, "error": "offline"}


def test_controller_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "orkastrator.yaml"
    config_path.write_text(yaml.safe_dump(graph_config_data(), sort_keys=False))
    monkeypatch.setenv("ORKASTRATOR_CONFIG", str(config_path))
    monkeypatch.setenv("ORKASTRATOR_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-ide")
    assert cli._controller() is not None


def test_watch_stops_on_a_parked_question(fake_wiring: None) -> None:
    FakeController.monitor_status = "active"
    FakeController.parked_question = ["msg-1"]
    try:
        result = runner.invoke(cli.app, ["monitor", "run-1", "--watch", "--json"])
    finally:
        FakeController.monitor_status = "complete"
        FakeController.parked_question = []

    assert result.exit_code == 0
    assert json.loads(result.stdout)["questions"] == ["msg-1"]


def test_answer_reads_the_body_from_a_file(fake_wiring: None, tmp_path: Path) -> None:
    """A five-part direction is not something anyone types onto a command line."""

    FakeController.answered.clear()
    body = tmp_path / "answer.txt"
    body.write_text("restore the exclusion\nand fix the guard\n")

    result = runner.invoke(
        cli.app, ["answer", "run-1", "--message", "msg-1", "--body-file", str(body)]
    )

    assert result.exit_code == 0, result.output
    assert FakeController.answered == [
        ("run-1", "msg-1", "restore the exclusion\nand fix the guard\n")
    ]


def test_answer_refuses_both_a_body_and_a_body_file(fake_wiring: None, tmp_path: Path) -> None:
    """Silently preferring one would send a direction nobody chose."""

    FakeController.answered.clear()
    body = tmp_path / "answer.txt"
    body.write_text("from the file")

    result = runner.invoke(
        cli.app,
        ["answer", "run-1", "--message", "msg-1", "--body", "inline", "--body-file", str(body)],
    )

    assert result.exit_code != 0
    assert "exactly one" in result.output
    assert FakeController.answered == []


def test_answer_refuses_an_empty_body(fake_wiring: None) -> None:
    FakeController.answered.clear()

    result = runner.invoke(cli.app, ["answer", "run-1", "--message", "msg-1", "--body", "   "])

    assert result.exit_code != 0
    assert FakeController.answered == []


def test_questions_says_so_when_there_are_none(fake_wiring: None) -> None:
    FakeController.pending = []

    result = runner.invoke(cli.app, ["questions", "run-1"])

    assert result.exit_code == 0, result.output
    assert "no unanswered questions" in result.output


def test_reauthorize_shows_the_change_before_it_will_make_it(
    monkeypatch: pytest.MonkeyPatch, fake_wiring: None
) -> None:
    """A digest pair is not something an owner can judge, so nothing applies until they see one.

    `--note` used to be the only argument, which made the operator's typed claim
    about the change stand in for a reading of it. The default is now a preview.
    """

    monkeypatch.setattr(cli, "_controller", FakeReauthorizingController)
    FakeReauthorizingController.calls.clear()

    preview = runner.invoke(cli.app, ["reauthorize", "run-1"])

    assert preview.exit_code == 0
    assert 'final_gate.advisory_checks: [] -> ["conformance"]' in preview.stdout
    assert "--confirm" in preview.stdout
    assert FakeReauthorizingController.calls == [("run-1", "", False)]

    applied = runner.invoke(
        cli.app, ["reauthorize", "run-1", "--confirm", "--note", "advisory suite is flaking"]
    )

    assert applied.exit_code == 0
    assert FakeReauthorizingController.calls[-1] == ("run-1", "advisory suite is flaking", True)


def test_confirming_a_policy_change_without_a_reason_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_wiring: None
) -> None:
    monkeypatch.setattr(cli, "_controller", FakeReauthorizingController)
    FakeReauthorizingController.calls.clear()

    result = runner.invoke(cli.app, ["reauthorize", "run-1", "--confirm"])

    assert result.exit_code == 1
    assert "--confirm needs --note" in result.stderr
    assert FakeReauthorizingController.calls == []
