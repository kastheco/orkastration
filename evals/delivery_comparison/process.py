"""Bounded, process-group-safe subprocess execution."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import ProcessEvidence

_DEFAULT_KEEP_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR")


def offline_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment without inherited credentials or proxy configuration."""

    env = {key: os.environ[key] for key in _DEFAULT_KEEP_ENV if key in os.environ}
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PIP_NO_INDEX": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if extra:
        env.update(extra)
    return env


def _bounded_read(path: Path, limit: int) -> tuple[str, bool]:
    size = path.stat().st_size
    with path.open("rb") as stream:
        data = stream.read(limit)
    return data.decode("utf-8", errors="replace"), size > limit


def _evidence(
    command: list[str],
    started: float,
    stdout_path: Path,
    stderr_path: Path,
    limit: int,
    *,
    exit_code: int | None,
    timed_out: bool,
    launch_error: str | None,
) -> ProcessEvidence:
    stdout_text, stdout_truncated = _bounded_read(stdout_path, limit)
    stderr_text, stderr_truncated = _bounded_read(stderr_path, limit)
    return ProcessEvidence(
        argv=command,
        exit_code=exit_code,
        timed_out=timed_out,
        wall_time_seconds=round(time.monotonic() - started, 6),
        stdout=stdout_text,
        stderr=stderr_text,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        launch_error=launch_error,
    )


def _kill_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    output_limit_bytes: int = 64 * 1024,
    environment: Mapping[str, str] | None = None,
) -> ProcessEvidence:
    """Run argv without a shell and always reap its detached process group."""

    command = list(argv)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="delivery-process-") as temporary:
        stdout_path = Path(temporary, "stdout")
        stderr_path = Path(temporary, "stderr")
        timed_out = False
        exit_code: int | None = None
        launch_error: str | None = None
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=offline_environment(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError as exc:
                launch_error = f"{type(exc).__name__}: {exc}"
            else:
                try:
                    exit_code = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(process, signal.SIGTERM)
                    try:
                        exit_code = process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        _kill_group(process, signal.SIGKILL)
                        exit_code = process.wait()
                _kill_group(process, signal.SIGKILL)
        return _evidence(
            command,
            started,
            stdout_path,
            stderr_path,
            output_limit_bytes,
            exit_code=exit_code,
            timed_out=timed_out,
            launch_error=launch_error,
        )


def run_until_path(
    argv: Sequence[str],
    *,
    cwd: Path,
    signal_path: Path,
    timeout_seconds: float,
    output_limit_bytes: int = 64 * 1024,
    environment: Mapping[str, str] | None = None,
) -> tuple[ProcessEvidence, bool]:
    """Kill a live detached process group only after it publishes the fault handshake."""

    command = list(argv)
    started = time.monotonic()
    interrupted = False
    with tempfile.TemporaryDirectory(prefix="delivery-fault-") as temporary:
        stdout_path = Path(temporary, "stdout")
        stderr_path = Path(temporary, "stderr")
        timed_out = False
        exit_code: int | None = None
        launch_error: str | None = None
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=offline_environment(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError as exc:
                launch_error = f"{type(exc).__name__}: {exc}"
            else:
                deadline = started + timeout_seconds
                while time.monotonic() < deadline:
                    if signal_path.is_file() and process.poll() is None:
                        interrupted = True
                        _kill_group(process, signal.SIGKILL)
                        exit_code = process.wait()
                        break
                    polled = process.poll()
                    if polled is not None:
                        exit_code = polled
                        break
                    time.sleep(0.01)
                else:
                    timed_out = True
                    _kill_group(process, signal.SIGKILL)
                    exit_code = process.wait()
                _kill_group(process, signal.SIGKILL)
        return (
            _evidence(
                command,
                started,
                stdout_path,
                stderr_path,
                output_limit_bytes,
                exit_code=exit_code,
                timed_out=timed_out,
                launch_error=launch_error,
            ),
            interrupted,
        )
