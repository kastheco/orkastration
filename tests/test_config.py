"""YAML and process configuration tests."""

from pathlib import Path

import pytest

from kasgraph.config import ConfigError, Settings, load_graph_config


def write_config(path: Path, *, strength: str = "high") -> None:
    path.write_text(
        f"""
version: 2
max_parallel_lanes: 2
max_parallel_workers: 4
planner: {{agent: codex, model: gpt-planner, strength: high}}
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
    monkeypatch.setenv("KASGRAPH_CONFIG", str(config_path))
    monkeypatch.setenv("KASGRAPH_CLAUDE_COMMAND", "claude-dev --profile planner")
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-dev --profile test")
    monkeypatch.setenv("KASGRAPH_CODEX_COMMAND", "codex-dev --profile planner")
    monkeypatch.setenv("KASGRAPH_DB_PATH", str(tmp_path / "state.sqlite3"))

    settings = Settings.from_env()

    assert settings.orca_command == ("orca-dev", "--profile", "test")
    assert settings.claude_command == ("claude-dev", "--profile", "planner")
    assert settings.codex_command == ("codex-dev", "--profile", "planner")
    assert settings.graph.planner.model == "gpt-planner"
    assert settings.graph.planner.fast is False
    assert settings.graph.max_parallel_workers == 4
    assert settings.graph.roles.worker.model == "gpt-test"
    assert settings.graph.roles.worker.fast is True
    assert settings.graph.roles.fixer.fallback is not None
    assert settings.graph.roles.fixer.fallback.model == "gpt-fallback"
    assert settings.graph.roles.initial_reviewer.fast is False
    assert settings.graph.roles.re_reviewer.strength == "xhigh"
    assert settings.graph.review_cycle.max_fix_rounds_per_finding == 2
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
    config = load_graph_config(Path("kasgraph.yaml"))
    assert config.version == 2
    assert config.planner.model == "gpt-5.6-terra"


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
    monkeypatch.setenv("KASGRAPH_CONFIG", str(config_path))
    monkeypatch.setenv("KASGRAPH_COMMAND_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigError, match="TIMEOUT"):
        Settings.from_env()
