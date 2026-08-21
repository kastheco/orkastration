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

from kasgraph import __version__
from kasgraph.config import ConfigError, Settings
from kasgraph.execution import ExecutionController
from kasgraph.models import SupervisorPlan
from kasgraph.orca import OrcaClient, OrcaError, SubprocessRunner
from kasgraph.planner import CodexCliPlanner, PlannerError, SubprocessCodexRunner
from kasgraph.store import StateStore

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
    """Run the Kasgraph CLI."""


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
            "supervisor": settings.graph.supervisor.model_dump(mode="json"),
            "max_parallel_lanes": settings.graph.max_parallel_lanes,
            "database_path": str(settings.database_path),
            "database": store.counts(),
            "orca_command": list(settings.orca_command),
            "codex_command": list(settings.codex_command),
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
def plan(
    objective: Annotated[str, typer.Option("--objective", "-o", help="Work to coordinate.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Use authenticated Codex to generate and record a typed proposal."""

    async def create() -> BaseModel:
        proposal = await _planner().plan(objective)
        return _controller().propose(proposal)

    _run(create(), json_output=json_output)


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
            if not watch or result.status in {"complete", "failed", "blocked"}:
                return result
            await asyncio.sleep(interval)

    _run(advance(), json_output=json_output)


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
    return ExecutionController(config=settings.graph, orca=orca, store=store)


def _planner() -> CodexCliPlanner:
    settings = Settings.from_env()
    runner = SubprocessCodexRunner(
        command=settings.codex_command,
        cwd=Path.cwd(),
        profile=settings.graph.supervisor,
        timeout_seconds=settings.planner_timeout_seconds,
    )
    return CodexCliPlanner(runner)


def _run[T](awaitable: Coroutine[Any, Any, T], *, json_output: bool) -> None:
    try:
        result = asyncio.run(awaitable)
    except (ConfigError, OrcaError, PlannerError, KeyError, TypeError, ValueError) as exc:
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
