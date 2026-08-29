"""Deterministic, score-free aggregation and report generation."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from .models import AdapterDelta, AggregateMetrics, ComparisonReport, TrialResult


def _median(values: list[int | float]) -> float:
    return round(float(statistics.median(values)), 6) if values else 0.0


def build_report(trials: list[TrialResult]) -> ComparisonReport:
    ordered = sorted(trials, key=lambda trial: trial.trial_id)
    modes = {trial.comparison_mode for trial in ordered}
    if len(modes) != 1:
        raise ValueError("one report cannot mix comparison modes")
    comparison_mode = next(iter(modes))
    grouped: dict[str, list[TrialResult]] = defaultdict(list)
    for trial in ordered:
        grouped[trial.adapter_id].append(trial)
    aggregates: list[AggregateMetrics] = []
    for adapter_id, rows in sorted(grouped.items()):
        aggregates.append(
            AggregateMetrics(
                adapter_id=adapter_id,
                comparison_mode=comparison_mode,
                config_digest=rows[0].config_digest,
                trials=len(rows),
                successes=sum(row.success for row in rows),
                median_wall_time_seconds=_median([row.wall_time_seconds for row in rows]),
                median_model_calls=_median([row.model_calls for row in rows]),
                median_tokens=_median([row.input_tokens + row.output_tokens for row in rows]),
                median_cost_usd=_median([row.cost_usd for row in rows]),
                median_supervisor_turns=_median([row.supervisor_turns for row in rows]),
                total_human_interruptions=sum(row.human_interruptions for row in rows),
                total_reviewer_calls=sum(row.reviewer_calls for row in rows),
                total_fixer_calls=sum(row.fixer_calls for row in rows),
                duplicate_action_trials=sum(bool(row.duplicate_action_ids) for row in rows),
                lost_work_trials=sum(row.lost_committed_work for row in rows),
                crash_recovery_trials=sum(row.crash_recovery for row in rows),
                scope_violation_trials=sum(bool(row.scope_violations) for row in rows),
                infrastructure_failure_trials=sum(
                    row.classification == "infrastructure_failure" for row in rows
                ),
            )
        )
    deltas: list[AdapterDelta] = []
    if len(aggregates) == 2:
        first, second = aggregates
        deltas.append(
            AdapterDelta(
                comparison_mode=comparison_mode,
                adapter_a=first.adapter_id,
                adapter_b=second.adapter_id,
                success_delta=first.successes - second.successes,
                median_wall_time_seconds_delta=round(
                    first.median_wall_time_seconds - second.median_wall_time_seconds, 6
                ),
                median_supervisor_turns_delta=round(
                    first.median_supervisor_turns - second.median_supervisor_turns, 6
                ),
                crash_recovery_trials_delta=(
                    first.crash_recovery_trials - second.crash_recovery_trials
                ),
            )
        )
    return ComparisonReport(
        comparison_mode=comparison_mode,
        trials=ordered,
        aggregates=aggregates,
        deltas=deltas,
    )


def write_report(report: ComparisonReport, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    lines = [
        "# Delivery comparison",
        "",
        f"Comparison mode: `{report.comparison_mode}`.",
        "",
        "No weighted score is computed.",
        "",
    ]
    lines.extend(
        [
            "| adapter | successes/trials | median wall (s) | median supervisor turns | "
            "crash recoveries | infra failures |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for aggregate in report.aggregates:
        lines.append(
            f"| {aggregate.adapter_id} | {aggregate.successes}/{aggregate.trials} | "
            f"{aggregate.median_wall_time_seconds:.6f} | "
            f"{aggregate.median_supervisor_turns:.1f} | "
            f"{aggregate.crash_recovery_trials} | "
            f"{aggregate.infrastructure_failure_trials} |"
        )
    lines.extend(["", "## Trials", ""])
    for trial in report.trials:
        lines.append(
            f"- `{trial.trial_id}`: **{trial.classification}**; "
            f"behavior={trial.verifier.behavior_passed}, tree={trial.verifier.exact_tree_passed}"
        )
    (output / "summary.md").write_text("\n".join(lines) + "\n")
