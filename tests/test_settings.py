"""Tests for reading REPOACTIVE_* environment settings."""

import pytest

from repoactive.settings import SettingsError, load_settings


def test_defaults_with_clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPOACTIVE_UI", raising=False)
    assert load_settings().ui == "interactive"


def test_ui_value_is_lowercased(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACTIVE_UI", "NonInteractive")
    assert load_settings().ui == "noninteractive"


def test_invalid_ui_value_raises_naming_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACTIVE_UI", "bogus")
    with pytest.raises(SettingsError, match="REPOACTIVE_UI"):
        load_settings()


def test_log_level_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPOACTIVE_LOG_LEVEL", raising=False)
    assert load_settings().log_level is None


def test_log_level_is_lowercased(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACTIVE_LOG_LEVEL", "INFO")
    assert load_settings().log_level == "info"


def test_invalid_log_level_raises_naming_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACTIVE_LOG_LEVEL", "verbose")
    with pytest.raises(SettingsError, match="REPOACTIVE_LOG_LEVEL"):
        load_settings()


def test_log_handler_defaults_to_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPOACTIVE_LOG_HANDLER", raising=False)
    monkeypatch.delenv("REPOACTIVE_UI", raising=False)
    assert load_settings().log_handler == "rich"


def test_log_handler_defaults_to_plain_when_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REPOACTIVE_LOG_HANDLER", raising=False)
    monkeypatch.setenv("REPOACTIVE_UI", "noninteractive")
    assert load_settings().log_handler == "plain"


def test_explicit_log_handler_wins_over_noninteractive_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPOACTIVE_LOG_HANDLER", "rich")
    monkeypatch.setenv("REPOACTIVE_UI", "noninteractive")
    assert load_settings().log_handler == "rich"


def test_log_handler_is_lowercased(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACTIVE_LOG_HANDLER", "Plain")
    assert load_settings().log_handler == "plain"


def test_invalid_log_handler_raises_naming_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACTIVE_LOG_HANDLER", "syslog")
    with pytest.raises(SettingsError, match="REPOACTIVE_LOG_HANDLER"):
        load_settings()
