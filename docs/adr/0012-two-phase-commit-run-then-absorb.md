# 12. Run jobs on fresh commits, then absorb results into existing commits

Status: Accepted (the absorb variant was implemented; see the alternative
below for the rejected simpler form)

## Context

When a job re-runs and its branch bookmark already exists from a prior run,
repoactive currently reuses that commit in place:

```
ws.edit(bookmark)    # @ = existing commit (change-id X)
ws.rebase(*parents)  # X rebased onto new parents; jj auto-rebases X's children
ws.restore(bookmark) # working copy reset to X's changes so the command starts clean
```

This mutates the existing commit in place, which is good — jj's change-id
continuity means any commit that sits on top of X (a dependent job's branch
that was not selected this run) is automatically rebased by jj when X moves.

The bug: if the command then fails, `ws.abandon()` removes `@`, which IS the
bookmark commit. jj moves the bookmark pointer to the parent — effectively
trunk — destroying the job's branch and losing its previous result.
Dependent branches are left orphaned until they are next re-run.

### Why a simple "save and restore" does not work

Saving the old bookmark position before the rebase and restoring it on
failure is not straightforward: `jj rebase` mutates the commit in place (the
old revision no longer exists in the working view), so there is nothing to
restore to without reaching into the operation log (`jj undo`), which is a
heavy operation and not safe to call from code that may itself be running
concurrently.

### Why successor commits matter

When job B depends on job A and A is re-run:

- **If A's commit is mutated in place** (same change-id): jj sees the old A
  as obsolete and auto-rebases B's commit on top of the new A. B's branch
  stays correctly parented even if B is not selected in this run.
- **If A's commit is replaced by a new commit** (new change-id): jj has no
  knowledge of the relationship. B's commit is left on top of the
  now-abandoned old A until B is next re-run — an orphaned commit in the
  repo's visible history.

Any fix must preserve the in-place mutation semantics for jobs that have
dependents not selected in the same run.

## Decision

Split the run into three phases: **run**, **absorb**, **apply**.

### Phase 1 — Run

Run every selected job on a **fresh commit**, regardless of whether a
bookmark already exists. Each job's workspace starts with `ws.new(*parents)`
(no `edit`/`rebase`/`restore`). Old bookmarks are never touched during
execution.

If a job's command fails, `ws.abandon()` discards only the fresh working
commit. The existing bookmark and all dependent commits are completely
unaffected.

Each successful job records its result as before but additionally stores the
new commit's change-id alongside the old commit's change-id (if a bookmark
existed) and the parent revsets it ran on (`JobResult.new_change_id`,
`old_change_id`, `parents`). The `UpdatePlan` is not built during this
phase; the absorb phase builds it once the canonical commits are known.

### Phase 2 — Absorb

Walk successful jobs — generator-emitted jobs included — in topological
order (parents before children). For each job that had an existing bookmark
commit:

1. `jj rebase -r old_change_id --onto <parents>` — move the old commit onto
   the same parents the new commit used, without touching `@`. jj
   auto-rebases any dependents that are not in this run (change-id
   continuity preserved) and no-ops when the old commit is already on the
   correct parents. The bookmark follows the rewrite; no explicit bookmark
   move is needed.
2. Compare the diff of the rebased old commit with the diff of the new
   commit. If they are identical, steps 3 and 4 are skipped — the rebase
   alone produced the correct result, and the old commit's message is
   already valid.
3. `ws.restore(new_change_id)` — replace the old commit's content with the
   new commit's content. Both commits share the same parents, so the same
   diff applies to the same base; the result is identical.
4. `jj describe -r old_change_id` — set the commit message.
5. Abandon the new commit (`jj abandon new_change_id`) — it is now
   redundant.

For jobs with no prior bookmark, the new commit is the canonical result; the
bookmark is simply set on it (same as today).

**Canonical change-id translation.** A dependent job's recorded parents are
the change-ids of its dependencies' _fresh_ phase-1 commits — which the
absorb abandons. The absorb therefore keeps a map from each job's phase-1
change-id to its canonical post-absorb change-id (the old change-id when a
bookmark existed, the new one otherwise) and translates every job's parents
through it before rebasing. Without this, a stacked job's old commit would
be rebased onto its dependency's abandoned fresh commit instead of the
absorbed one.

