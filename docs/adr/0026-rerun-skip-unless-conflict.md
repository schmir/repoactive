# 26. The skip-unless-conflict rerun mode

Status: Accepted (extends [ADR 0025](0025-incremental-jobs.md))

## Context

Some commands are expensive or not reproducible, and their output only needs
to follow the branch as its parents move. Running them again on every
repoactive run is wasted work, and when nothing conflicts there is nothing
for them to update.

## Decision

`rerun = "skip-unless-conflict"` rebases an existing command commit onto the
run's parents like the other modes, but does not run the command and keeps
the commit's content and message untouched. If the rebase leaves the command
commit conflicted, the command runs after all, on an emptied tree as for a
`fresh` job. A conflict that remains in the branch after the skipped run (a
fixup that no longer applies) freezes it as usual (ADR 0019).

## Consequences

- The job is reported as `rebased` when the rebase moved the command commit
  and as `unchanged` when it did not. In the latter case jj pushes nothing.
- If the rebase empties the command commit (trunk gained the same change),
  it is retired as for any rerun that produces nothing: the commit is
  abandoned and the bookmark deleted, unless human commits anchor the branch
  (ADR 0020).
- Changing the job's `command` or `title` in the configuration has no effect
  on an existing branch until a conflict makes the command run or the branch
  is removed, because the commit is kept as it is.
- The command output in the commit message is the one from the last real
  run.
- A job with no existing branch (new, or merged) has nothing to rebase, so
  the command runs.
- Dependents see the command commit as having a diff (`produced_diff`), as
  for an incremental rerun.
