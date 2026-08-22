"""Measure how much work a run spent per finding, without asking a model.

A run's health is not "did it finish". It is how many dispatches each finding
cost, how often the same work had to be scheduled twice, and what kept sending
findings to escalation. Those numbers decide whether a change to the graph
actually converged anything or just moved the failure somewhere else.

Every number here comes from rows the supervisor already wrote. Nothing is
inferred, nothing is sampled, and nothing here calls an agent - so the cost of
measuring a run is one SQLite read, and two runs are comparable because the
counting rule cannot drift between them.

The one judgement call is `repeat_stages`. Work is identified by the tuple
(lane, role, finding, round): whatever schedules that tuple a second time did
so because the first attempt did not stick. Counting extras rather than
guessing at retry markers keeps the metric stable across graph changes that
rename the markers.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from orkastrator.models import (
    FindingRecord,
    IntegrationRecord,
    LaneRecord,
    PublicationReceipt,
    StageKind,
    StageRecord,
)

__all__ = ["FindingReport", "LaneReport", "RunReport", "build_report", "render"]

# The roles that cost a dispatch per finding rather than per lane. A worker and
# an initial reviewer run once for the whole lane no matter how many findings
# come out of it, so counting them against findings would flatter a lane that
# found nothing.
_ADJUDICATION = frozenset({StageKind.FIXER, StageKind.RE_REVIEWER, StageKind.ESCALATION})


@dataclass(frozen=True, slots=True)
class FindingReport:
    """What one finding cost to drive to its current phase."""

    finding_id: str
    lane: str
    phase: str
    round: int
    dispatches: int
    repeats: int
    escalations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LaneReport:
    """One lane's outcome and the findings it produced."""

    name: str
    issue_id: str
    phase: str
    findings: int
    resolved: int
    deferred: int
    published: bool
    integrations: int
    conflicts: int
    # Escalations attributed to this lane's findings. Run-wide totals say a
    # reason recurs; they cannot say whether one lane's reviewer is producing
    # findings the downstream roles cannot act on, which is the question that
    # decides whether to fix the reviewer or the adjudicator.
    escalations: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunReport:
    """Convergence metrics for one accepted run.

    Compare two of these to decide whether a graph change helped. Falling
    `dispatches_per_finding` and `repeat_rate` are the goal; everything else is
    there to say where the cost went.
    """

    run_id: str
    lanes: tuple[LaneReport, ...] = ()
    findings: tuple[FindingReport, ...] = ()
    total_stages: int = 0
    adjudication_stages: int = 0
    dispatches_per_finding: float = 0.0
    repeat_stages: int = 0
    repeat_rate: float = 0.0
    multi_round_findings: int = 0
    starts: int = 0
    rejected_starts: int = 0
    reset_starts: int = 0
    rejection_rate: float = 0.0
    escalations_by_reason: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    overdue_stages: int = 0
    timed_out_stages: int = 0
    # What the overdue stages were doing, grouped so the histogram has one entry
    # per behaviour rather than one per observation. A run whose late stages are
    # all poll loops has a different problem from one whose late stages are all
    # working, and only this line tells them apart.
    overdue_by_activity: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_report(
    *,
    run_id: str,
    lanes: list[LaneRecord],
    stages: list[StageRecord],
    findings: list[FindingRecord],
    integrations: list[IntegrationRecord],
    publications: list[PublicationReceipt],
    events: list[dict[str, object]],
) -> RunReport:
    """Reduce one run's persisted rows to its convergence metrics."""

    lane_names = {lane.lane_id: lane.name for lane in lanes}
    published = {receipt.lane for receipt in publications}

    adjudication = [stage for stage in stages if stage.role in _ADJUDICATION]
    # (lane, role, finding, round) is the identity of one unit of work. Anything
    # beyond the first stage for that tuple is work that had to be redone.
    attempted = {(stage.lane_id, stage.role, stage.finding_id, stage.round) for stage in stages}
    repeat_stages = len(stages) - len(attempted)

    escalated: Counter[str] = Counter()
    per_finding_escalations: dict[str, list[str]] = {}
    rejected: Counter[str] = Counter()
    activities: Counter[str] = Counter()
    starts = reset_starts = overdue = timed_out = 0
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "escalation_recorded":
            reason = str(payload.get("reason") or "unknown")
            escalated[reason] += 1
            finding_id = str(payload.get("finding_id") or "")
            per_finding_escalations.setdefault(finding_id, []).append(reason)
        elif kind == "stage_started":
            starts += 1
        elif kind == "stage_start_reservation_reset":
            reset_starts += 1
        elif kind == "stage_result_rejected":
            rejected[_rejection_class(str(payload.get("reason") or ""))] += 1
        elif kind == "stage_overdue":
            overdue += 1
            activities[_activity_class(payload.get("activity"))] += 1
        elif kind == "stage_timed_out":
            timed_out += 1

    finding_reports = tuple(
        FindingReport(
            finding_id=record.finding_id,
            lane=lane_names.get(record.lane_id, record.lane_id),
            phase=str(record.phase),
            round=record.round,
            dispatches=sum(
                1
                for stage in adjudication
                if stage.lane_id == record.lane_id and stage.finding_id == record.finding_id
            ),
            repeats=_repeats_for(stages, record),
            escalations=tuple(per_finding_escalations.get(record.finding_id, ())),
        )
        for record in findings
    )

    lane_reports = tuple(
        LaneReport(
            name=lane.name,
            issue_id=lane.issue_id,
            phase=str(lane.phase),
            findings=sum(1 for record in findings if record.lane_id == lane.lane_id),
            resolved=_phase_count(findings, lane.lane_id, "resolved"),
            deferred=_phase_count(findings, lane.lane_id, "deferred"),
            published=lane.name in published,
            integrations=sum(1 for row in integrations if row.lane_id == lane.lane_id),
            conflicts=sum(
                1
                for row in integrations
                if row.lane_id == lane.lane_id and row.status == "conflict"
            ),
            escalations=dict(
                Counter(
                    reason
                    for record in findings
                    if record.lane_id == lane.lane_id
                    for reason in per_finding_escalations.get(record.finding_id, ())
                ).most_common()
            ),
        )
        for lane in lanes
    )

    rejected_total = sum(rejected.values())
    return RunReport(
        run_id=run_id,
        lanes=lane_reports,
        findings=finding_reports,
        total_stages=len(stages),
        adjudication_stages=len(adjudication),
        dispatches_per_finding=_ratio(len(adjudication), len(findings)),
        repeat_stages=repeat_stages,
        repeat_rate=_ratio(repeat_stages, len(stages)),
        multi_round_findings=sum(1 for record in findings if record.round > 1),
        starts=starts,
        rejected_starts=rejected_total,
        reset_starts=reset_starts,
        rejection_rate=_ratio(rejected_total, starts),
        escalations_by_reason=dict(escalated.most_common()),
        rejection_reasons=dict(rejected.most_common()),
        overdue_stages=overdue,
        timed_out_stages=timed_out,
        overdue_by_activity=dict(activities.most_common()),
    )


