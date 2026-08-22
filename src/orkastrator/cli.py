"""Command-line interface for proposal acceptance and graph monitoring."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, Never

import typer
import yaml
from pydantic import BaseModel, ValidationError

from orkastrator import __version__
from orkastrator.config import ConfigError, Settings
from orkastrator.execution import ExecutionController
from orkastrator.git import GitError
from orkastrator.models import (
    FindingPhase,
    FindingReason,
    SupervisorPlan,
    workflow_contract_schemas,
)
from orkastrator.orca import OrcaClient, OrcaError, SubprocessRunner
from orkastrator.publication import GitHubPublisher, PublicationError
from orkastrator.store import StateStore, UnsupportedStateError

app = typer.Typer(
    help="Record, accept, and monitor Orca execution graphs.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    """Run the orkastrator CLI."""


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Check YAML configuration, SQLite, and Orca reachability."""

    async def check() -> dict[str, object]:
        settings, store, orca = _components()
        status = await orca.status()
        return {
            "ok": True,
            "config_path": str(settings.config_path),
            "roles": settings.graph.roles.model_dump(mode="json"),
            "max_parallel_lanes": settings.graph.max_parallel_lanes,
            "max_parallel_workers": settings.graph.max_parallel_workers,
            "database_path": str(settings.database_path),
            "database": store.counts(),
            "orca_command": list(settings.orca_command),
            "github_command": list(settings.github_command),
            "orca_reachable": status.get("ok") is True,
        }

    _run(check(), json_output=json_output)


@app.command()
def snapshot(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Read current Orca worktree state without mutation."""

    async def read() -> BaseModel:
        _, _, orca = _components()
        return await orca.snapshot()

    _run(read(), json_output=json_output)


@app.command()
def schemas(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Emit every agent-facing workflow result schema."""

    _emit(workflow_contract_schemas(), json_output=json_output)


@app.command()
def propose(
    file: Annotated[Path, typer.Option("--file", "-f", help="Graph proposal YAML file.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Validate and record a proposal without creating Orca state."""

    try:
        proposal = _load_proposal(file)
        receipt = _controller().propose(proposal)
    except (ConfigError, OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(receipt, json_output=json_output)


@app.command()
def accept(
    run_id: Annotated[str, typer.Argument(help="Recorded proposal run ID.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Accept a proposal, create its Orca Task DAGs, and start the first wave."""

    _run(_controller().accept(run_id), json_output=json_output)


@app.command()
def monitor(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Continue until the graph reaches a terminal state."),
    ] = False,
    interval: Annotated[
        float,
        typer.Option("--interval", min=0.25, help="Seconds between Orca reconciliations."),
    ] = 5.0,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Advance ready stages and reconcile the graph against Orca Tasks."""

    async def advance() -> BaseModel:
        controller = _controller()
        while True:
            result = await controller.monitor(run_id)
            # An unanswered question means an agent is parked waiting on a decision
            # the graph cannot make for it, so watching past it just burns ticks.
            if not watch or result.questions or result.status in {"complete", "failed", "blocked"}:
                return result
            await asyncio.sleep(interval)

    _run(advance(), json_output=json_output)


@app.command()
def reopen(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    finding: Annotated[str, typer.Option("--finding", help="Finding ID to send back.")],
    phase: Annotated[
        FindingPhase, typer.Option("--phase", help="Phase to reopen the finding into.")
    ] = FindingPhase.PENDING_ESCALATION,
    round: Annotated[
        int | None,
        typer.Option("--round", min=1, help="Round to reopen at. Defaults to the current one."),
    ] = None,
    reason: Annotated[
        FindingReason | None,
        typer.Option("--reason", help="Escalation trigger to adjudicate again."),
    ] = None,
    note: Annotated[
        str, typer.Option("--note", help="Why this finding is being reopened.")
    ] = "reopened by the supervisor",
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Send a settled finding back to an earlier phase and clear what superseded it."""

    try:
        _, store, _ = _components()
        record = store.reopen_finding(
            run_id,
            finding,
            phase=phase,
            round=round,
            escalation_reason=reason,
            note=note,
        )
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(record.model_dump(mode="json"), json_output=json_output)


@app.command()
def settle(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    finding: Annotated[str, typer.Option("--finding", help="Finding ID to settle.")],
    phase: Annotated[
        FindingPhase, typer.Option("--phase", help="Owner decision to record.")
    ] = FindingPhase.DEFERRED,
    note: Annotated[
        str, typer.Option("--note", help="Why this finding is being settled.")
    ] = "settled by the supervisor",
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Record an owner decision on a finding no further agent round can settle."""

    try:
        _, store, _ = _components()
        record = store.settle_finding(run_id, finding, phase=phase, note=note)
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(record.model_dump(mode="json"), json_output=json_output)


@app.command(name="show")
def show_graph(
    run_id: Annotated[str, typer.Argument(help="Local graph run ID.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Show local proposal and graph correlations without touching Orca."""

    try:
        _, store, _ = _components()
        payload = {
            "run": store.run(run_id).model_dump(mode="json"),
            "lanes": [lane.model_dump(mode="json") for lane in store.lanes(run_id)],
            "stages": [stage.model_dump(mode="json") for stage in store.stages(run_id)],
            "findings": [finding.model_dump(mode="json") for finding in store.findings(run_id)],
            "integrations": [
                receipt.model_dump(mode="json") for receipt in store.integrations(run_id)
            ],
            "publications": [
                receipt.model_dump(mode="json") for receipt in store.publications(run_id)
            ],
            "ci": [receipt.model_dump(mode="json") for receipt in store.ci_receipts(run_id)],
            "ci_failures": [
                finding.model_dump(mode="json") for finding in store.ci_failures(run_id)
            ],
        }
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(payload, json_output=json_output)


def _load_proposal(path: Path) -> SupervisorPlan:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Graph proposal must be a YAML mapping: {path}")
    return SupervisorPlan.model_validate(raw)


def _components() -> tuple[Settings, StateStore, OrcaClient]:
    settings = Settings.from_env()
    store = StateStore(settings.database_path)
    store.setup()
    runner = SubprocessRunner(
        command=settings.orca_command,
        cwd=Path.cwd(),
        timeout_seconds=settings.command_timeout_seconds,
    )
    return settings, store, OrcaClient(runner)


def _controller() -> ExecutionController:
    settings, store, orca = _components()
    return ExecutionController(
        config=settings.graph,
        orca=orca,
        store=store,
        publisher=GitHubPublisher(gh_command=settings.github_command),
    )


def _run[T](awaitable: Coroutine[Any, Any, T], *, json_output: bool) -> None:
    try:
        result = asyncio.run(awaitable)
    except (
        ConfigError,
        GitError,
        OrcaError,
        PublicationError,
        UnsupportedStateError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(result, json_output=json_output)


def _emit(value: object, *, json_output: bool) -> None:
    if isinstance(value, BaseModel):
        payload: object = value.model_dump(mode="json", by_alias=True)
    elif isinstance(value, list) and all(isinstance(item, BaseModel) for item in value):
        payload = [item.model_dump(mode="json", by_alias=True) for item in value]
    else:
        payload = value
    typer.echo(json.dumps(payload, indent=2, sort_keys=json_output, default=str))


def _fail(message: str, *, json_output: bool) -> Never:
    if json_output:
        typer.echo(json.dumps({"ok": False, "error": message}), err=True)
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)
