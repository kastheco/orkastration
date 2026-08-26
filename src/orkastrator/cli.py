"""Command-line interface for proposal acceptance and graph monitoring."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Never, cast

import typer
import yaml
from pydantic import BaseModel, ValidationError

from orkastrator import __version__
from orkastrator.config import ConfigError, Settings
from orkastrator.execution import ExecutionController
from orkastrator.git import GitError
from orkastrator.locks import RunLockedError, held_runs, run_lock
from orkastrator.models import (
    TERMINAL_LANE_PHASES,
    FindingPhase,
    FindingReason,
    PolicyReauthorization,
    ReviewFinding,
    StagePhase,
    SupervisorPlan,
    workflow_contract_schemas,
)
from orkastrator.orca import OrcaClient, OrcaError, SubprocessRunner
from orkastrator.publication import GitHubPublisher, PublicationError
from orkastrator.reap import ReapPlan
from orkastrator.reap import build_plan as build_reap_plan
from orkastrator.reap import render as render_reap_plan
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
            if not watch:
                return result.model_copy(update={"exit_reason": "single_tick"})
            # An unanswered question means an agent is parked waiting on a decision
            # the graph cannot make for it, so watching past it just burns ticks.
            if result.questions:
                return result.model_copy(update={"exit_reason": "unanswered_question"})
            terminal_lanes = bool(result.lanes) and all(
                lane.phase in TERMINAL_LANE_PHASES for lane in result.lanes
            )
            in_flight = any(
                stage.phase in {StagePhase.STARTING, StagePhase.DISPATCHED}
                for stage in result.stages
            )
            if terminal_lanes and not in_flight:
                return result.model_copy(update={"exit_reason": "terminal_graph"})
            await asyncio.sleep(interval)

    _drive(
        run_id,
        advance(),
        json_output=json_output,
        failure_statuses=(
            frozenset({"blocked", "failed", "report_failed"}) if watch else frozenset()
        ),
    )


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
    force: Annotated[
        bool,
        typer.Option("--force", help="Reopen a finding that is already resolved or deferred."),
    ] = False,
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
            force=force,
            note=note,
        )
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _emit(record.model_dump(mode="json"), json_output=json_output)


@app.command()
def recover(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    finding: Annotated[
        str, typer.Option("--finding", help="Historical finding this recovery supersedes.")
    ],
    file: Annotated[
        Path,
        typer.Option(
            "--file",
            "-f",
            help="YAML or JSON ReviewFinding contract for the newly proven defect.",
        ),
    ],
    note: Annotated[
        str, typer.Option("--note", help="Why this current-head recovery is authorized.")
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Create a fresh finding bound to the lane's exact recorded current head."""

    try:
        contract = ReviewFinding.model_validate(yaml.safe_load(file.read_text()))
    except (OSError, yaml.YAMLError, ValidationError, TypeError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    _drive(
        run_id,
        _controller().recover_finding(run_id, finding, contract, note),
        json_output=json_output,
    )


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
        settings, store, _ = _components()
        with run_lock(settings.database_path, run_id):
            record = store.settle_finding(run_id, finding, phase=phase, note=note)
    except (ConfigError, RunLockedError, KeyError, ValueError) as exc:
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
    """Recover a lane block or an unreadable lane-level stage report.

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


@app.command("reconcile-head")
def reconcile_head(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    lane: Annotated[str, typer.Option("--lane", help="Diverged lane to reconcile.")],
    note: Annotated[
        str, typer.Option("--note", help="Why this legacy divergence is being recovered.")
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Advance a recorded lane head only when Git and its integration ledger prove it."""

    async def reconcile() -> BaseModel:
        return await _controller().reconcile_head(run_id, lane, note)

    _drive(run_id, reconcile(), json_output=json_output)


@app.command("record-external-merge")
def record_external_merge(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    lane: Annotated[str, typer.Option("--lane", help="Lane whose pull request was merged.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Record a lane pull request merged outside orkastrator."""

    async def record() -> BaseModel:
        settings, store, _ = _components()
        matches = [item for item in store.lanes(run_id) if item.name == lane]
        if len(matches) != 1:
            raise ValueError(f"run {run_id} has no lane {lane}")
        receipts = store.publications(run_id, matches[0].lane_id)
        if not receipts:
            raise ValueError(f"lane {lane} has no published pull request")
        publisher = GitHubPublisher(gh_command=settings.github_command)
        landed = await publisher.record_external_merge(receipts[-1])
        store.record_publication(run_id, matches[0].lane_id, landed)
        return landed

    _drive(run_id, record(), json_output=json_output)


@app.command()
def reauthorize(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    note: Annotated[
        str | None, typer.Option("--note", help="Why this policy change is authorized.")
    ] = None,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Apply the change shown by a previous run of this command."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Show what a mid-run policy change actually moved, and freeze it once you have seen it.

    Acceptance digests the proposal and the config together, so editing
    `orkastrator.yaml` mid-run fails every subsequent tick. Use this when only
    the policy moved. If the proposal itself changed, this refuses and the
    honest answer is a new proposal.

    Without `--confirm` this reads the change and applies nothing, because two
    digests are not something an owner can judge and a note typed before seeing
    the diff is a claim rather than a reading. Re-run with `--confirm --note`
    to authorize what it printed.
    """

    if confirm and not note:
        _fail(
            "--confirm needs --note saying why this policy change is authorized",
            json_output=json_output,
        )
    try:
        result = _controller().reauthorize(run_id, note or "", apply=confirm)
    except (ConfigError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    if not json_output:
        for line in _policy_change_lines(run_id, result):
            typer.echo(line)
    _emit(result.model_dump(mode="json"), json_output=json_output)


@app.command()
def reap(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Close the panes shown by a previous run of this command."),
    ] = False,
    note: Annotated[
        str, typer.Option("--note", help="Why these settled agent panes are being reaped.")
    ] = "reaped by the supervisor",
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Close agent terminals a previous supervisor left behind.

    Releasing a settled stage closes the pane orkastrator opened for it, but
    only for stages dispatched after the handle was recorded on the row. Older
    stages, and stages whose supervisor died between opening the terminal and
    writing the row, settle with no local record and their agent tree stays
    resident. Orca still knows which terminal each Dispatch was attached to.

    A pane is only a candidate when its stage is both released and processed,
    so the stage in flight is held whatever Orca reports about it. That is what
    makes this safe to run against a live run, and it deliberately does not take
    the run lock, since the case it exists for is a supervisor already ticking.

    Without `--confirm` this prints what it would close and closes nothing:
    which panes it thinks are yours is the whole question.

    Do not close a held pane by hand. Closing a pane whose Dispatch was never
    released re-dispatches its Task: Orca opens a fresh worktree and starts a
    new agent on work nobody is waiting for. That is why release goes through
    `worker-release` first and why this holds anything it has not seen settle.
    Reclaiming a stage of an abandoned run means fencing the Dispatch and
    settling the Task, not killing the terminal. See KAS-632.
    """

    async def sweep() -> ReapPlan:
        _, store, orca = _components()
        record = store.run(run_id)
        if record.orca_run_id is None:
            raise ValueError(f"run {run_id} has no accepted Orca Run to sweep")
        lanes = store.lanes(run_id)
        plan = build_reap_plan(
            run_id=run_id,
            lanes=lanes,
            stages=store.stages(run_id),
            attached=await orca.worker_terminals(record.orca_run_id),
            terminals=await orca.open_terminals(),
        )
        if not confirm:
            return plan
        lane_ids = {lane.name: lane.lane_id for lane in lanes}
        for target in plan.close:
            await orca.close_terminal(target.terminal_handle)
            store.record_hand_action(
                run_id,
                lane_ids.get(target.lane),
                command="reap",
                target=target.terminal_handle,
                phase="closed",
                note=note,
            )
        if not plan.close:
            store.record_hand_action(
                run_id,
                None,
                command="reap",
                target=run_id,
                phase="nothing_to_close",
                note=note,
            )
        return replace(plan, closed=tuple(target.terminal_handle for target in plan.close))

    try:
        plan = asyncio.run(sweep())
    except (ConfigError, OrcaError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    if json_output:
        _emit(plan.to_dict(), json_output=True)
        return
    typer.echo(render_reap_plan(plan))


@app.command()
def mail(
    run_id: Annotated[str, typer.Argument(help="Accepted graph run ID.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine JSON.")] = False,
) -> None:
    """Show supervisor direction the agents in flight have not read.

    A message sent to a dispatch reports success as soon as Orca accepts it,
    and nothing afterwards says whether the agent ever looked. An agent that
    enters a wait loop around a subprocess stops checking its mailbox and does
    not resume, so direction sent after that point is dropped in silence while
    both sides believe they are in contact.

    Reads the mailbox rather than consuming it, so this is safe against a live
    run: `check` marks messages read, and doing that here would destroy the
    evidence. A non-empty listing means the supervisor is talking to itself.
    """

    async def read() -> dict[str, object]:
        _, store, orca = _components()
        lanes = {lane.lane_id: lane.name for lane in store.lanes(run_id)}
        pending: list[dict[str, object]] = []
        for stage in store.stages(run_id):
            # Only stages still in flight: a settled stage's mailbox cannot be
            # read by anyone any more, so unread there is expected, not a fault.
            if stage.released or stage.orca_dispatch_id is None:
                continue
            unread = await orca.unread_messages(stage.orca_dispatch_id)
            pending.extend(
                {
                    "lane": lanes.get(stage.lane_id, stage.lane_id),
                    "role": str(stage.role.value),
                    "dispatch_id": stage.orca_dispatch_id,
                    "sequence": message.sequence,
                    "subject": message.subject,
                }
                for message in unread
            )
        return {"run_id": run_id, "unread": pending}

    try:
        result = asyncio.run(read())
    except (ConfigError, OrcaError, KeyError, ValueError) as exc:
        _fail(str(exc), json_output=json_output)
    if json_output:
        _emit(result, json_output=True)
        return
    unread = cast(list[dict[str, object]], result["unread"])
    if not unread:
        typer.echo(f"run {run_id}\n\n  no unread direction on any stage in flight")
        return
    typer.echo(f"run {run_id}\n\n  {len(unread)} unread\n")
    for message in unread:
        typer.echo(
            f"  seq {message['sequence']}  {message['lane']}:{message['role']}"
            f"  {message['subject']}"
        )


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


def _drive[T](
    run_id: str,
    awaitable: Coroutine[Any, Any, T],
    *,
    json_output: bool,
    failure_statuses: frozenset[str] = frozenset(),
) -> None:
    """Run one command that dispatches to Orca, as the run's only driver.

    Orca refuses a `worker-start` from a terminal other than the one bound to
    the Task Run, so a second supervisor on the same run does not fail cleanly:
    it makes the first one fail, intermittently, until somebody notices. Refuse
    in front of that instead, and name the process already holding the run.
    """

    try:
        settings, _, _ = _components()
        with run_lock(settings.database_path, run_id):
            _run(
                awaitable,
                json_output=json_output,
                failure_statuses=failure_statuses,
            )
    except (ConfigError, RunLockedError) as exc:
        awaitable.close()
        _fail(str(exc), json_output=json_output)


def _run[T](
    awaitable: Coroutine[Any, Any, T],
    *,
    json_output: bool,
    failure_statuses: frozenset[str] = frozenset(),
) -> None:
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
    if getattr(result, "status", None) in failure_statuses:
        raise typer.Exit(code=1)


def _policy_change_lines(run_id: str, result: PolicyReauthorization) -> list[str]:
    """Render a policy change as sentences an owner can judge."""

    if not result.comparable:
        return [
            f"run {run_id} was accepted before the policy payload was recorded, so what "
            "changed cannot be read; only the digests differ",
            f"  config_sha256 -> {result.authorization.config_sha256}",
        ]
    if not result.changes:
        return [f"run {run_id}: the configuration on disk matches the one it was accepted under"]
    lines = [f"run {run_id}: {len(result.changes)} policy change(s) since acceptance"]
    lines.extend(f"  {item.path}: {item.before} -> {item.after}" for item in result.changes)
    if not result.applied:
        lines.append('nothing applied; re-run with --confirm --note "..." to authorize this')
    return lines


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
