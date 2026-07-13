"""Tests for the progress/status display."""

import io
from typing import cast

import pytest
from rich.console import Console

from repoactive.progress import ProgressView, format_duration, format_elapsed


def _terminal_console(width: int = 40) -> Console:
    # force_terminal makes is_terminal True so the live block renders into buf.
    return Console(file=io.StringIO(), force_terminal=True, width=width)


def _plain_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False)


def _written(console: Console) -> str:
    # The helpers above back every Console with a StringIO.
    return cast(io.StringIO, console.file).getvalue()


def _view(
    *,
    max_lines: int = 3,
    timeout: float | None = None,
    idle_after: float = 30.0,
    console: Console,
) -> ProgressView:
    return ProgressView(
        name="myjob",
        command="echo hi",
        max_lines=max_lines,
        timeout=timeout,
        idle_after=idle_after,
        console=console,
    )


class TestProgressView:
    def test_keeps_only_last_n_lines(self) -> None:
        with _view(console=_terminal_console()) as view:
            for i in range(5):
                view.feed(f"line {i}\n")
        assert view.tail() == ["line 2", "line 3", "line 4"]

    def test_strips_trailing_newlines_in_tail(self) -> None:
        view = _view(console=_plain_console())
        view.feed("only\n")
        assert view.tail() == ["only"]

    def test_enabled_on_a_terminal(self) -> None:
        view = _view(console=_terminal_console())
        assert view.enabled is True

    def test_disabled_when_not_a_terminal(self) -> None:
        console = _plain_console()
        view = _view(console=console)
        assert view.enabled is False
        with view:
            view.feed("a\n")
            view.feed("b\n")
        # Nothing is drawn, but the tail is still tracked.
        assert _written(console) == ""
        assert view.tail() == ["a", "b"]

    def test_disabled_when_zero_lines(self) -> None:
        view = _view(max_lines=0, console=_terminal_console())
        assert view.enabled is False

    def test_renders_header_and_lines_on_a_terminal(self) -> None:
        # Wide enough that the no-wrap header is not truncated.
        console = _terminal_console(width=80)
        with _view(console=console) as view:
            view.feed("hello world\n")
        out = _written(console)
        # The header segments are styled individually, so the ANSI codes between
        # them break up the line - assert each segment on its own.
        for segment in ("==> ", "[myjob]", "[0s]", "echo hi"):
            assert segment in out
        assert "hello world" in out

    def test_renders_elapsed_and_timeout(self) -> None:
        console = _terminal_console(width=80)
        with _view(timeout=120, console=console) as view:
            view.feed("hello\n")
        out = _written(console)
        assert "[0s/2m]" in out

    def test_clock_turns_red_near_timeout(self) -> None:
        console = _terminal_console(width=80)
        # A near-zero timeout puts the very first render past the 80% mark.
        with _view(timeout=1e-9, console=console) as view:
            view.feed("hello\n")
        # bold red
        assert "\x1b[1;31m" in _written(console)

    def test_clock_yellow_before_warn_threshold(self) -> None:
        console = _terminal_console(width=80)
        with _view(timeout=120, console=console) as view:
            view.feed("hello\n")
        out = _written(console)
        assert "\x1b[33m" in out  # yellow
        assert "\x1b[1;31m" not in out

    def test_idle_indicator_after_threshold(self) -> None:
        console = _terminal_console(width=80)
        # idle_after=0 makes the indicator show on the very next render.
        with _view(idle_after=0.0, console=console) as view:
            view.feed("hello\n")
        out = _written(console)
        assert "no output for" in out

    def test_no_idle_indicator_before_threshold(self) -> None:
        console = _terminal_console(width=80)
        with _view(console=console) as view:
            view.feed("hello\n")
        out = _written(console)
        assert "no output for" not in out


class TestFormatElapsed:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "0.0s"),
            (4.25, "4.2s"),
            (59.94, "59.9s"),
            (61.0, "1m 1s"),
            (192.4, "3m 12s"),
        ],
    )
    def test_format(self, seconds: float, expected: str) -> None:
        assert format_elapsed(seconds) == expected


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (12, "12s"),
            (60, "1m"),
            (61, "1m 1s"),
            (120, "2m"),
            (3600, "1h"),
            (3723, "1h 2m 3s"),
            (3603, "1h 3s"),
        ],
    )
    def test_format(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected
