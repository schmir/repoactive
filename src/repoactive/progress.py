"""A live tail of a running command's output.

While a job command runs, ``_run_command`` streams its output line by line into a
``ProgressView``. The view keeps only the last few lines and renders them as a
small, fixed-height block that scrolls in place — so a long command shows live
progress without flooding the terminal. When the command finishes the block is
left in place (its final lines stay on screen), with the status line printed
below it.

Rendering is delegated to ``rich.live.Live``. Rich handles terminal-width
truncation and only draws when stdout is a real terminal, so piped/CI output is
left untouched.
"""

import os
from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

# Number of output lines shown in the live block. Overridable so a user who wants
# more (or less) context can tune it; <= 0 disables the live block entirely.
PROGRESS_LINES_ENV = "REPOACTIVE_PROGRESS_LINES"
DEFAULT_PROGRESS_LINES = 8


def progress_line_count() -> int:
    """How many output lines to show, from ``REPOACTIVE_PROGRESS_LINES``.

    Defaults to ``DEFAULT_PROGRESS_LINES`` and falls back to it on a non-integer
    value. A value <= 0 is returned as-is and disables the live block.
    """
    raw = os.environ.get(PROGRESS_LINES_ENV)
    if raw is None:
        return DEFAULT_PROGRESS_LINES
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PROGRESS_LINES


class ProgressView:
    """A live tail of the last ``max_lines`` output lines.

    Use as a context manager around a streaming read loop, calling ``feed`` for
    each line. On exit the block's final lines are left on screen (non-transient
    Live). When disabled — ``max_lines <= 0`` or stdout is not a terminal — nothing
    is drawn, but ``feed`` still tracks the most recent lines, queryable via
    ``tail``.
    """

    def __init__(self, *, header: str, max_lines: int, console: Console | None = None) -> None:
        self._header = header
        self._console = console or Console()
        self._tail_lines: deque[str] = deque(maxlen=max(max_lines, 0))
        # Only drive Live when there is something to show and somewhere to show it.
        self.enabled = max_lines > 0 and self._console.is_terminal
        # transient=False leaves the final block on screen when the Live stops.
        self._live = Live(console=self._console, transient=False, refresh_per_second=12)

    def __enter__(self) -> "ProgressView":
        if self.enabled:
            self._live.start()
            self._live.update(self._render())
        return self

    def __exit__(self, *exc: object) -> None:
        if self.enabled:
            self._live.stop()

    def feed(self, line: str) -> None:
        """Record one output line and refresh the live block (if drawing)."""
        self._tail_lines.append(line.rstrip("\n"))
        if self.enabled:
            self._live.update(self._render())

    def tail(self) -> list[str]:
        """The most recent lines kept (the last ``max_lines`` fed)."""
        return list(self._tail_lines)

    def _render(self) -> Group:
        body = [
            Text(f"  | {line}", style="dim", no_wrap=True, overflow="ellipsis")
            for line in self._tail_lines
        ]
        return Group(Text(self._header), *body)
