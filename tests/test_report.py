"""The report is the instrument that says whether a graph change converged anything.

Its numbers get compared across runs, so the counting rules are pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from orkastrator.models import (
    AllowedWriteScope,
    FindingEvidence,
    FindingLocation,
    FindingRecord,
    LaneRecord,
    ReviewFinding,
    StageKind,
    StagePhase,
    StageRecord,
)
from orkastrator.report import build_report, render

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _contract(finding_id: str, paths: tuple[str, ...] = ("a.py",)) -> ReviewFinding:
    return ReviewFinding(
        id=finding_id,
        evidence=[
            FindingEvidence(
                location=FindingLocation(path=path, start_line=1, end_line=2),
                claim="claim",
            )
            for path in paths
        ],
        failure_mode="fails",
        required_outcome="passes",
        allowed_write_scope=AllowedWriteScope(paths=list(dict.fromkeys(paths))),
    )


def _lane(lane_id: str, name: str) -> LaneRecord:
    return LaneRecord(
        lane_id=lane_id,
        run_id="run",
        name=name,
        issue_id="KAS-1",
        repo_selector="path:/tmp/repo",
        base_ref="main",
        phase="active",
        worktree_id=None,
        review_head_sha=None,
        integration_head_sha=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _finding(
    lane_id: str,
    finding_id: str,
    *,
    phase: str = "resolved",
    round: int = 1,
    origin: str = "initial_review",
    paths: tuple[str, ...] = ("a.py",),
):
    return FindingRecord(
        finding_key=f"{lane_id}:{finding_id}",
        lane_id=lane_id,
        finding_id=finding_id,
        origin=origin,
        contract=_contract(finding_id, paths),
        effective_contract=_contract(finding_id, paths),
        phase=phase,
        round=round,
        escalation_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _stage(
    lane_id: str,
    role: StageKind,
    *,
    finding_id: str | None = None,
    round: int | None = None,
    key: str = "",
    released: bool = True,
    created: datetime = NOW,
    updated: datetime = NOW,
) -> StageRecord:
    return StageRecord(
        stage_id=key or f"{lane_id}:{role}:{finding_id}:{round}",
        stage_key=key or f"{lane_id}:{role}:{finding_id}:{round}",
        lane_id=lane_id,
        role=role,
        finding_id=finding_id,
        round=round,
        attempt_kind=None,
        phase=StagePhase.COMPLETED,
        orca_task_id=None,
        orca_dispatch_id=None,
        worktree_id=None,
        result_json=None,
        processed=True,
        released=released,
        created_at=created,
        updated_at=updated,
    )


def test_only_per_finding_roles_count_against_dispatches_per_finding():
    """A worker and an initial reviewer run once per lane however many findings land.

    Counting them per finding would make a lane that found one thing look twice
    as expensive as a lane that found two, which is backwards.
    """

    lane = _lane("lane", "demo")
    stages = [
        _stage("lane", StageKind.WORKER, key="w"),
        _stage("lane", StageKind.INITIAL_REVIEWER, key="ir"),
        _stage("lane", StageKind.FIXER, finding_id="finding-one", round=1),
        _stage("lane", StageKind.RE_REVIEWER, finding_id="finding-one", round=1),
    ]

    report = build_report(
        run_id="run",
        lanes=[lane],
        stages=stages,
        findings=[_finding("lane", "finding-one")],
        integrations=[],
        publications=[],
        events=[],
    )

    assert report.total_stages == 4
    assert report.adjudication_stages == 2
    assert report.dispatches_per_finding == 2.0


def test_scheduling_the_same_work_twice_counts_as_a_repeat():
    """(lane, role, finding, round) identifies one unit of work.

    A second stage for that tuple exists because the first did not stick, and
    that is the number that has to fall for the graph to be converging.
    """

    stages = [
        _stage("lane", StageKind.FIXER, finding_id="finding-one", round=1, key="a"),
        _stage("lane", StageKind.FIXER, finding_id="finding-one", round=1, key="b"),
        _stage("lane", StageKind.RE_REVIEWER, finding_id="finding-one", round=1, key="c"),
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "demo")],
        stages=stages,
        findings=[_finding("lane", "finding-one")],
        integrations=[],
        publications=[],
        events=[],
    )

    assert report.repeat_stages == 1
    assert report.repeat_rate == round(1 / 3, 3)
    assert report.findings[0].repeats == 1


def test_escalations_are_grouped_by_reason_and_attributed_to_their_finding():
    events = [
        {
            "kind": "escalation_recorded",
            "payload": {"finding_id": "finding-one", "reason": "ambiguous_result"},
        },
        {
            "kind": "escalation_recorded",
            "payload": {"finding_id": "finding-one", "reason": "ambiguous_result"},
        },
        {
            "kind": "escalation_recorded",
            "payload": {"finding_id": "finding-two", "reason": "validation_failed"},
        },
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "demo")],
        stages=[],
        findings=[_finding("lane", "finding-one"), _finding("lane", "finding-two")],
        integrations=[],
        publications=[],
        events=events,
    )

    assert report.escalations_by_reason == {"ambiguous_result": 2, "validation_failed": 1}
    by_id = {item.finding_id: item for item in report.findings}
    assert by_id["finding-one"].escalations == ("ambiguous_result", "ambiguous_result")
    assert by_id["finding-two"].escalations == ("validation_failed",)


def test_rejections_are_grouped_by_cause_not_by_message():
    """Rejection messages embed identifiers, so raw strings histogram to one each."""

    events = [
        {"kind": "stage_started", "payload": {}},
        {"kind": "stage_started", "payload": {}},
        {
            "kind": "stage_result_rejected",
            "payload": {"reason": "invalid structured result: bad shape", "stage_id": "s1"},
        },
        {
            "kind": "stage_result_rejected",
            "payload": {"reason": "invalid structured result: other shape", "stage_id": "s2"},
        },
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "demo")],
        stages=[],
        findings=[],
        integrations=[],
        publications=[],
        events=events,
    )

    assert report.rejection_reasons == {"malformed_result": 2}
    assert report.starts == 2
    assert report.rejection_rate == 1.0


def test_rejections_separate_the_supervisor_own_faults_from_the_agent_ones():
    """Every reason shares one prefix, so matching it first hides eight causes."""

    reasons = [
        "invalid structured result: escalations contract changed after persistence",
        "invalid structured result: re-review result changed after persistence",
        "invalid structured result: fixer result is not pinned to its assigned base and exact head",
        "invalid structured result: fix attempt does not match its persisted finding and round",
        "invalid structured result: initial review revision does not match the worker changeset",
        "invalid structured result: 1 validation error for FixAttempt\ncommit_sha\n  "
        "String should match pattern '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'",
        "invalid structured result: nothing recognisable at all",
    ]
    events = [{"kind": "stage_started", "payload": {}} for _ in reasons]
    events += [
        {"kind": "stage_result_rejected", "payload": {"reason": reason, "stage_id": f"s{index}"}}
        for index, reason in enumerate(reasons)
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "demo")],
        stages=[],
        findings=[],
        integrations=[],
        publications=[],
        events=events,
    )

    assert report.rejection_reasons == {
        "identity_mismatch": 3,
        "supervisor_contract_race": 2,
        "unresolved_sha": 1,
        "malformed_result": 1,
    }


def test_an_open_stage_is_measured_to_now_and_a_released_one_to_its_last_touch():
    """Every ratio above counts finished work, so only this line sees a stuck run."""

    start = datetime(2026, 8, 22, 6, 0, tzinfo=UTC)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    stages = [
        _stage("lane", StageKind.WORKER, key="open", released=False, created=start, updated=start),
        _stage(
            "lane",
            StageKind.FIXER,
            key="done",
            created=start,
            updated=datetime(2026, 8, 22, 6, 30, tzinfo=UTC),
        ),
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "demo")],
        stages=stages,
        findings=[],
        integrations=[],
        publications=[],
        events=[],
        now=now,
    )

    assert [(item.lane, item.role, item.minutes) for item in report.live_stages] == [
        ("demo", "worker", 360)
    ]
    assert [(c.role, c.stages, c.minutes, c.median_minutes) for c in report.role_costs] == [
        ("worker", 1, 360, 360),
        ("fixer", 1, 30, 30),
    ]
    rendered = render(report)
    assert "  360m  demo:worker" in rendered
    assert "360m  worker" in rendered
    assert "1 stages  median 360m" in rendered


def test_a_finished_run_reports_the_same_minutes_however_long_ago_it_finished():
    """Two reports of one settled run must be comparable, so `now` cannot leak in."""

    start = datetime(2026, 8, 22, 6, 0, tzinfo=UTC)
    stages = [
        _stage(
            "lane",
            StageKind.WORKER,
            key="done",
            created=start,
            updated=datetime(2026, 8, 22, 7, 0, tzinfo=UTC),
        )
    ]
    kwargs = {
        "run_id": "run",
        "lanes": [_lane("lane", "demo")],
        "stages": stages,
        "findings": [],
        "integrations": [],
        "publications": [],
        "events": [],
    }

    early = build_report(**kwargs, now=datetime(2026, 8, 22, 8, 0, tzinfo=UTC))
    late = build_report(**kwargs, now=datetime(2026, 9, 22, 8, 0, tzinfo=UTC))

    assert early.role_costs == late.role_costs
    assert [(c.role, c.minutes) for c in early.role_costs] == [("worker", 60)]
    assert early.live_stages == late.live_stages == ()


def test_late_stages_are_grouped_by_behaviour_not_by_the_ratio_that_proves_it():
    """A poll loop's ratio moves every observation; the histogram must not."""

    events = [
        {"kind": "stage_overdue", "payload": {"activity": "exec repeated 9/10 turns unchanged"}},
        {"kind": "stage_overdue", "payload": {"activity": "exec repeated 16/21 turns unchanged"}},
        {"kind": "stage_overdue", "payload": {"activity": "exec"}},
        {"kind": "stage_overdue", "payload": {"activity": None}},
        {"kind": "stage_timed_out", "payload": {"minutes": 240}},
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "demo")],
        stages=[],
        findings=[],
        integrations=[],
        publications=[],
        events=events,
    )

    assert report.overdue_stages == 4
    assert report.timed_out_stages == 1
    assert report.overdue_by_activity == {"exec poll loop": 2, "exec": 1, "unknown": 1}

    rendered = render(report)
    assert "stages past soft budget  4" in rendered
    assert "exec poll loop" in rendered
    # The evidence stays out of the histogram, so the line is stable across runs.
    assert "9/10" not in rendered


