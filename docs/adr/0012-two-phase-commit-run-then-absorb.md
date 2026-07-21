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
3. `jj restore --from new_change_id --into old_change_id` — replace the old
   commit's content with the new commit's content. Naming both revisions
   rewrites `old_change_id` directly, without touching any working copy.
   Both commits share the same parents, so the same diff applies to the same
   base; the result is identical.
4. `jj describe -r old_change_id` — set the commit message.
5. `jj rebase -s new_change_id --onto old_change_id` — before abandoning the
   new commit, rebase it (and, critically, its descendants) onto
   `old_change_id`. A dependent job stacked on this job in the same run
   still has its own phase-1 fresh commit parented on `new_change_id` at
   this point (see "Canonical change-id translation" below); `-s` carries
   that commit along to `old_change_id`'s now-canonical content. This is a
   content no-op for `new_change_id` itself, since `old_change_id` already
   holds identical content by construction (steps 2-4). `-r` (a single
   revision, used in step 1) is not enough here: it leaves existing
   descendants behind, refilled onto the moved revision's _old_ parent
   instead of following it — exactly the corruption this step prevents (see
   Consequences).
6. Abandon the new commit (`jj abandon new_change_id`) — it is now
   redundant, and childless or content-identical to its child if not (step 5
   already moved every descendant off it).

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

Every absorb step — rebase, diff, restore, describe, abandon — names its
revisions explicitly, so all of them run against the main repository and
none need a working copy (the per-job workspaces are torn down after phase
1). No command execution happens in this phase.

### Phase 3 — Apply

Push bookmarks and create/update MRs, exactly as today.

### Companion: select successor jobs automatically

When job A is selected for a run, repoactive inspects all unmerged commits
that are descendants of A's current bookmark, reads their `Repoactive-Job`
trailers, and force-includes the named jobs into the current run. This is
implemented as a single `pending_job_names(revset=...)` call using a
`descendants()` revset, so the entire stack above A is discovered in one
query without iteration.

This is distinct from the no-arg `pending_job_names()` mechanism (ADR 0003),
which includes all jobs that have any unmerged branch regardless of
structure. The successor expansion is targeted: only jobs whose commits sit
above a selected job's bookmark are added. The intent is consistency — if A
changes, any job that built its last result on top of A's old commit should
rebuild on top of A's new result, so the whole stack stays fresh in a single
run.

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

Whether a successor actually runs is decided at dispatch time, once the fate
of the jobs below it is known. A successor exists to be rebuilt when the
stack below it moves, so it bypasses its own cooldown — but when every one
of its dependencies was itself skipped this run (the selected job turned out
to be on cooldown, or a lower successor was skipped for the same reason),
nothing it builds on changed and it is skipped with a no-op result. The skip
propagates up the stack and leaves the successor's bookmark untouched in the
absorb phase. The decision is judged on `depends_on`, not the commit graph:
a successor whose config no longer declares the dependency it is stacked on
falls through and runs — the safe direction when trailer and config
disagree.

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
- **`restore --from/--into` relies on shared parents.** The correctness of
  step 3 in phase 2 depends on the new commit and the rebased old commit
  having the same parents (and therefore the same base content). This is
  guaranteed by construction: phase 1 always uses `ws.new(*parents)`, and
  the absorb rebases the old commit onto those same parents (translated to
  their canonical post-absorb change-ids, which name the same changes).
- **Abandoning `new_change_id` without step 5 silently reverts a stacked
  dependent's ancestor.** A dependent job's phase-1 fresh commit is parented
  on its dependency's `new_change_id` (see "Canonical change-id
  translation"). `jj abandon` reparents descendants onto the abandoned
  commit's _original_ parent, not onto `old_change_id` — the same gap-fill
  behaviour as `-r` (see step 5). If the dependent hasn't been absorbed yet
  when its dependency is abandoned, its fresh commit is silently
  transplanted onto the dependency's pre-run parent, dropping the
  dependency's diff entirely. The absorb then compares that corrupted commit
  against the correctly-rebased `old_change_id`, finds a difference, and
  overwrites the correct content with the corrupted one — reverting the
  ancestor's change out of the stack. Step 5's `-s` rebase closes this by
  moving the not-yet-absorbed descendant onto `old_change_id` first, before
  the abandon's own gap-fill can reach it.
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
