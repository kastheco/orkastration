"""CLI contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from kasgraph import __version__, cli
from kasgraph.config import GraphConfig
from kasgraph.models import OrcaSnapshot, ProposalReceipt, SupervisorPlan
from kasgraph.orca import OrcaError
from kasgraph.store import StateStore

runner = CliRunner()


def graph_config() -> GraphConfig:
    profile = {"agent": "codex", "model": "gpt-test", "strength": "high"}
    return GraphConfig.model_validate(
        {
            "version": 1,
            "max_parallel_lanes": 2,
            "supervisor": profile,
            "roles": {
                "worker": profile,
                "initial_reviewer": profile,
                "fixer": profile,
                "re_reviewer": profile,
            },
        }
    )


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


class FakePlanner:
    async def plan(self, objective: str) -> SupervisorPlan:
        assert objective == "coordinate this"
        return proposal()


class FakeController:
    def propose(self, value: SupervisorPlan) -> ProposalReceipt:
        return ProposalReceipt(run_id="run-1", proposal=value)

    async def accept(self, run_id: str) -> StatusResult:
        return StatusResult(run_id=run_id, status="active")

    async def monitor(self, run_id: str) -> StatusResult:
        return StatusResult(run_id=run_id, status="complete")


class StatusResult(BaseModel):
    run_id: str
    status: str


@pytest.fixture
def fake_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = SimpleNamespace(
        config_path=tmp_path / "kasgraph.yaml",
        graph=graph_config(),
        database_path=tmp_path / "state.sqlite3",
        orca_command=("orca-ide",),
        codex_command=("codex",),
    )
    monkeypatch.setattr(cli, "_components", lambda: (settings, FakeStore(), FakeOrca()))
    monkeypatch.setattr(cli, "_controller", FakeController)
    monkeypatch.setattr(cli, "_planner", FakePlanner)


def test_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_doctor_and_snapshot_emit_json(fake_wiring: None) -> None:
    doctor = runner.invoke(cli.app, ["doctor", "--json"])
    snapshot = runner.invoke(cli.app, ["snapshot", "--json"])
    assert doctor.exit_code == 0
    assert json.loads(doctor.stdout)["supervisor"]["model"] == "gpt-test"
    assert json.loads(snapshot.stdout)["worktrees"] == []


def test_plan_records_codex_result(fake_wiring: None) -> None:
    result = runner.invoke(cli.app, ["plan", "--objective", "coordinate this", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "run-1"


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


def test_controller_and_planner_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "kasgraph.yaml"
    config_path.write_text(
        """
version: 1
max_parallel_lanes: 2
supervisor: {agent: codex, model: gpt-test, strength: high}
roles:
  worker: {agent: codex, model: gpt-test, strength: high}
  initial_reviewer: {agent: codex, model: gpt-test, strength: high}
  fixer: {agent: codex, model: gpt-test, strength: high}
  re_reviewer: {agent: codex, model: gpt-test, strength: high}
""".strip()
    )
    monkeypatch.setenv("KASGRAPH_CONFIG", str(config_path))
    monkeypatch.setenv("KASGRAPH_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-ide")
    assert cli._controller() is not None
    assert cli._planner() is not None


def test_plan_rejects_non_model_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class BadPlanner:
        async def plan(self, objective: str) -> Any:
            return {"invalid": True}

    monkeypatch.setattr(cli, "_planner", cast(Any, BadPlanner))
    monkeypatch.setattr(cli, "_controller", FakeController)
    result = runner.invoke(cli.app, ["plan", "-o", "coordinate this"])
    assert result.exit_code == 1
