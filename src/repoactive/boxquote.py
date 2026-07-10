"""Render and strip boxquote.el-style boxes used to embed command output in messages."""

import re

# A boxquote block: a ``,----`` opening line through its closing ``` `---- ``` line.
# DOTALL lets ``.*?`` span the body lines; MULTILINE anchors both delimiters to a
# line of their own. Non-greedy so each block stops at its own closing line.
_BOXQUOTE_BLOCK = re.compile(r"^,----.*?^`----[ \t]*$", re.MULTILINE | re.DOTALL)


def boxquote(msg: str, title: str = "") -> str:
    """Render ``msg`` inside a boxquote.el-style box.

    The first line is ``,----[ title ]`` (or just ``,----`` when ``title`` is
    empty), each line of ``msg`` is prefixed with ``| ``, and the box closes
    with ``` `---- ```.
    """
    top = f",----[ {title} ]" if title else ",----"
    body = "\n".join(f"| {line}" for line in msg.splitlines())
    return f"{top}\n{body}\n`----"


def strip_boxquotes(text: str) -> str:
    """Remove every boxquote block (see :func:`boxquote`) from ``text``.

    A block runs from a ``,----`` opening line to its closing ``` `---- ``` line;
    the surrounding text is kept. Blank lines exposed by a removed block are
    collapsed so paragraphs stay separated by a single blank line, and leading
    and trailing blank lines are trimmed.
    """
    without = _BOXQUOTE_BLOCK.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", without).strip("\n")
