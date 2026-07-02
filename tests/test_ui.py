"""Tests for the undo-hint panel and its REPOACTIVE_UI switch."""

import pytest

from repoactive.ui import print_undo_hint


def _print_hint(capsys: pytest.CaptureFixture[str]) -> str:
    print_undo_hint(
        title="To undo", body="Run this:", command="jj op restore abc123", style="cyan"
    )
    return capsys.readouterr().out


def test_panel_printed_by_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOACTIVE_UI", raising=False)
    assert "jj op restore abc123" in _print_hint(capsys)


def test_noninteractive_suppresses_output(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACTIVE_UI", "noninteractive")
    assert _print_hint(capsys) == ""


def test_value_is_case_insensitive(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACTIVE_UI", "NONINTERACTIVE")
    assert _print_hint(capsys) == ""


def test_interactive_prints(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACTIVE_UI", "interactive")
    assert "jj op restore abc123" in _print_hint(capsys)
