"""One driver per run, enforced by the kernel rather than by convention.

Orca binds a Task Run to the coordinator terminal that started its workers, and
refuses a `worker-start` from anyone else with `consumer_fenced`. That check is
correct and it is also too late: by the time it fires, two supervisors have
already been reconciling the same run against each other for however long
nobody noticed. A second ticker does not announce itself. It just makes the
first one fail intermittently.

So the check moves in front. `flock` is the right primitive because the answer
does not depend on anything the holder remembers to do: the kernel releases an
`flock` when the holding process dies, however it dies, so a crashed supervisor
never leaves a lock somebody has to reason about and break. That removes the
whole stale-lock question rather than answering it, which is the point.

The identity written into the file is for the error message only. Whether the
lock is held is decided by `flock` alone, never by reading that payload.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path


class RunLockedError(RuntimeError):
    """Raised when another live process is already driving this run."""


def lock_path(database_path: Path, run_id: str) -> Path:
    """Where one run's driver lock lives, beside the database it drives."""

    return database_path.parent / "locks" / f"{run_id}.lock"


@contextlib.contextmanager
def run_lock(database_path: Path, run_id: str) -> Iterator[Path]:
    """Hold the exclusive right to drive one run, or refuse and say who has it.

    The descriptor stays open for the body's lifetime because that is what the
    lock is: closing it releases. Held locks are therefore visible to `doctor`
    as files it cannot acquire, which is a live fact rather than a record that
    could be out of date.
    """

    path = lock_path(database_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RunLockedError(
            f"run {run_id} is already being driven by {describe_holder(path)}; "
            "two supervisors on one run is what produces Orca's consumer_fenced. "
            "Stop the other one, or watch it instead of starting a second."
        ) from exc
    try:
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "held_since": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            handle,
        )
        handle.flush()
        yield path
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def holder(path: Path) -> dict[str, object] | None:
    """Identify who holds this lock, or None when nobody does.

    Acquiring and immediately releasing is the test. The file's contents are
    read only once `flock` has already said somebody holds it, so a leftover
    file from a dead process reads as free no matter what it still says inside.
    """

    if not path.is_file():
        return None
    try:
        handle = path.open("a+")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            try:
                payload = json.load(handle)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {"pid": None, "host": None, "held_since": None}
            return payload if isinstance(payload, dict) else {}
        fcntl.flock(handle, fcntl.LOCK_UN)
        return None
    finally:
        handle.close()


def describe_holder(path: Path) -> str:
    """One readable line naming the process that holds this lock."""

    payload = holder(path)
    if payload is None:
        return "another process"
    pid = payload.get("pid")
    host = payload.get("host")
    since = payload.get("held_since")
    return f"pid {pid} on {host} since {since}"


def held_runs(database_path: Path) -> list[dict[str, object]]:
    """Every run currently being driven, for `doctor` to report."""

    directory = database_path.parent / "locks"
    if not directory.is_dir():
        return []
    active: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.lock")):
        payload = holder(path)
        if payload is not None:
            active.append({"run_id": path.stem, **payload})
    return active
