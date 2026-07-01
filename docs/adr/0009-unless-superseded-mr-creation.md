# 9. `create_mr = "unless-superseded"` collapses a dependency chain into one MR

Status: Accepted

## Context

A dependent job's change is stacked on its dependencies' branches
(`_compute_parents`), while every MR targets the job's `base_branch` (or the
platform default branch) — never the dependency's branch. A dependent's MR
diff therefore already contains all of its dependencies' changes.

For a chain `A ← B ← C` this means C's MR is the union A+B+C, and empty jobs
are transparent (an empty job passes its parents through as
`effective_revsets`, so C built on an empty B sits directly on A's branch).
What users often want from such a chain is **one** MR containing the union —
opened on the topmost job that actually produced a diff, falling back to the
job below when the ones above came up empty. Neither static `create_mr`
value can express that: `true` on every job opens an MR per non-empty job,
and `false` on all but the top job loses the fallback (an empty C means no
MR at all, even though B changed).

## Decision

`create_mr` accepts a third value, `"unless-superseded"`: the job's MR is
skipped whenever a dependent job's MR **created in the same run** already
contains this job's changes. The branch is still pushed either way.

Resolution happens once per run, after all jobs have run and before the plan
is applied (`_suppress_superseded_mrs`). Walking the results in reverse
topological order, each job is decided before its dependencies:

- a job whose MR survives (a plain `create_mr = true` job, or an
  `"unless-superseded"` job that nothing covers) _covers_ its dependencies;
- a covered job passes its cover down unchanged — including a job that
  recorded no MR itself (empty, `create_mr = false`, on cooldown), so cover
  flows through gaps in the chain;
- a covered `"unless-superseded"` job has its `MRUpdate` dropped from the
  plan, reported as `MR superseded by [<job>]`, naming the job whose MR was
  actually kept.

Only MRs recorded in this run's plan supersede. A dependent that is empty,
failed, on cooldown, or not selected in this run does not: it contributed no
MR that could carry this job's changes. `create_mr = true` is never
overridden — `"unless-superseded"` only ever suppresses the job it is set
on, keeping the reasoning local to each job's config.

The plan stays serializable and self-contained: suppression is resolved at
plan-build time (it needs the full dependency graph from the run's results,
which the plan alone does not carry), so applying a stored plan later needs
no re-derivation.

## Consequences

- A chain with `"unless-superseded"` on the lower jobs yields exactly one MR
  per run: the topmost non-empty job's, containing the union of all changes
  below it.
- Runs are decided independently (stateless, consistent with ADR 0001/0005).
  If C is selected only in a later run than A/B (tag schedules), B may get
  an MR today and C another one tomorrow; repoactive does not close or
  retarget the earlier MR. The suppressed job's branch is still pushed, so
  an MR that already exists on it keeps updating via the push even when its
  `ensure_mr` is skipped.
- A suppressed job's changes always end up in some created MR: in any chain
  the topmost job with a recorded MR is covered by nothing, so its MR always
  survives.
- With multiple dependents (diamonds), one covering MR suffices to suppress,
  but the other dependent's MR _also_ contains this job's changes — merging
  both double-lands them. That hazard is inherent to stacking on multiple
  dependents, not introduced here, but `"unless-superseded"` makes such
  shapes more tempting.
