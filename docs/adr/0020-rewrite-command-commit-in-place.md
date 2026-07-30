# 20. Rewrite the command commit in place; no absorb phase

Status: Accepted (supersedes
[ADR 0012](0012-two-phase-commit-run-then-absorb.md) and replaces the "Run
ordering" / absorb mechanics of [ADR 0019](0019-preserve-human-commits.md))

## Context

[ADR 0012](0012-two-phase-commit-run-then-absorb.md) ran every job on a
_fresh_ commit and then _absorbed_ the result back into the pre-existing
command commit, to keep a failed command from destroying an existing branch
and to preserve jj change-id continuity. That split accreted machinery: a
run phase, an absorb phase, a canonical-parent translation table, and - once
[ADR 0019](0019-preserve-human-commits.md) added prerequisites and fixups -
a per-job absorb that interleaved the two phases so a stacked dependent
could fork from its dependency's already-absorbed tip.

All of that exists to move regenerated content from a throwaway fresh commit
onto the real command commit without losing the command commit's identity.
But jj can rewrite a commit _in place_ directly: point the working copy at
the command commit, empty it, and let the command regenerate its content
there. The change-id is preserved for free, fixups (descendants) auto-rebase
onto the new content, and there is nothing to absorb afterwards.

The one thing ADR 0012 got from the fresh commit - failure safety - is
recoverable another way. ADR 0012 rejected reaching into the operation log
(`jj op restore`) as "a heavy operation and not safe to call from code that
may itself be running concurrently." That objection no longer holds:
`run_all` takes a per-repository run lock and executes jobs sequentially, so
no operation races the restore. An op-log restore to the state captured
before a job mutated anything is exact, cheap, and local.

## Decision

`run_job` rewrites the command commit in place; there is no absorb phase.

Detection ([ADR 0019](0019-preserve-human-commits.md)) still classifies the
branch. Let `anchor = command_anchor(layers)` be the commit to rewrite (the
`Repoactive-Job` command commit for a normal branch, `None` for a new job or
a merged-and-undeleted branch).

Before any mutation, capture `op_id = repo.op_id()`. Then:

- **`anchor` is not `None` (existing branch).**
  1. `jj rebase -s anchor --onto <rebase_targets>` - move the command commit
     _and its fixup descendants_ onto the run's parents, with prerequisites
     merged in via `heads(...)` (the `rebase_targets` of ADR 0019,
     unchanged).
  2. `jj edit anchor` - make it the workspace's working copy.
  3. `jj restore` - empty it, discarding the old command output so the
     command regenerates onto a clean tree (and so a conflicting refresh is
     never fed to the command as conflict markers).
  4. Run the command; `jj describe` the regenerated output.

  The change-id is preserved, so the bookmark and any not-selected dependent
  branches follow automatically, and fixups ride on top via jj's
  auto-rebase-on-rewrite.

- **`anchor` is `None` (new job / merged branch).** `jj new <parents>`, run,
  `jj describe`. The bookmark is set on the new commit by the plan step.

Then, per outcome:

- **Command fails:** `jj op restore op_id`. The branch is left byte-for-byte
  as it was; nothing is pushed.
- **Empty diff:** `jj abandon` the now-empty command commit, then the plan
  step deletes the bookmark (if any) and records the remote deletion - a
  command that now produces nothing retires its branch. This applies only
  when the branch carries no human commits. If it does (prerequisites or
  fixups per [ADR 0019](0019-preserve-human-commits.md)), the empty command
  commit is kept instead: abandoning it would reparent those human commits
  directly onto the run's parents, and the next run would then read them as
  prerequisites rather than fixups. The kept commit still gets
  `jj describe`d with the regenerated (empty) message.
- **Non-empty diff:** the rewritten (or fresh) command commit is the result.
  `run_job` returns its branch tip as `effective_revsets`, so the next
  dependent forks from it directly.

Because the rewrite happens per job in topological order, a dependency is
fully rebuilt - fixups included - before its dependent's `repo.new` runs on
its tip. This is the ordering [ADR 0019](0019-preserve-human-commits.md)
called "absorb each job before its dependents compute theirs," now achieved
without an absorb step at all.

### Stale default workspace

The rewrite runs in the job's throwaway workspace. Rewriting a commit that
_another_ workspace has checked out marks that workspace stale, and the
default (colocated) workspace is exactly where a human's `@` sits when they
have committed a fixup on the branch - the
[ADR 0019](0019-preserve-human-commits.md) scenario. jj then refuses further
working-copy commands there, including the next job's `jj workspace add`.
`run_job` therefore reconciles the default workspace with
`jj workspace update-stale` after each job (and defensively before each
`workspace add`), fast-forwarding it to the rewritten commit - the same move
jj would make automatically had the rewrite happened in that workspace.

## Consequences

- **No absorb phase.** The run/absorb/apply split collapses to run/apply.
  Gone: the fresh-then-fold dance, the canonical-parent translation table,
  `_absorb_job`, and the `same_content`/restore-in-place plumbing. `run_job`
  writes the final commit directly, and a small plan step only finalizes the
  bookmark (set for a fresh commit, delete for an empty result) and records
  the push/MR.
- **Failure safety rests on the op log, not a fresh commit.** A failed
  command restores to the pre-mutation operation, leaving the branch
  unchanged; an empty result abandons the command commit and lets the plan
  step retire the bookmark. Safe because the run lock serialises runs and
  jobs run sequentially - the concurrency objection ADR 0012 raised against
  op-log restore does not apply here.
- **The default workspace can move.** `update-stale` advances a human's `@`
  to the rewritten commit when it sat on the branch. This is what an
  in-workspace rewrite would have done anyway, but teams should know a run
  may fast-forward the working copy off an obsolete commit.
- **Idempotency skip reinstated for unchanged reruns.** ADR 0012's
  "unchanged jobs get a free skip" (don't rewrite/push when the regenerated
  content and message match the previous run, so CI is not retriggered) is
  back, adapted to the in-place path. `classify_branch` sets
  `run_idempotency_check` on a `NormalLayers` branch when _both_ hold: the
  rewrite would not move the command commit (its current parents already
  equal the resolved rebase target), and the branch still matches what was
  last pushed (the local tip's commit id equals the remote-tracking
  bookmark's). When it holds and the regenerated tree and stripped message
  match the pre-rewrite command commit, `run_job` rolls the rewrite back via
  the run's op checkpoint, so the command commit keeps its original id - the
  bookmark does not move, jj pushes nothing, and CI is not retriggered -
  instead of being rewritten with a fresh committer timestamp every run.
- **The skip compares commit ids against the remote, not just parents.** jj
  preserves the change id across an auto-rebase, so when a dependency
  changes and jj rebases this branch onto it, the branch's command commit
  moves (new commit id) while its parents still "look" like a no-op rebase.
  Only comparing the local tip's commit id against the remote-tracking
  bookmark reveals that the branch has diverged from what was pushed and
  will be pushed regardless - so the skip correctly does not fire there, and
  the regenerated output lands with a fresh message rather than reusing the
  previous run's. A corollary is that the skip only ever fires in a push
  run: a local run never pushes, so there is no remote-tracking bookmark to
  match and the rewrite always happens (nothing is pushed either way).
- **Everything ADR 0019 promised the human still holds.** Prerequisites are
  merged (never rebased), fixups ride on the regenerated output, a merged
  branch is cleaned up, and detection is unchanged - only the mechanism that
  applies the result moved from absorb to in-place rewrite.
