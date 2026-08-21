"""YAML execution profiles plus environment-backed process configuration."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(ValueError):
    """Raised when required runtime configuration is invalid."""


class AgentProfile(BaseModel):
    """One Orca worker launch profile."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(default="codex", pattern=r"^[a-z][a-z0-9-]*$")
    model: str = Field(min_length=1, max_length=200)
    strength: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")


class RoleProfiles(BaseModel):
    """The four fixed execution roles in every lane graph."""

    model_config = ConfigDict(extra="forbid")

    worker: AgentProfile
    initial_reviewer: AgentProfile
    fixer: AgentProfile
    re_reviewer: AgentProfile


class GraphConfig(BaseModel):
    """Declarative execution-graph configuration."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    max_parallel_lanes: int = Field(default=2, ge=1, le=32)
    supervisor: AgentProfile
    roles: RoleProfiles


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for one supervisor process."""

    config_path: Path
    graph: GraphConfig
    database_path: Path
    claude_command: tuple[str, ...]
    codex_command: tuple[str, ...]
    orca_command: tuple[str, ...]
    command_timeout_seconds: float
    planner_timeout_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from explicit environment variables."""

        timeout = _positive_float("KASGRAPH_COMMAND_TIMEOUT_SECONDS", default=30.0)
        planner_timeout = _positive_float("KASGRAPH_PLANNER_TIMEOUT_SECONDS", default=300.0)
        config_path = Path(os.environ.get("KASGRAPH_CONFIG", "kasgraph.yaml")).expanduser()
        graph = load_graph_config(config_path)
        database_path = Path(
            os.environ.get(
                "KASGRAPH_DB_PATH",
                str(Path.home() / ".local" / "share" / "kasgraph" / "state.sqlite3"),
            )
        ).expanduser()
        return cls(
            config_path=config_path,
            graph=graph,
            database_path=database_path,
            claude_command=_command("KASGRAPH_CLAUDE_COMMAND", default=("claude",)),
            codex_command=_command("KASGRAPH_CODEX_COMMAND", default=("codex",)),
            orca_command=_orca_command(),
            command_timeout_seconds=timeout,
            planner_timeout_seconds=planner_timeout,
        )


def load_graph_config(path: Path) -> GraphConfig:
    """Load and validate the explicit YAML execution configuration."""

    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"Kasgraph config does not exist: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read Kasgraph config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Kasgraph config must be a YAML mapping: {path}")
    try:
        return GraphConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid Kasgraph config {path}: {exc}") from exc


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


def _command(name: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    explicit = os.environ.get(name, "").strip()
    if not explicit:
        return default
    command = tuple(shlex.split(explicit))
    if not command:
        raise ConfigError(f"{name} did not contain an executable")
    return command


def _positive_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value
