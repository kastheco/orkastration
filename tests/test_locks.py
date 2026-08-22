"""One driver per run, and the kernel is what decides."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from orkastrator.locks import RunLockedError, held_runs, holder, lock_path, run_lock

RUN = "1f13dd37-342a-4cd1-8137-c9a06fbcaaf3"


def _hold(database: Path, run_id: str, ready: Path) -> subprocess.Popen[bytes]:
    """A separate process holding the lock until it is killed."""

    script = textwrap.dedent(f"""
        import pathlib, time
        from orkastrator.locks import run_lock
        with run_lock(pathlib.Path({str(database)!r}), {run_id!r}):
            pathlib.Path({str(ready)!r}).write_text("held")
            time.sleep(300)
    """)
    return subprocess.Popen([sys.executable, "-c", script])


def test_a_second_driver_is_refused_and_told_who_has_the_run(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"

    def take_it_again() -> None:
        with run_lock(database, RUN):
            pytest.fail("two drivers acquired the same run")

    with run_lock(database, RUN), pytest.raises(RunLockedError) as refused:
        take_it_again()

    assert RUN in str(refused.value)
    assert f"pid {os.getpid()}" in str(refused.value)
    # Say what the collision actually causes, not just that it happened.
    assert "consumer_fenced" in str(refused.value)


def test_two_different_runs_do_not_contend(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"

    with run_lock(database, RUN), run_lock(database, "other-run"):
        assert {entry["run_id"] for entry in held_runs(database)} == {RUN, "other-run"}


def test_a_released_lock_reads_as_free_even_though_the_file_remains(tmp_path: Path) -> None:
    """The file is an artifact. `flock` is the fact.

    This is why there is no stale-lock breaking command: a lock left behind by a
    process that died is already free, because the kernel released it when the
    process did.
    """

    database = tmp_path / "state.sqlite3"
    with run_lock(database, RUN):
        pass

    path = lock_path(database, RUN)
    assert path.is_file()
    assert json.loads(path.read_text())["pid"] == os.getpid()
    assert holder(path) is None
    assert held_runs(database) == []


def test_a_lock_held_by_another_process_is_reported_with_that_process(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    ready = tmp_path / "ready"

    process = _hold(database, RUN, ready)
    try:
        deadline = time.monotonic() + 30
        while not ready.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.is_file(), "helper never acquired the lock"

        with pytest.raises(RunLockedError, match=f"pid {process.pid}"), run_lock(database, RUN):
            pytest.fail("acquired a run another process is driving")

        assert [entry["pid"] for entry in held_runs(database)] == [process.pid]
    finally:
        process.kill()
        process.wait(timeout=10)

    # And the moment that process is gone, so is the lock. Nothing to clean up.
    assert held_runs(database) == []


def test_an_unreadable_lock_file_still_reports_the_run_as_held(tmp_path: Path) -> None:
    """Whether it is held never depends on parsing the payload."""

    database = tmp_path / "state.sqlite3"
    path = lock_path(database, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)

    with run_lock(database, RUN):
        path.write_text("not json")
        assert holder(path) == {"pid": None, "host": None, "held_since": None}
        assert [entry["run_id"] for entry in held_runs(database)] == [RUN]


def test_held_runs_is_empty_before_anything_has_ever_run(tmp_path: Path) -> None:
    assert held_runs(tmp_path / "state.sqlite3") == []
