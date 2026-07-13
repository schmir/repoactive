"""A live tail of a running command's output.

While a job command runs, ``_run_command`` streams its output line by line into a
``ProgressView``. The view keeps only the last few lines and renders them as a
small, fixed-height block that scrolls in place — so a long command shows live
progress without flooding the terminal. The header line shows the job name, a
ticking elapsed clock (with the job's timeout), and the command:
``==> [<name>] [<elapsed>/<timeout>] <command>``. When the command finishes
the block is left in place (its final lines stay on screen), with the status
line printed below it.

Rendering is delegated to ``rich.live.Live``. Rich handles terminal-width
truncation and only draws when stdout is a real terminal, so piped/CI output is
left untouched.
"""

import time
from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

# Once elapsed passes this fraction of the timeout, the header clock turns red
# to warn that the watchdog is about to kill the command.
_TIMEOUT_WARN_FRACTION = 0.8


def format_duration(seconds: float) -> str:
    """Format a duration as a compact string like '12s', '2m' or '1h 2m 3s'.

    Zero components are omitted (except a bare '0s'), so a round timeout like
    120s renders as '2m'. Second-level granularity, unlike the coarser
    ``runner._format_duration`` used for cooldown ages.
    """
    seconds = int(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_elapsed(seconds: float) -> str:
    """Format an elapsed wall time.

    Sub-second precision under a minute ('4.2s'), ``format_duration``
    granularity above ('3m 12s').
    """
    if seconds < 60:  # noqa: PLR2004
        return f"{seconds:.1f}s"
    return format_duration(seconds)


class ProgressView:
    """A live tail of the last ``max_lines`` output lines.

    Use as a context manager around a streaming read loop, calling ``feed`` for
    each line. The header line is ``==> [<name>] [<elapsed>] <command>``, with
    the elapsed time ticking (measured from ``__enter__``) and, when ``timeout``
    is given, shown as ``[<elapsed>/<timeout>]``. On exit the block's final
    lines are left on screen (non-transient Live). When disabled —
    ``max_lines <= 0`` or stdout is not a terminal — nothing is drawn, but
    ``feed`` still tracks the most recent lines, queryable via ``tail``.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        name: str,
        command: str,
        max_lines: int,
        timeout: float | None = None,
        idle_after: float = 30.0,
        console: Console | None = None,
    ) -> None:
        self._name = name
        self._command = command
        self._timeout = timeout
        # A silent command is indistinguishable from a hung one; once no line
        # arrived for idle_after seconds the header says so.
        self._idle_after = idle_after
        self._console = console or Console()
        self._tail_lines: deque[str] = deque(maxlen=max(max_lines, 0))
        self._start = time.monotonic()
        self._last_line = self._start
        # Only drive Live when there is something to show and somewhere to show it.
        self.enabled = max_lines > 0 and self._console.is_terminal
        # get_renderable (rather than a fixed renderable) so Live's auto-refresh
        # thread re-renders the elapsed clock even while the command is silent.
        # transient=False leaves the final block on screen when the Live stops.
        self._live = Live(
            get_renderable=self._render,
            console=self._console,
            transient=False,
            refresh_per_second=12,
        )

    def __enter__(self) -> "ProgressView":
        self._start = time.monotonic()
        self._last_line = self._start
        if self.enabled:
            self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.enabled:
            self._live.stop()

    def feed(self, line: str) -> None:
        """Record one output line; Live's auto-refresh picks it up on the next draw."""
        self._last_line = time.monotonic()
        self._tail_lines.append(line.rstrip("\n"))

    def tail(self) -> list[str]:
        """Return the most recent lines kept (the last ``max_lines`` fed)."""
        return list(self._tail_lines)

    def _render(self) -> Group:
        now = time.monotonic()
        elapsed = now - self._start
        clock = format_duration(elapsed)
        clock_style = "yellow"
        if self._timeout is not None:
            clock += f"/{format_duration(self._timeout)}"
            if elapsed >= _TIMEOUT_WARN_FRACTION * self._timeout:
                clock_style = "bold red"
        # cyan job name and bold command match jobtree/ui styling elsewhere.
        header = Text.assemble(
            "==> ",
            (f"[{self._name}]", "cyan"),
            " ",
            (f"[{clock}]", clock_style),
            " ",
            (self._command, "bold"),
            no_wrap=True,
            overflow="ellipsis",
        )
        idle = now - self._last_line
        if idle >= self._idle_after:
            header.append(f" (no output for {format_duration(idle)})", style="dim")
        # Snapshot the deque: _render runs on Live's refresh thread while feed
        # appends from the reader thread, and iterating during a mutation raises.
        body = [
            Text(f"  | {line}", style="dim", no_wrap=True, overflow="ellipsis")
            for line in list(self._tail_lines)
        ]
        return Group(header, *body)