def test_a_run_with_no_late_stages_says_so_without_an_empty_histogram():
    report = build_report(
        run_id="run",
        lanes=[],
        stages=[],
        findings=[],
        integrations=[],
        publications=[],
        events=[],
    )

    assert report.overdue_by_activity == {}
    assert "overdue stages were doing" not in render(report)


def test_escalations_are_attributed_to_the_lane_whose_reviewer_produced_them():
    """Run-wide totals cannot say which reviewer is writing unactionable findings."""

    findings = [
        _finding("lane-a", "finding-one"),
        _finding("lane-b", "finding-two"),
        _finding("lane-b", "finding-three"),
    ]
    events = [
        {
            "kind": "escalation_recorded",
            "payload": {"finding_id": "finding-one", "reason": "ambiguous"},
        },
        {
            "kind": "escalation_recorded",
            "payload": {"finding_id": "finding-two", "reason": "ambiguous"},
        },
        {
            "kind": "escalation_recorded",
            "payload": {"finding_id": "finding-two", "reason": "ambiguous"},
        },
        {
            "kind": "escalation_recorded",
            "payload": {"finding_id": "finding-three", "reason": "conflict"},
        },
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane-a", "alpha"), _lane("lane-b", "beta")],
        stages=[],
        findings=findings,
        integrations=[],
        publications=[],
        events=events,
    )

    by_name = {lane.name: lane.escalations for lane in report.lanes}
    assert by_name["alpha"] == {"ambiguous": 1}
    assert by_name["beta"] == {"ambiguous": 2, "conflict": 1}

    rendered = render(report)
    assert "escalations ambiguous=2  conflict=1" in rendered
    # A finding that escalated twice for one reason names it once, with a count.
    assert "escalated=ambiguousx2" in rendered


