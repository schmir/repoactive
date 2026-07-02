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
