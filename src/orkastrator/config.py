"""YAML execution profiles plus environment-backed process configuration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from orkastrator.models import ConfigChange


class ConfigError(ValueError):
    """Raised when required runtime configuration is invalid."""


class AgentProfile(BaseModel):
    """One Orca worker launch profile."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(default="codex", pattern=r"^[a-z][a-z0-9-]*$")
    model: str = Field(min_length=1, max_length=200)
    strength: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    fast: bool = False


class FallbackProfile(AgentProfile):
    """A bounded replacement used only for a declared capability mismatch."""

    trigger: Literal["capability_mismatch"]


class FixerProfile(AgentProfile):
    """Primary fixer profile plus its optional capability fallback."""

    fallback: FallbackProfile | None = None


class RoleProfiles(BaseModel):
    """The four configured roles available to dynamic lane stages."""

    model_config = ConfigDict(extra="forbid")

    worker: AgentProfile
    initial_reviewer: AgentProfile
    fixer: FixerProfile
    re_reviewer: AgentProfile


class StageBudget(BaseModel):
    """How long one dispatched stage may run before that is itself the signal."""

    model_config = ConfigDict(extra="forbid")

    soft_minutes: int | None = Field(default=None, ge=1, le=1440)
    hard_minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode="after")
    def hard_is_not_before_soft(self) -> StageBudget:
        if (
            self.soft_minutes is not None
            and self.hard_minutes is not None
            and self.hard_minutes < self.soft_minutes
        ):
            raise ValueError("hard_minutes cannot be earlier than soft_minutes")
        return self


class StageBudgets(BaseModel):
    """Per-role wall-clock budgets for a dispatched stage.

    Both halves are optional and unset by default, because a budget is a claim
    about how long this repository's work takes and no default can know that.
    An unset budget is the behaviour that shipped before: wait forever.
    """

    model_config = ConfigDict(extra="forbid")

    worker: StageBudget = Field(default_factory=StageBudget)
    initial_reviewer: StageBudget = Field(default_factory=StageBudget)
    fixer: StageBudget = Field(default_factory=StageBudget)
    re_reviewer: StageBudget = Field(default_factory=StageBudget)
    escalation: StageBudget = Field(default_factory=StageBudget)
    # How many hard timeouts one stage may spend before the lane blocks instead.
    # Releasing a wedged worker is worth doing twice; a stage that wedges every
    # time is telling you something a third dispatch will not.
    max_timeouts: int = Field(default=2, ge=1, le=10)

    def for_role(self, role: str) -> StageBudget:
        """The budget configured for one stage role."""

        return cast(StageBudget, getattr(self, role, StageBudget()))


class NewFindingPolicy(BaseModel):
    """Which findings may enter an already-frozen review run."""

    model_config = ConfigDict(extra="forbid")

    introduced_by_fix: Literal["accept"]
    otherwise: Literal["defer_to_next_review_run"]


class ScopePolicy(BaseModel):
    """Deterministic fixer scope enforcement policy."""

    model_config = ConfigDict(extra="forbid")

    escape: Literal["stop_and_escalate"]
    reject_out_of_scope_diff: Literal[True]
    required_boundary: Literal["paths"]
    symbols: Literal["enforce_when_adapter_available"]


class ParallelFixerPolicy(BaseModel):
    """Isolation and integration rules for per-finding fixers."""

    model_config = ConfigDict(extra="forbid")

    max_per_lane: int = Field(ge=1, le=32)
    workspace: Literal["isolated_from_review_revision"]
    require_disjoint_write_scopes: Literal[True]
    on_overlap: Literal["serialize"]
    integration: Literal["serial"]


class ReviewCycleConfig(BaseModel):
    """Frozen-finding convergence policy for one lane review run."""

    model_config = ConfigDict(extra="forbid")

    initial_scope: Literal["lane_changeset"]
    freeze_findings_after_initial_review: Literal[True]
    max_fix_rounds_per_finding: int = Field(ge=1, le=2)
    re_review_scope: list[
        Literal[
            "finding_contract",
            "relevant_original_context",
            "fixer_diff",
            "validation_evidence",
        ]
    ] = Field(min_length=4, max_length=4)
    new_findings_during_re_review: NewFindingPolicy
    scope: ScopePolicy
    parallel_fixers: ParallelFixerPolicy
    escalation: AgentProfile

    @model_validator(mode="after")
    def complete_re_review_scope(self) -> ReviewCycleConfig:
        """Require every settled evidence input exactly once."""

        required = {
            "finding_contract",
            "relevant_original_context",
            "fixer_diff",
            "validation_evidence",
        }
        if set(self.re_review_scope) != required:
            raise ValueError("re_review_scope must contain every settled evidence input once")
        return self


class BranchPublicationConfig(BaseModel):
    """Authorized branch mutations for an accepted run."""

    model_config = ConfigDict(extra="forbid")

    create: Literal[True]
    push: Literal[True]
    force_push: Literal[False]


class PullRequestPublicationConfig(BaseModel):
    """Authorized pull-request lifecycle for an accepted run."""

    model_config = ConfigDict(extra="forbid")

    create_or_update: Literal[True]
    initial_state: Literal["draft"]
    mark_ready_after_final_gate: Literal[True]


