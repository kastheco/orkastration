"""Environment-backed configuration with no implicit dotenv loading."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when required runtime configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for one supervisor process."""

    model: str | None
    database_path: Path
    orca_command: tuple[str, ...]
    max_parallel_lanes: int
    command_timeout_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from explicit environment variables."""

        max_parallel = _positive_int("KASGRAPH_MAX_PARALLEL_LANES", default=2)
        timeout = _positive_float("KASGRAPH_COMMAND_TIMEOUT_SECONDS", default=30.0)
        raw_model = os.environ.get("KASGRAPH_MODEL", "").strip()
        database_path = Path(
            os.environ.get(
                "KASGRAPH_DB_PATH",
                str(Path.home() / ".local" / "share" / "kasgraph" / "state.sqlite3"),
            )
        ).expanduser()
        return cls(
            model=raw_model or None,
            database_path=database_path,
            orca_command=_orca_command(),
            max_parallel_lanes=max_parallel,
            command_timeout_seconds=timeout,
        )

    def require_model(self) -> str:
        """Return the configured model or raise an actionable error."""

        if self.model is None:
            raise ConfigError("KASGRAPH_MODEL is required for plan and run commands")
        return self.model


def _orca_command() -> tuple[str, ...]:
    explicit = os.environ.get("ORCA_CLI_COMMAND", "").strip()
    if explicit:
        command = tuple(shlex.split(explicit))
        if not command:
            raise ConfigError("ORCA_CLI_COMMAND did not contain an executable")
        return command
    if sys.platform.startswith("linux") and shutil.which("orca-ide"):
        return ("orca-ide",)
    return ("orca",)


def _positive_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigError(f"{name} must be at least 1")
    return value


def _positive_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value
