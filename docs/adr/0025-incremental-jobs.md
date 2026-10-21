# 25. Incremental jobs keep the previous output

Status: Accepted (refines
[ADR 0020](0020-rewrite-command-commit-in-place.md))

## Context

[ADR 0020](0020-rewrite-command-commit-in-place.md) rewrites an existing
command commit in place and empties it with `jj restore` so the command
regenerates its output from a clean tree. Some commands are cumulative: they
extend or amend what they produced before and need that output to be
present.

## Decision

A job option `rerun` (default `"fresh"`, settable in `[job-defaults]` and
inherited by jobs like `shell`) selects how an existing command commit is
prepared. `"fresh"` is the ADR 0020 behaviour. `"incremental"` skips the
`jj restore` step: the run still rebases the command commit onto the run's
parents and runs `jj edit`, but the command then starts from the previously
generated content. If the rebase left the command commit conflicted, the
restore runs after all.

Everything else in ADR 0020 is unchanged: change-id preservation, op-log
rollback on failure, abandoning an empty result, and the idempotency skip.

## Consequences

- The command must tolerate its own earlier output, and must be idempotent
  if it is to avoid growing the diff on every run.
- If the rebase leaves the command commit conflicted, the restore runs
  anyway, as for a `fresh` job: the command must not be fed conflict
  markers. The previous output is discarded for that run.
- The command commit keeps its earlier output, so it has a diff even when a
  rerun adds nothing. A job gated on it with `run_only_if_changed` therefore
  still runs: "changed" means the command commit has a diff, not that this
  run changed something.
- A new job or a merged branch has no anchor, so the option has no effect
  there.
