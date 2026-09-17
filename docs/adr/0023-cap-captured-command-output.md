# 23. Cap the captured command output

Status: Accepted

## Context

A job's command output is captured in full and lands in two places: the
commit message, rendered as a boxquote (`output_in_commit`), and the MR
description, rendered as a fenced code block. Nothing bounded it.

A job whose output ran into the hundreds of KiB broke the run with
`jj: argument list too long`: the commit message was passed to
`jj describe --message`, and Linux caps a single `argv` entry at 128 KiB.
Passing the message on stdin lifts that particular ceiling, but two problems
survive it:

- A commit description of several hundred KiB is unreadable. The reason the
  output is in the commit at all is so a reviewer can see what the command
  did, and a wall of text does not serve that.
- GitHub rejects a pull-request body over 65536 characters, so a large
  output fails MR creation there no matter what jj accepts.

The two sinks could each cap what they render, but that means two bounds to
keep in step, and the uncapped text would still be carried around between
them.

## Decision

Cap the output once, where it is captured: `run_command` stores at most
`MAX_OUTPUT_CHARS` (32 KiB) in the `CommandResult`, so the commit message
and the MR description both get the capped text and neither needs to know
about the limit. 32 KiB is roughly 400 lines - enough to show what a command
did, comfortably inside GitHub's body limit even with the rest of the
description around it.

What goes is the **middle**, not the tail: a command's first lines say what
it ran and its last lines say how it ended, so those are the parts worth
keeping. 30% of the budget is taken from the start and 70% from the end, cut
on line boundaries, with a marker line naming how many characters were
dropped.

The cap applies to the success path only. A command that fails is still
reported with its complete output, because a failure is diagnosed from all
of it and that text goes to the terminal, not into a commit or an MR.

## Consequences

- The full output of a successful command exists only while it runs; only
  the capped text is kept. A job that needs its complete output preserved
  must write it to a file in the repository, which is a diff like any other.
- The cap is a constant, not configuration. A per-job knob can be added
  later if a real job needs one; until then there is nothing to configure,
  document, or merge.
- The commit message is no longer a verbatim copy of the output, which
  [ADR 0017](0017-secret-env-redaction.md) assumed when reasoning about a
  command that prints a secret. Truncation is not a redaction mechanism and
  must not be relied on as one: a secret printed in the first or last lines
  is still embedded.
