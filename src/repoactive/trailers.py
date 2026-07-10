"""Detect and strip the git trailer block from a commit message."""

import re

# A trailer line: a token with no whitespace, followed by ``:`` and then a space
# (``Repoactive-Job: name``) or nothing (an empty value). Requiring the space (or
# end of line) keeps prose and URLs (``see http://x``) from matching.
_TRAILER_LINE = re.compile(r"^\S+:( .*)?$")
# A folded continuation of the preceding trailer: an RFC-822-style indented line.
_CONTINUATION_LINE = re.compile(r"^\s")


def _is_trailer_block(paragraph: str) -> bool:
    """Return whether ``paragraph`` is entirely trailers (plus folded continuations)."""
    saw_trailer = False
    for line in paragraph.splitlines():
        if _TRAILER_LINE.match(line):
            saw_trailer = True
        elif not (saw_trailer and _CONTINUATION_LINE.match(line)):
            return False
    return saw_trailer


def strip_trailers(message: str) -> str:
    """Remove the trailing git trailer block from ``message``.

    Following git's model, the final paragraph is treated as trailers when it is
    separated from the body by a blank line and every line is a ``token: value``
    trailer (or a folded continuation of one). When it is, that paragraph and the
    blank line before it are dropped; otherwise ``message`` is returned unchanged.
    A message that is only a subject line is never treated as trailers.
    """
    stripped = message.rstrip("\n")
    body, sep, last = stripped.rpartition("\n\n")
    if sep and _is_trailer_block(last):
        return body.rstrip("\n")
    return stripped
