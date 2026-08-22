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
from orkastrator.locks import RunLockedError, held_runs, run_lock
from orkastrator.models import (
    FindingPhase,
    FindingReason,
    SupervisorPlan,
    workflow_contract_schemas,
)
from orkastrator.orca import OrcaClient, OrcaError, SubprocessRunner
from orkastrator.publication import GitHubPublisher, PublicationError
from orkastrator.report import build_report, render
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
            # A live driver, read from the kernel rather than from a record that
            # could be stale: "is something already ticking this run" in one command.
            "driving": held_runs(settings.database_path),
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

    _drive(run_id, _controller().accept(run_id), json_output=json_output)


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

    _drive(run_id, advance(), json_output=json_output)


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


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    lane: Annotated[
        str | None,
        typer.Option("--lane", help="Lane to resume. Defaults to every blocked lane."),
    ] = None,
    note: Annotated[
        str, typer.Option("--note", help="Why this lane is being resumed.")
    ] = "resumed by the supervisor",
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Clear a lane block and let the run advance again.

    `reopen` and `settle` act on findings. A lane that blocked with every
    finding already settled - on a CI query made a second too early, on a pull
    request somebody merged - is reachable by neither, and this is that case.
    """

    try:
        resumed = _controller().resume(run_id, lane, note)
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(
        {"run_id": run_id, "resumed": [item.model_dump(mode="json") for item in resumed]},
        json_output=json_output,
    )


@app.command()
def reauthorize(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    note: Annotated[str, typer.Option("--note", help="Why this policy change is authorized.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Re-freeze a live run against a configuration the owner changed on purpose.

    Acceptance digests the proposal and the config together, so editing
    `orkastrator.yaml` mid-run fails every subsequent tick. Use this when only
    the policy moved. If the proposal itself changed, this refuses and the
    honest answer is a new proposal.
    """

    try:
        authorization = _controller().reauthorize(run_id, note)
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(authorization.model_dump(mode="json"), json_output=json_output)


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


@app.command()
def questions(
    run_id: Annotated[str, typer.Argument(help="Local graph run ID.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Show every unanswered question raised inside one run, body included."""

    try:
        controller = _controller()
        pending = asyncio.run(controller.questions(run_id))
    except (ConfigError, OrcaError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    if json_output:
        _emit([item.model_dump(mode="json") for item in pending], json_output=True)
        return
    if not pending:
        typer.echo("no unanswered questions")
        return
    for item in pending:
        typer.echo(f"{item.message_id}  {item.kind}  {item.lane or '?'}/{item.role or '?'}")
        typer.echo(f"  asked {item.asked_at}: {item.subject}")
        typer.echo(f"  {item.body}")
        typer.echo("")


@app.command()
def answer(
    run_id: Annotated[str, typer.Argument(help="Local graph run ID.")],
    message: Annotated[str, typer.Option("--message", help="Message ID from `questions`.")],
    body: Annotated[str, typer.Option("--body", help="Answer text.")] = "",
    body_file: Annotated[
        Path | None,
        typer.Option("--body-file", help="Read the answer from a file instead of --body."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Answer one blocked agent, resolving the reply handle from the run itself."""

    if (body == "") == (body_file is None):
        _fail("pass exactly one of --body or --body-file", json_output=json_output)
    try:
        text = body if body_file is None else body_file.read_text()
    except OSError as exc:
        _fail(str(exc), json_output=json_output)
    if not text.strip():
        _fail("answer body cannot be empty", json_output=json_output)
    try:
        controller = _controller()
        question = asyncio.run(controller.answer(run_id, message, text))
    except (ConfigError, OrcaError, KeyError, ValueError, OSError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(
        {"answered": message, "lane": question.lane, "role": question.role},
        json_output=json_output,
    )


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Local graph run ID.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Report what one run cost per finding, from persisted rows only.

    Read-only and offline. Run it against two runs to tell whether a change to
    the graph converged anything: `dispatches per finding` and `repeat rate`
    are the two numbers that should fall.
    """

    try:
        _, store, _ = _components()
        summary = build_report(
            run_id=run_id,
            lanes=store.lanes(run_id),
            stages=store.stages(run_id),
            findings=store.findings(run_id),
            integrations=store.integrations(run_id),
            publications=store.publications(run_id),
            events=store.events(run_id),
        )
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    if json_output:
        _emit(summary.to_dict(), json_output=True)
        return
    typer.echo(render(summary))


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
        publisher=GitHubPublisher(
            gh_command=settings.github_command,
            advisory_checks=settings.graph.final_gate.advisory_checks,
        ),
    )


def _drive[T](run_id: str, awaitable: Coroutine[Any, Any, T], *, json_output: bool) -> None:
    """Run one command that dispatches to Orca, as the run's only driver.

    Orca refuses a `worker-start` from a terminal other than the one bound to
    the Task Run, so a second supervisor on the same run does not fail cleanly:
    it makes the first one fail, intermittently, until somebody notices. Refuse
    in front of that instead, and name the process already holding the run.
    """

    try:
        settings, _, _ = _components()
        with run_lock(settings.database_path, run_id):
            _run(awaitable, json_output=json_output)
    except (ConfigError, RunLockedError) as exc:
        awaitable.close()
        _fail(str(exc), json_output=json_output)


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
