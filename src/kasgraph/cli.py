"""Command-line interface for one-cycle supervision."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, Never

import typer
from pydantic import BaseModel

from kasgraph import __version__
from kasgraph.config import ConfigError, Settings
from kasgraph.orca import OrcaClient, OrcaError, SubprocessRunner
from kasgraph.planner import PlannerDeps, PlanRejected, PydanticPlanner
from kasgraph.store import StateStore
from kasgraph.supervisor import Supervisor

app = typer.Typer(
    help="Plan and reconcile one safe action at a time through Orca.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[  # pyright: ignore[reportUnusedParameter]
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    """Run the Kasgraph CLI."""


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Check local configuration, SQLite, and Orca reachability."""

    async def check() -> dict[str, object]:
        settings, store, orca = _components(require_model=False)
        status = await orca.status()
        return {
            "ok": True,
            "model_configured": settings.model is not None,
            "database_path": str(settings.database_path),
            "database": store.counts(),
            "orca_command": list(settings.orca_command),
            "orca_reachable": status.get("ok") is True,
        }

    _run(check(), json_output=json_output)


@app.command()
def snapshot(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Read the current Orca worktree snapshot without mutation."""

    async def read() -> BaseModel:
        _, _, orca = _components(require_model=False)
        return await orca.snapshot()

    _run(read(), json_output=json_output)


@app.command()
def plan(
    objective: Annotated[str, typer.Option("--objective", "-o", help="Work to coordinate.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Generate and validate a typed plan without persisting or executing it."""

    async def create() -> BaseModel:
        supervisor = _supervisor()
        _, proposal = await supervisor.plan(objective)
        if not isinstance(proposal, BaseModel):
            raise TypeError("planner returned an invalid plan")
        return proposal

    _run(create(), json_output=json_output)


@app.command(name="run")
def run_cycle(
    objective: Annotated[str, typer.Option("--objective", "-o", help="Work to coordinate.")],
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Create the selected Orca lane. Default is dry-run."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Record one plan and optionally perform its one selected Orca mutation."""

    _run(_supervisor().run_cycle(objective, execute=execute), json_output=json_output)


@app.command()
def reconcile(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Reconcile persisted lanes against current Orca state."""

    _run(_supervisor().reconcile(), json_output=json_output)


def _components(*, require_model: bool) -> tuple[Settings, StateStore, OrcaClient]:
    settings = Settings.from_env()
    if require_model:
        settings.require_model()
    store = StateStore(settings.database_path)
    store.setup()
    runner = SubprocessRunner(
        command=settings.orca_command,
        cwd=Path.cwd(),
        timeout_seconds=settings.command_timeout_seconds,
    )
    return settings, store, OrcaClient(runner)


def _supervisor() -> Supervisor:
    settings, store, orca = _components(require_model=True)
    model = settings.require_model()
    planner = PydanticPlanner(
        model,
        PlannerDeps(orca=orca, max_parallel_lanes=settings.max_parallel_lanes),
    )
    return Supervisor(planner=planner, orca=orca, store=store)


def _run[T](awaitable: Coroutine[Any, Any, T], *, json_output: bool) -> None:
    try:
        result = asyncio.run(awaitable)
    except (ConfigError, OrcaError, PlanRejected, TypeError, ValueError, KeyError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(result, json_output=json_output)


def _emit(value: object, *, json_output: bool) -> None:
    if isinstance(value, BaseModel):
        payload: object = value.model_dump(mode="json", by_alias=True)
    elif isinstance(value, list) and all(isinstance(item, BaseModel) for item in value):
        payload = [item.model_dump(mode="json", by_alias=True) for item in value]
    else:
        payload = value
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        typer.echo(json.dumps(payload, indent=2, default=str))


def _fail(message: str, *, json_output: bool) -> Never:
    if json_output:
        typer.echo(json.dumps({"ok": False, "error": message}), err=True)
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)
