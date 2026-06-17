# 3. Refresh unmerged branches in the default run

Status: Accepted

## Context

[ADR 0002](0002-tag-based-job-selection.md) introduced tags so a job can be
scheduled on a cadence (e.g. `tags = ["weekly"]`, run from cron via
`--tag weekly`). That cadence governs when the job runs — but a job that ran
last Sunday may have left a branch that is still unmerged days later, while
`trunk()` has moved on and possibly now conflicts with it. Waiting until
next Sunday to rebase the branch is too slow.

The machinery to fix this already exists: when a job's bookmark exists,
`run_job` rebases it onto the current parents (`trunk()`) and re-runs the
command, regenerating the diff against the latest `trunk()`. What was
missing is purely _selection_ — a weekly job is not in the daily default
run, so that refresh never fires between weekly runs.

## Decision

The bare `repoactive run` (implicit, no names/tags) additionally selects
**every job that currently has an unmerged branch**, regardless of its tags,
and force-includes their dependencies. "Unmerged" means the job's last
commit has not landed in `trunk()`; it is detected from the `Repoactive-Job`
trailer on unmerged commits (`~(::trunk())`, the same signal as
`recent-commits --unmerged`), unbounded in time. (With `--create-prs` such a
branch has an open MR, but the branch — not a merge request — is what
repoactive detects, so this works for a plain `run`/`--push` too.) Explicit
selection (`--tag`/named jobs) does **not** sweep up unrelated unmerged
branches; it refreshes only its own selected jobs.

Unmerged branches are refreshed **even for `disabled` jobs**: such a branch
was most likely created by an explicit `repoactive run my-job`, and letting
it drift out of date with `trunk()` serves no one. The consequence —
`disabled` no longer guarantees repoactive never pushes to that branch while
it is unmerged — is acceptable; truly stopping it means landing or deleting
the branch.

## Consequences

- A schedule tag governs when a _new_ branch is created; the default run
  keeps an existing branch rebased and current. No waiting for the next
  scheduled run to resolve a `trunk()` conflict.
- Self-correcting lifecycle: once the branch lands, its commit is an
  ancestor of `trunk()`, so it is no longer unmerged and the job drops back
  to its normal tag-driven cadence. If a refresh run produces no diff (trunk
  already has the change), the existing empty-handling deletes the branch
  (closing the MR if one was open).
- Cooldown is unaffected: it keys on _landed_ commits, and an unmerged
  branch has by definition not landed, so the two never collide in practice.
- The default run issues one extra `jj log` to enumerate unmerged repoactive
  commits — cheap, and skipped entirely for explicit selection.