The absorb rebase/diff/describe/abandon steps run against the main
repository (job workspaces are torn down after phase 1). The restore step
needs a working copy, so a single temporary workspace is created lazily —
only when some job's content actually differs — and shared across all jobs
in the run. No command execution happens in this phase.

### Phase 3 — Apply

Push bookmarks and create/update MRs, exactly as today.

### Companion: select successor jobs automatically

When job A is selected for a run, repoactive should inspect the commits that
sit directly on top of A's current bookmark commit (its children in the
commit DAG), read their `Repoactive-Job` trailers, and force-include the
named jobs into the current run.

This is distinct from the existing `unmerged_job_names` mechanism (ADR
0003), which includes all jobs that have any unmerged branch regardless of
structure. The new selection is targeted: only jobs whose commit is an
immediate child of a selected job's commit are added. The intent is
consistency — if A changes, any job that built its last result on top of A's
old commit should rebuild on top of A's new result, so the whole stack stays
fresh in a single run.

Without this, the absorb phase would update A's old commit and jj would
auto-rebase B's old commit on top of it — but B's content would remain the
output of B's last run, computed against the previous A. B would be
structurally correct (on the right parent) but semantically stale until B's
next independent run.

The `Repoactive-Job` trailer is the right signal because it records which
job produced each commit, independently of the current config's `depends_on`
graph. A job that was previously stacked on A carries the trailer regardless
of whether its config still declares `depends_on: ["A"]`.

Force-included successor jobs follow the same selection rules as today (they
are added to `run_names`, their own successors are inspected recursively,
and they may themselves pull in further successors).

### Alternative considered: skip absorb, push new commits directly

Phase 2 can be omitted entirely: after phase 1, move each bookmark directly
to the new commit and push. This is simpler to implement. The cost is loss
of change-id continuity — dependent commits not in this run are left on
now-abandoned commits until their next re-run. If all dependent jobs are
always selected together (e.g. the full default run), the orphan window is
zero and this alternative is sufficient.

## Consequences

- **Failure is safe.** A failed command leaves every existing bookmark
  completely unchanged. Dependent branches are not affected.
- **Change-id continuity is preserved** (absorb variant). A human watching
  the repo between runs sees `jj obslog` chains rather than disconnected new
  commits appearing each run. Dependent branches that are not in this run
  are kept correctly parented by jj's auto-rebase.
- **Phase 2 is a new partial-failure mode.** If the absorb fails partway
  through (unexpected jj error), some old commits are updated and others are
  not. The fresh new commits for the un-absorbed jobs are unreachable (their
  bookmarks were not moved). Recovery requires a re-run, which re-runs phase
  1 and re-attempts the absorb. The window is small: absorb operations are
  local jj commands with no network I/O and no command execution.
- **`restore --changes-in` relies on shared parents.** The correctness of
  step 3 in phase 2 depends on the new commit and the rebased old commit
  having the same parents (and therefore the same base content). This is
  guaranteed by construction: phase 1 always uses `ws.new(*parents)`, and
  the absorb rebases the old commit onto those same parents (translated to
  their canonical post-absorb change-ids, which name the same changes).
- **Unchanged jobs get a free skip.** When the new commit's diff matches the
  rebased old commit's diff (command produced the same output as the prior
  run on the same parents), steps 3 and 4 of the absorb are skipped. The old
  commit is already correct after the rebase; no content or message update
  is needed. This is the common case for a stable job on an unchanged
  codebase.
- **Simpler per-job code.** The `bookmark_existed` branch in `run_job` (with
  its `edit`/`rebase`/`restore` tangle) is replaced by unconditional
  `ws.new(*parents)`. The absorb logic is concentrated in one new function
  rather than interspersed with command execution.
- **Successor selection keeps stacks fresh.** By force-including jobs whose
  commits build on a selected job's commit, a single run updates the whole
  stack rather than leaving dependents on a stale (though structurally
  correct) base. The `Repoactive-Job` trailer makes this detection
  independent of the current config's `depends_on` declarations.
- **No behaviour change for new jobs.** A job with no prior bookmark skips
  the absorb step entirely; its new commit becomes the bookmark directly.