def test_an_empty_run_reports_zeroes_rather_than_dividing_by_zero():
    report = build_report(
        run_id="run",
        lanes=[],
        stages=[],
        findings=[],
        integrations=[],
        publications=[],
        events=[],
    )

    assert report.dispatches_per_finding == 0.0
    assert report.repeat_rate == 0.0
    assert report.rejection_rate == 0.0
    assert "run run" in render(report)


def test_the_rendered_report_leads_with_the_two_numbers_that_must_fall():
    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "demo")],
        stages=[
            _stage("lane", StageKind.FIXER, finding_id="finding-one", round=1, key="a"),
            _stage("lane", StageKind.FIXER, finding_id="finding-one", round=1, key="b"),
        ],
        findings=[_finding("lane", "finding-one", phase="deferred", round=2)],
        integrations=[],
        publications=[],
        events=[],
    )

    text = render(report)

    assert "dispatches per finding" in text
    assert "repeat rate" in text
    assert "findings past round 1    1 of 1" in text
    assert "finding-one" in text


def test_a_file_argued_over_by_several_findings_is_named_once_not_per_finding():
    """The signature of a loop that cannot settle: same file, new finding id."""

    findings = [
        # Three separate findings, three different ids, one file. Counted by id
        # this is progress; counted by file it is the same argument three times.
        _finding("lane", "finding-target-selection", paths=("upload.py",)),
        _finding(
            "lane",
            "finding-target-overcorrected",
            origin="introduced_by_fix",
            paths=("upload.py",),
            round=2,
        ),
        _finding(
            "lane",
            "finding-target-diverged",
            origin="introduced_by_fix",
            paths=("upload.py", "names.py"),
            round=3,
        ),
        # A file only one finding touched is not contested and is left out.
        _finding("lane", "finding-elsewhere", paths=("quiet.py",)),
    ]

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=[],
        findings=findings,
        integrations=[],
        publications=[],
        events=[],
        now=NOW,
    )

    assert [region.path for region in report.contested_regions] == ["upload.py"]
    contested = report.contested_regions[0]
    assert (contested.findings, contested.introduced, contested.max_round) == (3, 2, 3)
    assert contested.finding_ids == (
        "finding-target-diverged",
        "finding-target-overcorrected",
        "finding-target-selection",
    )
    rendered = render(report)
    assert "contested regions" in rendered
    assert "2 from a fix" in rendered
    assert "quiet.py" not in rendered


def test_one_finding_citing_a_file_six_times_is_not_six_findings():
    """De-duplicated per finding, so a many-hunk finding cannot fake a dispute."""

    report = build_report(
        run_id="run",
        lanes=[_lane("lane", "alpha")],
        stages=[],
        findings=[_finding("lane", "finding-one", paths=("upload.py", "upload.py"))],
        integrations=[],
        publications=[],
        events=[],
        now=NOW,
    )

    assert report.contested_regions == ()
