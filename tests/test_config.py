"""Configuration tests."""

from pathlib import Path

import pytest

from kasgraph.config import ConfigError, Settings


def test_settings_use_explicit_orca_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-dev --profile test")
    monkeypatch.setenv("KASGRAPH_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("KASGRAPH_MODEL", "test")

    settings = Settings.from_env()

    assert settings.orca_command == ("orca-dev", "--profile", "test")
    assert settings.database_path == tmp_path / "state.sqlite3"
    assert settings.require_model() == "test"


def test_require_model_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KASGRAPH_MODEL", raising=False)
    with pytest.raises(ConfigError, match="KASGRAPH_MODEL"):
        Settings.from_env().require_model()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KASGRAPH_MAX_PARALLEL_LANES", "0"),
        ("KASGRAPH_MAX_PARALLEL_LANES", "nope"),
        ("KASGRAPH_COMMAND_TIMEOUT_SECONDS", "0"),
        ("KASGRAPH_COMMAND_TIMEOUT_SECONDS", "nope"),
    ],
)
def test_invalid_numeric_settings_fail(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Settings.from_env()
