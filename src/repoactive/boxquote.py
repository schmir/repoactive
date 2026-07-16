"""Render and strip boxquote.el-style boxes used to embed command output in messages."""

import re

# A boxquote block: a ``,----`` opening line through its closing ``` `---- ``` line.
# DOTALL lets ``.*?`` span the body lines; MULTILINE anchors both delimiters to a
# line of their own. Non-greedy so each block stops at its own closing line.
_BOXQUOTE_BLOCK = re.compile(r"^,----.*?^`----[ \t]*$", re.MULTILINE | re.DOTALL)


def boxquote(msg: str, title: str = "") -> str:
    """Render ``msg`` inside a boxquote.el-style box.

    The box opens with ``,----[ title ]`` (or just ``,----`` when ``title`` is
    empty), each line of ``msg`` is prefixed with ``| ``, and the box closes
    with ``` `---- ```.

    A multi-line ``title`` (e.g. a multi-line job command) is bracketed across
    several lines instead of jammed onto the opening line: the first title line
    follows ``,----[ ``, the remaining lines are ``|``-prefixed and indented to
    align under that first line, the closing ``]`` trails the last of them, and
    a blank ``|`` line then separates the bracketed title from ``msg``. A
    trailing newline on ``title`` is dropped so it does not produce an empty
    final title line.
    """
    body = "\n".join(f"| {line}" for line in msg.splitlines())
    title = title.rstrip("\n")
    if "\n" not in title:
        top = f",----[ {title} ]" if title else ",----"
        return f"{top}\n{body}\n`----"
    open_bracket = ",----[ "
    # Indent continuation lines so the title text aligns under the first line;
    # the "|" replaces the leading "," so the bar still runs down the left edge.
    indent = "|" + " " * (len(open_bracket) - 1)
    head, *rest = title.split("\n")
    header_lines = [f"{open_bracket}{head}", *(f"{indent}{line}" for line in rest[:-1])]
    header_lines.append(f"{indent}{rest[-1]} ]")
    header = "\n".join(header_lines)
    return f"{header}\n|\n{body}\n`----"


def strip_boxquotes(text: str) -> str:
    """Remove every boxquote block (see :func:`boxquote`) from ``text``.

    A block runs from a ``,----`` opening line to its closing ``` `---- ``` line;
    the surrounding text is kept. Blank lines exposed by a removed block are
    collapsed so paragraphs stay separated by a single blank line, and leading
    and trailing blank lines are trimmed.
    """
    without = _BOXQUOTE_BLOCK.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", without).strip("\n")
