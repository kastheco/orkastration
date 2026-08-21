"""SQLite correlation ledger for proposals and Orca execution graphs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from kasgraph.models import (
    ROLE_ORDER,
    LanePhase,
    LaneRecord,
    RoleName,
    RunRecord,
    StagePhase,
    StageRecord,
    SupervisorPlan,
)


class StateStore:
    """Persist local correlation without replacing Orca as runtime authority."""

    def __init__(self, path: Path):
        self.path = path

    def setup(self) -> None:
        """Create the schema and apply the one additive scaffold migration."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS supervisor_runs (
                    run_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    orca_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lanes (
                    lane_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES supervisor_runs(run_id),
                    name TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    repo_selector TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT 'graph',
                    phase TEXT NOT NULL,
                    worktree_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, name)
                );
                CREATE TABLE IF NOT EXISTS lane_stages (
                    stage_id TEXT PRIMARY KEY,
                    lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
                    role TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    orca_task_id TEXT,
                    orca_dispatch_id TEXT,
                    released INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(lane_id, role)
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
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(supervisor_runs)").fetchall()
            }
            if "orca_run_id" not in columns:
                connection.execute("ALTER TABLE supervisor_runs ADD COLUMN orca_run_id TEXT")

    def record_proposal(self, proposal: SupervisorPlan) -> str:
        """Record a proposal and its fixed four-stage lane graphs atomically."""

        run_id = str(uuid.uuid4())
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO supervisor_runs
                    (run_id, objective, status, plan_json, orca_run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    proposal.objective,
                    "proposed",
                    proposal.model_dump_json(),
                    None,
                    now,
                    now,
                ),
            )
            for lane in proposal.lanes:
                lane_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO lanes
                        (lane_id, run_id, name, issue_id, repo_selector, agent_id, phase,
                         worktree_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lane_id,
                        run_id,
                        lane.name,
                        lane.issue_id,
                        lane.repo_selector,
                        "graph",
                        LanePhase.PROPOSED.value,
                        None,
                        now,
                        now,
                    ),
                )
                for role in ROLE_ORDER:
                    connection.execute(
                        """
                        INSERT INTO lane_stages
                            (stage_id, lane_id, role, phase, orca_task_id, orca_dispatch_id,
                             released, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            lane_id,
                            role.value,
                            StagePhase.PENDING.value,
                            None,
                            None,
                            0,
                            now,
                            now,
                        ),
                    )
            self._event(
                connection,
                run_id,
                None,
                "proposal_recorded",
                proposal.model_dump(mode="json"),
            )
        return run_id

    def run(self, run_id: str) -> RunRecord:
        """Return one recorded run."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM supervisor_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id}")
        return _run(row)

    def lanes(self, run_id: str | None = None) -> list[LaneRecord]:
        """Return lanes in stable creation order."""

        query = "SELECT * FROM lanes"
        arguments: tuple[str, ...] = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            arguments = (run_id,)
        query += " ORDER BY created_at, lane_id"
        with self._connection() as connection:
            rows = connection.execute(query, arguments).fetchall()
        return [_lane(row) for row in rows]

    def stages(self, run_id: str) -> list[StageRecord]:
        """Return every stage for a run in lane and role order."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT lane_stages.*
                FROM lane_stages
                JOIN lanes USING (lane_id)
                WHERE lanes.run_id = ?
                ORDER BY lanes.created_at,
                    CASE lane_stages.role
                        WHEN 'worker' THEN 1
                        WHEN 'initial_reviewer' THEN 2
                        WHEN 'fixer' THEN 3
                        WHEN 're_reviewer' THEN 4
                    END
                """,
                (run_id,),
            ).fetchall()
        return [_stage(row) for row in rows]

    def mark_accepted(self, run_id: str, orca_run_id: str) -> None:
        """Bind an accepted proposal to its authoritative Orca Run."""

        now = _now()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE supervisor_runs
                SET status = 'active', orca_run_id = ?, updated_at = ?
                WHERE run_id = ? AND status = 'proposed'
                """,
                (orca_run_id, now, run_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"run {run_id} is not awaiting acceptance")
            self._event(connection, run_id, None, "proposal_accepted", {"orca_run_id": orca_run_id})

    def bind_stage_task(self, run_id: str, lane_id: str, role: RoleName, task_id: str) -> None:
        """Bind one local stage to its Orca Task."""

        now = _now()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE lane_stages
                SET orca_task_id = ?, phase = ?, updated_at = ?
                WHERE lane_id = ? AND role = ? AND orca_task_id IS NULL
                """,
                (task_id, StagePhase.READY.value, now, lane_id, role.value),
            ).rowcount
            if changed != 1:
                raise ValueError(f"stage {role.value} is already bound for lane {lane_id}")
            self._event(
                connection,
                run_id,
                lane_id,
                "stage_task_bound",
                {"role": role, "task_id": task_id},
            )

    def mark_stage_started(
        self,
        run_id: str,
        lane_id: str,
        role: RoleName,
        dispatch_id: str,
        worktree_id: str,
        payload: object,
    ) -> None:
        """Record a started Dispatch and its lane worktree."""

        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE lane_stages
                SET phase = ?, orca_dispatch_id = ?, updated_at = ?
                WHERE lane_id = ? AND role = ?
                """,
                (StagePhase.DISPATCHED.value, dispatch_id, now, lane_id, role.value),
            )
            connection.execute(
                """
                UPDATE lanes SET phase = ?, worktree_id = ?, updated_at = ? WHERE lane_id = ?
                """,
                (LanePhase.ACTIVE.value, worktree_id, now, lane_id),
            )
            self._event(connection, run_id, lane_id, "stage_started", payload)

    def sync_stage(self, run_id: str, lane_id: str, role: RoleName, phase: StagePhase) -> None:
        """Update a stage from authoritative Orca Task state."""

        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE lane_stages SET phase = ?, updated_at = ?
                WHERE lane_id = ? AND role = ?
                """,
                (phase.value, now, lane_id, role.value),
            )
            self._event(
                connection,
                run_id,
                lane_id,
                "stage_reconciled",
                {"role": role, "phase": phase},
            )

    def mark_released(self, run_id: str, lane_id: str, role: RoleName) -> None:
        """Record that Orca released a settled worker terminal."""

        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE lane_stages SET released = 1, updated_at = ?
                WHERE lane_id = ? AND role = ?
                """,
                (now, lane_id, role.value),
            )
            self._event(connection, run_id, lane_id, "worker_released", {"role": role})

    def set_terminal_status(self, run_id: str, status: str) -> None:
        """Set the local run and lane terminal state."""

        if status not in {"complete", "failed", "blocked"}:
            raise ValueError(f"invalid terminal status {status}")
        now = _now()
        lane_phase = LanePhase(status)
        with self._connection() as connection:
            connection.execute(
                "UPDATE supervisor_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, now, run_id),
            )
            connection.execute(
                "UPDATE lanes SET phase = ?, updated_at = ? WHERE run_id = ?",
                (lane_phase.value, now, run_id),
            )
            self._event(connection, run_id, None, "run_terminal", {"status": status})

    def counts(self) -> dict[str, int]:
        """Return small diagnostics for doctor output."""

        with self._connection() as connection:
            run_count = int(
                connection.execute("SELECT COUNT(*) FROM supervisor_runs").fetchone()[0]
            )
            lane_count = int(connection.execute("SELECT COUNT(*) FROM lanes").fetchone()[0])
            stage_count = int(connection.execute("SELECT COUNT(*) FROM lane_stages").fetchone()[0])
        return {"runs": run_count, "lanes": lane_count, "stages": stage_count}

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


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        orca_run_id=str(row["orca_run_id"]) if row["orca_run_id"] is not None else None,
        proposal=SupervisorPlan.model_validate_json(str(row["plan_json"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _lane(row: sqlite3.Row) -> LaneRecord:
    return LaneRecord(
        lane_id=str(row["lane_id"]),
        run_id=str(row["run_id"]),
        name=str(row["name"]),
        issue_id=str(row["issue_id"]),
        repo_selector=str(row["repo_selector"]),
        phase=LanePhase(str(row["phase"])),
        worktree_id=str(row["worktree_id"]) if row["worktree_id"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _stage(row: sqlite3.Row) -> StageRecord:
    return StageRecord(
        stage_id=str(row["stage_id"]),
        lane_id=str(row["lane_id"]),
        role=RoleName(str(row["role"])),
        phase=StagePhase(str(row["phase"])),
        orca_task_id=str(row["orca_task_id"]) if row["orca_task_id"] is not None else None,
        orca_dispatch_id=(
            str(row["orca_dispatch_id"]) if row["orca_dispatch_id"] is not None else None
        ),
        released=bool(row["released"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
