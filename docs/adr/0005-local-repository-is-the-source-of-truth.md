# 5. The local repository view is the source of truth (no fetch)

Status: Accepted

## Context

`repoactive` makes every scheduling and placement decision from the
**local** jj/git repository it is pointed at. It never runs `git fetch` (nor
`jj git fetch`): no command in the codebase updates the local view from the
remote before a run. Three load-bearing decisions read that local view:

- **Rebasing.** A job's commit is created on top of `trunk()` (or its
  `base_branch`), and an existing job branch is rebased onto it
  (`runner._compute_parents`, `runner.run_job`).
- **Cooldown.** A job on `cooldown_period` is skipped when a commit carrying
  its `Repoactive-Job` trailer has landed on the base branch inside the
  window (`runner._on_cooldown` → `JJ.has_recent_job_commit`, revset
  `::<base>`). See [ADR 0001](0001-no-schedule-field.md) for why this
  trailer-based gating is the foundation the design rests on.
- **Unmerged-branch refresh.** The bare run sweeps up jobs whose last commit
  is not an ancestor of `trunk()` (`JJ.pending_job_names`, revset
  `~(::trunk())`) and rebases them onto the current `trunk()` — see
  [ADR 0003](0003-refresh-unmerged-branches-in-default-run.md).

All three are computed against whatever the local clone last saw. A merge
that happened on the remote is invisible to `repoactive` until the local
`trunk()` advances past it.

## Decision

`repoactive` treats the local repository as the source of truth and does
**not** fetch on its own. **Keeping the local clone current — in particular
advancing `trunk()` / the base branches to match the remote — is the
caller's responsibility**, done before invoking `repoactive` (e.g. a
`jj git fetch` / `git fetch --prune` step in the same cron job or CI
pipeline that runs `repoactive`).

This is deliberate: fetching is a policy decision (which remote, which refs,
how to handle diverged local trunk, what credentials) that belongs to the
surrounding automation, not to a tool whose job is to run scripts and manage
the resulting branches. Pushing — the one network write `repoactive`
performs — is explicit and gated behind `--mode push`/`--mode publish`;
reads from the remote are left to the environment for the same reason.

## Consequences

- **The clone must be fetched before each run.** If it is not, the
  consequences compound:
  - Jobs rebase onto a stale `trunk()` and may push branches that redo work
    that has already landed, or that conflict with the true remote state.
  - **Cooldown silently fails to engage.** Cooldown keys on the trailer
    reaching the _local_ base branch. An MR that merged on the remote does
    not advance local `trunk()` until a fetch, so `has_recent_job_commit`
    keeps returning false and the job re-runs every cycle — the throttling
    that [ADR 0001](0001-no-schedule-field.md) relies on does not happen.
    (This is on top of the separate squash-merge caveat: a squash merge
    discards the commit and its trailer entirely.)
  - The unmerged-branch refresh
    ([ADR 0003](0003-refresh-unmerged-branches-in-default-run.md)) keeps
    refreshing a branch that has in fact already landed, until the fetch
    lets the local `trunk()` catch up; only then does the empty-diff
    handling delete it.
- **Only the freshness of the local clone matters, not its provenance.**
  `repoactive` does not care how `trunk()` got current — a sibling `fetch`
  step, a freshly cloned CI checkout, or a human pulling all satisfy the
  assumption equally.
- **Reproducibility is bounded by the local view.** Two runs against the
  same config but different local fetch states can legitimately differ (one
  sees a job's change as landed, the other does not). This is expected, not
  a bug.
- This assumption is documented for operators in the README ("Keeping the
  local clone current") and should be honored by any scheduled/CI wrapper
  around `repoactive`.