class PublicationConfig(BaseModel):
    """External write authority granted by exact graph acceptance."""

    model_config = ConfigDict(extra="forbid")

    authorized_by: Literal["graph_acceptance"]
    scope: Literal["accepted_run"]
    branch: BranchPublicationConfig
    pull_request: PullRequestPublicationConfig
    merge: Literal[False]
    deploy: Literal[False]


class FinalGateFailureConfig(BaseModel):
    """Bounded conversion of CI failures into scoped findings."""

    model_config = ConfigDict(extra="forbid")

    create_scoped_finding: Literal[True]
    scope_source: list[Literal["failing_check", "failure_output", "implicated_fix_commits"]] = (
        Field(min_length=1, max_length=3)
    )
    max_fix_rounds: int = Field(ge=1, le=2)
    scope_escape: Literal["stop_and_escalate"]


class FinalGateConfig(BaseModel):
    """Remote CI policy for the exact integrated lane revision."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["ci"]
    provider: Literal["auto", "github"]
    repository: Literal["lane_repository"]
    run_on: Literal["integrated_fix_set"]
    require_remote: Literal[True]
    require_all_checks: Literal[True]
    restart_initial_review: Literal[False]
    # Checks named here are observed and recorded but never gate the lane.
    # `require_all_checks` still means every check the repository actually
    # enforces; this is how a repository says which of its own checks it does
    # not. Without it the supervisor is stricter than the repository: kashh's
    # `all-checks` job excludes its Chrome conformance suite by name while that
    # suite's launch boundary is intermittent, and a lane whose every enforced
    # suite passed was blocked on the one check the repository had already
    # decided not to enforce. Exact names only - a substring rule would silently
    # widen the moment somebody names a real suite "advisory rollout".
    advisory_checks: list[str] = Field(default_factory=list, max_length=32)
    on_failure: FinalGateFailureConfig


class GraphConfig(BaseModel):
    """Declarative execution-graph configuration."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    max_parallel_lanes: int = Field(default=2, ge=1, le=32)
    max_parallel_workers: int = Field(default=6, ge=1, le=128)
    review_cycle: ReviewCycleConfig
    roles: RoleProfiles
    stage_budgets: StageBudgets = Field(default_factory=StageBudgets)
    publication: PublicationConfig
    final_gate: FinalGateConfig

    @model_validator(mode="after")
    def valid_concurrency_limits(self) -> GraphConfig:
        """Keep nested fixer fan-out within the global worker ceiling."""

        if self.review_cycle.parallel_fixers.max_per_lane > self.max_parallel_workers:
            raise ValueError("parallel_fixers.max_per_lane cannot exceed max_parallel_workers")
        return self


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for one supervisor process."""

    config_path: Path
    graph: GraphConfig
    database_path: Path
    github_command: tuple[str, ...]
    orca_command: tuple[str, ...]
    command_timeout_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from explicit environment variables."""

        timeout = _positive_float("ORKASTRATOR_COMMAND_TIMEOUT_SECONDS", default=30.0)
        config_path = Path(os.environ.get("ORKASTRATOR_CONFIG", "orkastrator.yaml")).expanduser()
        graph = load_graph_config(config_path)
        database_path = Path(
            os.environ.get(
                "ORKASTRATOR_DB_PATH",
                str(Path.home() / ".local" / "share" / "orkastrator" / "state.sqlite3"),
            )
        ).expanduser()
        return cls(
            config_path=config_path,
            graph=graph,
            database_path=database_path,
            github_command=_command("ORKASTRATOR_GITHUB_COMMAND", default=("gh",)),
            orca_command=_orca_command(),
            command_timeout_seconds=timeout,
        )


def load_graph_config(path: Path) -> GraphConfig:
    """Load and validate the explicit YAML execution configuration."""

    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"orkastrator config does not exist: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read orkastrator config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"orkastrator config must be a YAML mapping: {path}")
    try:
        return GraphConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid orkastrator config {path}: {exc}") from exc


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


def config_changes(before: object, after: object) -> list[ConfigChange]:
    """Every changed leaf between two dumped configurations, as dotted paths.

    Lists are compared whole rather than walked. A per-index diff of
    `advisory_checks` reads as three unrelated changes when what moved is one
    setting, and an owner authorizing a policy change is judging the setting.
    """

    changes: list[ConfigChange] = []
    _walk("", before, after, changes)
    return sorted(changes, key=lambda item: item.path)


def _walk(path: str, before: object, after: object, changes: list[ConfigChange]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}" if path else str(key)
            _walk(child, before.get(key, _ABSENT), after.get(key, _ABSENT), changes)
        return
    if before == after:
        return
    changes.append(ConfigChange(path=path or ".", before=_render(before), after=_render(after)))


def _render(value: object) -> str:
    if value is _ABSENT:
        return "(unset)"
    try:
        return json.dumps(value, sort_keys=True)[:2_048]
    except TypeError:
        return str(value)[:2_048]


class _Absent:
    """A key present on one side only, which is not the same as a null."""

    def __repr__(self) -> str:
        return "(unset)"


_ABSENT = _Absent()