def _phase_count(findings: list[FindingRecord], lane_id: str, phase: str) -> int:
    return sum(1 for record in findings if record.lane_id == lane_id and record.phase == phase)


def _repeats_for(stages: list[StageRecord], record: FindingRecord) -> int:
    owned = [
        stage
        for stage in stages
        if stage.lane_id == record.lane_id and stage.finding_id == record.finding_id
    ]
    return len(owned) - len({(stage.role, stage.round) for stage in owned})


def _activity_class(activity: object) -> str:
    """Group an overdue stage's observed activity by behaviour, not by wording.

    `execution._stage_activity` reports either a tool name or that tool plus the
    ratio of turns that repeated. The ratio is the evidence and it moves every
    time the stage is looked at, so counting the raw strings would produce a
    histogram with one entry per observation. What a reader wants is the count
    of late stages that were burning turns on the same call, which is the
    presence of "repeated", not its numerator.
    """

    if not isinstance(activity, str) or not activity.strip():
        # Unreadable is its own answer. Folding it into a tool bucket would
        # claim knowledge of a stage nobody could observe.
        return "unknown"
    tool, _, rest = activity.partition(" ")
    return f"{tool} poll loop" if "repeated" in rest else tool


def _rejection_class(reason: str) -> str:
    """Group a rejection message by its cause, not its wording.

    Rejection reasons embed identifiers and counts, so counting the raw strings
    produces a histogram with one entry per event and tells nobody anything.
    """

    lowered = reason.lower()
    for marker, label in (
        ("does not match", "stale_changeset"),
        ("invalid structured result", "malformed_result"),
        ("outside", "scope_escape"),
        ("not a descendant", "stale_base"),
        ("empty", "empty_result"),
    ):
        if marker in lowered:
            return label
    return "other"


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator <= 0 else round(numerator / denominator, 3)


def render(report: RunReport) -> str:
    """Render the report for a human, densest number first."""

    lines = [
        f"run {report.run_id}",
        "",
        f"  dispatches per finding   {report.dispatches_per_finding}"
        f"   ({report.adjudication_stages} adjudication stages / {len(report.findings)} findings)",
        f"  repeat rate              {report.repeat_rate}"
        f"   ({report.repeat_stages} of {report.total_stages} stages redid attempted work)",
        f"  start rejection rate     {report.rejection_rate}"
        f"   ({report.rejected_starts} of {report.starts} starts;"
        f" {report.reset_starts} reservations reset)",
        f"  findings past round 1    {report.multi_round_findings} of {len(report.findings)}",
        f"  stages past soft budget  {report.overdue_stages}"
        f"   ({report.timed_out_stages} released for exceeding a hard budget)",
    ]
    lines += _histogram("overdue stages were doing", report.overdue_by_activity)
    lines += _histogram("escalations", report.escalations_by_reason)
    lines += _histogram("start rejections", report.rejection_reasons)

    lines += ["", "lanes"]
    for lane in report.lanes:
        published = "published" if lane.published else "unpublished"
        lines.append(
            f"  {lane.name} [{lane.issue_id}] {lane.phase} {published}"
            f" findings={lane.findings} resolved={lane.resolved} deferred={lane.deferred}"
            f" integrations={lane.integrations} conflicts={lane.conflicts}"
        )
        if lane.escalations:
            escalations = "  ".join(
                f"{reason}={count}" for reason, count in lane.escalations.items()
            )
            lines.append(f"      escalations {escalations}")

    costly = sorted(report.findings, key=lambda item: (-item.dispatches, item.finding_id))
    if costly:
        lines += ["", "findings by cost"]
        for item in costly:
            # Counted rather than listed: a finding that escalated for the same
            # reason thirteen times printed that reason thirteen times, which
            # buried the finding's identity under its own repetition.
            reasons = "  ".join(
                f"{reason}x{count}" if count > 1 else reason
                for reason, count in Counter(item.escalations).most_common()
            )
            escalations = f" escalated={reasons}" if reasons else ""
            lines.append(
                f"  {item.dispatches:>3} dispatches (+{item.repeats} repeat)"
                f" round={item.round} {item.phase} {item.finding_id} [{item.lane}]{escalations}"
            )
    return "\n".join(lines)


def _histogram(title: str, counts: dict[str, int]) -> list[str]:
    if not counts:
        return []
    return ["", f"{title}"] + [f"  {count:>3}  {reason}" for reason, count in counts.items()]
