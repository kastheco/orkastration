"""YAML and process configuration tests."""

from pathlib import Path

import pytest

from orkastrator.config import ConfigError, Settings, config_changes, load_graph_config


def write_config(path: Path, *, strength: str = "high") -> None:
    path.write_text(
        f"""
version: 2
max_parallel_lanes: 2
max_parallel_workers: 4
review_cycle:
  initial_scope: lane_changeset
  freeze_findings_after_initial_review: true
  max_fix_rounds_per_finding: 2
  re_review_scope: [finding_contract, relevant_original_context, fixer_diff, validation_evidence]
  new_findings_during_re_review:
    introduced_by_fix: accept
    otherwise: defer_to_next_review_run
  scope:
    escape: stop_and_escalate
    reject_out_of_scope_diff: true
    required_boundary: paths
    symbols: enforce_when_adapter_available
  parallel_fixers:
    max_per_lane: 2
    workspace: isolated_from_review_revision
    require_disjoint_write_scopes: true
    on_overlap: serialize
    integration: serial
  escalation: {{agent: codex, model: gpt-escalation, strength: high}}
roles:
  worker: {{agent: codex, model: gpt-test, strength: {strength}, fast: true}}
  initial_reviewer: {{agent: codex, model: gpt-test, strength: high}}
  fixer:
    agent: codex
    model: gpt-test
    strength: high
    fallback:
      agent: codex
      model: gpt-fallback
      strength: high
      trigger: capability_mismatch
  re_reviewer: {{agent: codex, model: gpt-test, strength: xhigh}}
publication:
  authorized_by: graph_acceptance
  scope: accepted_run
  branch: {{create: true, push: true, force_push: false}}
  pull_request: {{create_or_update: true, initial_state: draft, mark_ready_after_final_gate: true}}
  merge: false
  deploy: false
final_gate:
  type: ci
  provider: auto
  repository: lane_repository
  run_on: integrated_fix_set
  require_remote: true
  require_all_checks: true
  restart_initial_review: false
  on_failure:
    create_scoped_finding: true
    scope_source: [failing_check, failure_output, implicated_fix_commits]
    max_fix_rounds: 2
    scope_escape: stop_and_escalate
""".strip()
    )


def test_settings_load_yaml_and_explicit_orca_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "graph.yaml"
    write_config(config_path)
    monkeypatch.setenv("ORKASTRATOR_CONFIG", str(config_path))
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-dev --profile test")
    monkeypatch.setenv("ORKASTRATOR_GITHUB_COMMAND", "gh-dev --profile test")
    monkeypatch.setenv("ORKASTRATOR_DB_PATH", str(tmp_path / "state.sqlite3"))

    settings = Settings.from_env()

    assert settings.orca_command == ("orca-dev", "--profile", "test")
    assert settings.github_command == ("gh-dev", "--profile", "test")
    assert settings.graph.max_parallel_workers == 4
    assert settings.graph.roles.worker.model == "gpt-test"
    assert settings.graph.roles.worker.fast is True
    assert settings.graph.roles.fixer.fallback is not None
    assert settings.graph.roles.fixer.fallback.model == "gpt-fallback"
    assert settings.graph.roles.initial_reviewer.fast is False
    assert settings.graph.roles.re_reviewer.strength == "xhigh"
    assert settings.graph.review_cycle.max_fix_rounds_per_finding == 2
    assert settings.graph.frozen_diff_budget_bytes == 65_536
    assert settings.graph.publication.branch.force_push is False
    assert settings.graph.final_gate.require_all_checks is True
    assert settings.database_path == tmp_path / "state.sqlite3"


def test_missing_config_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_graph_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize("contents", ["[]", "version: 1", "version: ["])
def test_invalid_config_is_rejected(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(contents)
    with pytest.raises(ConfigError):
        load_graph_config(path)


def test_current_repository_config_is_valid() -> None:
    config = load_graph_config(Path("orkastrator.yaml"))
    assert config.version == 2
    assert config.roles.worker.model == "gpt-5.6-sol"


def test_parallel_fixer_limit_cannot_exceed_global_limit(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    write_config(path)
    path.write_text(path.read_text().replace("max_parallel_workers: 4", "max_parallel_workers: 1"))
    with pytest.raises(ConfigError, match="max_per_lane"):
        load_graph_config(path)


@pytest.mark.parametrize("value", ["0", "nope"])
def test_invalid_timeout_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
    config_path = tmp_path / "graph.yaml"
    write_config(config_path)
    monkeypatch.setenv("ORKASTRATOR_CONFIG", str(config_path))
    monkeypatch.setenv("ORKASTRATOR_COMMAND_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigError, match="TIMEOUT"):
        Settings.from_env()


def test_stage_budgets_default_to_unset(tmp_path: Path) -> None:
    """A budget is a claim about how long this repository's work takes.

    No default can know that, so an unconfigured budget means what it meant
    before budgets existed: wait.
    """

    path = tmp_path / "graph.yaml"
    write_config(path)
    budgets = load_graph_config(path).stage_budgets
    assert budgets.for_role("worker").soft_minutes is None
    assert budgets.for_role("worker").hard_minutes is None
    assert budgets.max_timeouts == 2


def test_frozen_diff_budget_is_configurable(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    write_config(path)
    path.write_text(path.read_text() + "\nfrozen_diff_budget_bytes: 1024\n")

    assert load_graph_config(path).frozen_diff_budget_bytes == 1024


def test_a_hard_budget_before_its_soft_budget_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    write_config(path)
    path.write_text(
        path.read_text() + "\nstage_budgets:\n  worker: {soft_minutes: 90, hard_minutes: 45}\n"
    )
    with pytest.raises(ConfigError, match="hard_minutes"):
        load_graph_config(path)


def test_config_changes_reads_nested_leaves_and_leaves_lists_whole() -> None:
    """One setting that moved should read as one change, at a path you can look up."""

    before = {
        "max_parallel_workers": 4,
        "final_gate": {"advisory_checks": [], "required": True},
        "roles": {"worker": {"model": "gpt-test"}},
    }
    after = {
        "max_parallel_workers": 4,
        "final_gate": {"advisory_checks": ["conformance", "chrome"], "required": True},
        "roles": {"worker": {"model": "gpt-test", "fast": True}},
    }

    changes = config_changes(before, after)

    assert [(item.path, item.before, item.after) for item in changes] == [
        ("final_gate.advisory_checks", "[]", '["conformance", "chrome"]'),
        ("roles.worker.fast", "(unset)", "true"),
    ]


def test_config_changes_distinguishes_an_absent_key_from_a_null_one() -> None:
    """`(unset)` and `null` are different facts, and a budget of null is a real setting."""

    changes = config_changes({"budget": None}, {})
    assert [(item.path, item.before, item.after) for item in changes] == [
        ("budget", "null", "(unset)")
    ]
    assert config_changes({"a": 1}, {"a": 1}) == []
