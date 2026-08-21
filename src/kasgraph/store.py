"""Small SQLite ledger for supervisor correlations and recovery."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from kasgraph.models import LanePhase, LaneRecord, SupervisorPlan


class StateStore:
    """Persist supervisor decisions without becoming external-system authority."""

    def __init__(self, path: Path):
        self.path = path

    def setup(self) -> None:
        """Create the local schema if it does not exist."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS supervisor_runs (
                    run_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lanes (
                    lane_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES supervisor_runs(run_id),
                    name TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    repo_selector TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    worktree_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES supervisor_runs(run_id),
                    lane_id TEXT,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def record_plan(self, objective: str, plan: SupervisorPlan) -> str:
        """Record one accepted plan and its proposed lanes atomically."""

        run_id = str(uuid.uuid4())
        now = _now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO supervisor_runs VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, objective, "planned", plan.model_dump_json(), now, now),
            )
            for lane in plan.lanes:
                connection.execute(
                    "INSERT INTO lanes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        run_id,
                        lane.name,
                        lane.issue_id,
                        lane.repo_selector,
                        lane.agent_id,
                        LanePhase.PROPOSED.value,
                        None,
                        now,
                        now,
                    ),
                )
            self._event(connection, run_id, None, "plan_recorded", plan.model_dump(mode="json"))
        return run_id

    def lane_for_name(self, run_id: str, name: str) -> LaneRecord:
        """Return one lane from an accepted plan."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM lanes WHERE run_id = ? AND name = ?", (run_id, name)
            ).fetchone()
        if row is None:
            raise KeyError(f"lane {name!r} is not recorded for run {run_id}")
        return _lane(row)

    def lanes(self) -> list[LaneRecord]:
        """Return all lanes in creation order."""

        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM lanes ORDER BY created_at, lane_id").fetchall()
        return [_lane(row) for row in rows]

    def mark_lane_started(self, lane_id: str, worktree_id: str, payload: object) -> None:
        """Bind a lane to the Orca worktree created for it."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id FROM lanes WHERE lane_id = ?", (lane_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown lane {lane_id}")
            run_id = str(row["run_id"])
            connection.execute(
                "UPDATE lanes SET phase = ?, worktree_id = ?, updated_at = ? WHERE lane_id = ?",
                (LanePhase.ACTIVE.value, worktree_id, now, lane_id),
            )
            connection.execute(
                "UPDATE supervisor_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                ("active", now, run_id),
            )
            self._event(connection, run_id, lane_id, "lane_started", payload)

    def update_lane_phase(self, lane_id: str, phase: LanePhase, payload: object) -> None:
        """Record a reconciled lane phase."""

        now = _now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id, phase FROM lanes WHERE lane_id = ?", (lane_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown lane {lane_id}")
            if str(row["phase"]) == phase.value:
                return
            run_id = str(row["run_id"])
            connection.execute(
                "UPDATE lanes SET phase = ?, updated_at = ? WHERE lane_id = ?",
                (phase.value, now, lane_id),
            )
            self._event(connection, run_id, lane_id, "lane_reconciled", payload)

    def counts(self) -> dict[str, int]:
        """Return small diagnostics for doctor output."""

        with self._connection() as connection:
            run_count = int(
                connection.execute("SELECT COUNT(*) FROM supervisor_runs").fetchone()[0]
            )
            lane_count = int(connection.execute("SELECT COUNT(*) FROM lanes").fetchone()[0])
        return {"runs": run_count, "lanes": lane_count}

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        run_id: str,
        lane_id: str | None,
        kind: str,
        payload: object,
    ) -> None:
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                run_id,
                lane_id,
                kind,
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
                _now(),
            ),
        )


def _lane(row: sqlite3.Row) -> LaneRecord:
    return LaneRecord(
        lane_id=str(row["lane_id"]),
        run_id=str(row["run_id"]),
        name=str(row["name"]),
        issue_id=str(row["issue_id"]),
        repo_selector=str(row["repo_selector"]),
        agent_id=str(row["agent_id"]),
        phase=LanePhase(str(row["phase"])),
        worktree_id=str(row["worktree_id"]) if row["worktree_id"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
