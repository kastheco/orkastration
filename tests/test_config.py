"""YAML and process configuration tests."""

from pathlib import Path

import pytest

from kasgraph.config import ConfigError, Settings, load_graph_config


def write_config(path: Path, *, strength: str = "high") -> None:
    path.write_text(
        f"""
version: 1
max_parallel_lanes: 2
supervisor: {{agent: codex, model: gpt-supervisor, strength: high}}
roles:
  worker: {{agent: codex, model: gpt-test, strength: {strength}, fast: true}}
  initial_reviewer: {{agent: codex, model: gpt-test, strength: high}}
  fixer: {{agent: codex, model: gpt-test, strength: high}}
  re_reviewer: {{agent: codex, model: gpt-test, strength: xhigh}}
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
    assert settings.graph.supervisor.model == "gpt-supervisor"
    assert settings.graph.supervisor.fast is False
    assert settings.graph.roles.worker.model == "gpt-test"
    assert settings.graph.roles.worker.fast is True
    assert settings.graph.roles.initial_reviewer.fast is False
    assert settings.graph.roles.re_reviewer.strength == "xhigh"
    assert settings.database_path == tmp_path / "state.sqlite3"


def test_missing_config_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_graph_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize("contents", ["[]", "version: 2", "version: ["])
def test_invalid_config_is_rejected(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(contents)
    with pytest.raises(ConfigError):
        load_graph_config(path)


@pytest.mark.parametrize("value", ["0", "nope"])
def test_invalid_timeout_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
    config_path = tmp_path / "graph.yaml"
    write_config(config_path)
    monkeypatch.setenv("KASGRAPH_CONFIG", str(config_path))
    monkeypatch.setenv("KASGRAPH_COMMAND_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigError, match="TIMEOUT"):
        Settings.from_env()
