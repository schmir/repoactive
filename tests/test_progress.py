import io
from typing import cast

import pytest
from rich.console import Console

from repoactive.progress import DEFAULT_PROGRESS_LINES, ProgressView, progress_line_count


def _terminal_console() -> Console:
    # force_terminal makes is_terminal True so the live block renders into buf.
    return Console(file=io.StringIO(), force_terminal=True, width=40)


def _plain_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False)


def _written(console: Console) -> str:
    # The helpers above back every Console with a StringIO.
    return cast(io.StringIO, console.file).getvalue()


class TestProgressLines:
    def test_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REPOACTIVE_PROGRESS_LINES", raising=False)
        assert progress_line_count() == DEFAULT_PROGRESS_LINES

    def test_reads_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        override = 7
        monkeypatch.setenv("REPOACTIVE_PROGRESS_LINES", str(override))
        assert progress_line_count() == override

    def test_falls_back_on_non_integer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPOACTIVE_PROGRESS_LINES", "lots")
        assert progress_line_count() == DEFAULT_PROGRESS_LINES

    def test_non_positive_passes_through_to_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPOACTIVE_PROGRESS_LINES", "0")
        assert progress_line_count() == 0


class TestProgressView:
    def test_keeps_only_last_n_lines(self) -> None:
        with ProgressView(header="h", max_lines=3, console=_terminal_console()) as view:
            for i in range(5):
                view.feed(f"line {i}\n")
        assert view.tail() == ["line 2", "line 3", "line 4"]

    def test_strips_trailing_newlines_in_tail(self) -> None:
        view = ProgressView(header="h", max_lines=3, console=_plain_console())
        view.feed("only\n")
        assert view.tail() == ["only"]

    def test_enabled_on_a_terminal(self) -> None:
        view = ProgressView(header="h", max_lines=3, console=_terminal_console())
        assert view.enabled is True

    def test_disabled_when_not_a_terminal(self) -> None:
        console = _plain_console()
        view = ProgressView(header="h", max_lines=3, console=console)
        assert view.enabled is False
        with view:
            view.feed("a\n")
            view.feed("b\n")
        # Nothing is drawn, but the tail is still tracked.
        assert _written(console) == ""
        assert view.tail() == ["a", "b"]

    def test_disabled_when_zero_lines(self) -> None:
        view = ProgressView(header="h", max_lines=0, console=_terminal_console())
        assert view.enabled is False

    def test_renders_header_and_lines_on_a_terminal(self) -> None:
        console = _terminal_console()
        with ProgressView(header="==> [job] running", max_lines=3, console=console) as view:
            view.feed("hello world\n")
        out = _written(console)
        assert "running" in out
        assert "hello world" in out
