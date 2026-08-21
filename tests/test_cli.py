"""CLI contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from kasgraph import __version__, cli
from kasgraph.models import CycleResult, OrcaSnapshot, SupervisorPlan
from kasgraph.orca import OrcaError

runner = CliRunner()


def wait_plan() -> SupervisorPlan:
    return SupervisorPlan(rationale="No lane is ready.", next_action="wait")


class FakeStore:
    def counts(self) -> dict[str, int]:
        return {"runs": 1, "lanes": 2}


class FakeOrca:
    async def status(self) -> dict[str, object]:
        return {"ok": True}

    async def snapshot(self) -> OrcaSnapshot:
        return OrcaSnapshot(worktrees=[])


class FakeSupervisor:
    async def plan(self, objective: str) -> tuple[OrcaSnapshot, SupervisorPlan]:
        assert objective == "coordinate this"
        return OrcaSnapshot(worktrees=[]), wait_plan()

    async def run_cycle(self, objective: str, *, execute: bool) -> CycleResult:
        assert objective == "coordinate this"
        return CycleResult(
            run_id="run-1",
            executed=execute,
            action="complete" if execute else "wait",
            plan=wait_plan(),
        )

    async def reconcile(self) -> list[object]:
        return []


@pytest.fixture
def fake_components(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = SimpleNamespace(
        model="test",
        database_path=tmp_path / "state.sqlite3",
        orca_command=("orca-ide",),
    )
    monkeypatch.setattr(
        cli, "_components", lambda *, require_model: (settings, FakeStore(), FakeOrca())
    )
    monkeypatch.setattr(cli, "_supervisor", FakeSupervisor)


def test_version() -> None:
    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_doctor_emits_json(fake_components: None) -> None:
    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["orca_reachable"] is True


def test_snapshot_emits_model(fake_components: None) -> None:
    result = runner.invoke(cli.app, ["snapshot", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["worktrees"] == []


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["plan", "-o", "coordinate this", "--json"], "wait"),
        (["run", "-o", "coordinate this", "--json"], "wait"),
        (["run", "-o", "coordinate this", "--execute", "--json"], "complete"),
    ],
)
def test_supervisor_commands_emit_results(
    fake_components: None, arguments: list[str], expected: str
) -> None:
    result = runner.invoke(cli.app, arguments)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload.get("next_action", payload.get("action")) == expected


def test_reconcile_emits_list(fake_components: None) -> None:
    result = runner.invoke(cli.app, ["reconcile", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_runtime_error_is_clean_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenOrca(FakeOrca):
        async def snapshot(self) -> OrcaSnapshot:
            raise OrcaError("offline")

    settings = SimpleNamespace(
        model=None,
        database_path=Path("/tmp/unused.sqlite3"),
        orca_command=("orca-ide",),
    )
    monkeypatch.setattr(
        cli,
        "_components",
        lambda *, require_model: (settings, FakeStore(), BrokenOrca()),
    )

    result = runner.invoke(cli.app, ["snapshot", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {"ok": False, "error": "offline"}


def test_components_and_supervisor_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KASGRAPH_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("KASGRAPH_MODEL", "test")
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-ide")

    settings, store, orca = cli._components(require_model=True)
    supervisor = cli._supervisor()

    assert settings.model == "test"
    assert store.path.exists()
    assert orca is not None
    assert supervisor is not None


def test_plan_rejects_non_model_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class BadSupervisor:
        async def plan(self, objective: str) -> tuple[OrcaSnapshot, Any]:
            return OrcaSnapshot(worktrees=[]), {"invalid": True}

    monkeypatch.setattr(cli, "_supervisor", cast(Any, BadSupervisor))

    result = runner.invoke(cli.app, ["plan", "-o", "coordinate this"])

    assert result.exit_code == 1
    assert "planner returned an invalid plan" in result.stderr
