"""Shared rich console output for the user-facing "how to undo" hints.

repoactive prints a couple of hints that tell the user how to reverse something a
run did to their *local* repository (restore the jj operation log, or remove the
``.jj`` data added when colocating a plain git repo). These matter most right
after a run that may have scrolled a lot of output by, so they are rendered as a
bordered ``rich`` Panel to stand out.

The command is shown on its own line inside the panel, set off from the prose by
a blank line. ``expand=False`` keeps the box only as wide as its content, so the
command wraps only when it genuinely exceeds the terminal width.
"""

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from repoactive.settings import load_settings

# Lazily bound to sys.stdout/sys.stderr at print time, so test runners that
# redirect those streams still capture the output.
console = Console()
err_console = Console(stderr=True)


def print_status(name: str, *parts: str | tuple[str, str]) -> None:
    """Print a ``==> [<name>] ...`` status line with the job name in cyan.

    ``parts`` are ``Text.assemble`` segments: plain strings or (text, style)
    pairs, e.g. ``("committed", "green")``. Rendered as Text (no markup), so
    brackets in command output cannot be misparsed. soft_wrap keeps long lines
    (e.g. a failure carrying the full command output) unwrapped, matching plain
    ``print``; piped/CI output stays plain because rich emits no ANSI there.
    """
    console.print(Text.assemble("==> ", (f"[{name}]", "cyan"), " ", *parts), soft_wrap=True)


def print_undo_hint(*, title: str, body: str, command: str, style: str, err: bool = False) -> None:
    """Print an undo hint: a bordered panel holding the ``body`` prose and ``command``.

    ``title`` labels the panel border, ``style`` colours the border and command,
    and ``err`` routes the output to stderr (matching ``typer.secho(..., err=...)``).
    Suppressed entirely when ``REPOACTIVE_UI`` is set to ``noninteractive``.
    """
    if load_settings().ui == "noninteractive":
        return
    target = err_console if err else console
    content = Group(Text(body), Text(""), Text(command, style="bold"))
    target.print(Panel(content, title=title, title_align="left", border_style=style, expand=False))
