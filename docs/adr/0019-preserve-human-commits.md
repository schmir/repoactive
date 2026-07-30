# 19. Preserve human commits on a repoactive branch

Status: Accepted

## Context

A developer wants to add manual changes an MR needs to pass CI. There are
two cases:

- a **prerequisite** is something the command builds on (e.g. additional
  constraints when doing dependency updates); it belongs below the command's
  output. a prerequisite is placed below by rebasing the repoactive branch
  onto it (`jj rebase`, a one-liner), so it genuinely sits below.

- a **fixup** reacts to the command's output (a test the regenerated code
  breaks, a call site the new signature requires); it belongs above the
  command's output. a fixup is committed on top.

A simple example with human generated commits looks like:

```
trunk -> [prerequisites] -> [command output] -> [fixups]
          (human, below)     (repoactive)        (human, above)
```

For a stacked job (`depends_on`), the same picture recurs one level up: job
B's real base is not just job A's command commit but A's whole branch tip,
fixups included - that is what "A" actually means once a human has reacted
to it. Getting this wrong is subtle rather than loud: B still builds, it
just builds against content that quietly excludes what the human added to A.

## Decision

repoactive detects and handles manual changes. It scans the commits on a
repoactive generated branch and finds prerequisites, fixups and the commit
with the previous command output. repoactive will use the `Repoactive-Job`
trailer to distinguish repoactive's own commits from manual changes - both a
job's own command commit and, where relevant, a dependency's command commit
carry it; everything else is human.

It will not touch prerequisites, but will keep them as prerequisites when
running a new command. It will rebase fixups on newly generated command
output. When any of the operations result in conflicts, it will mark the
resulting MR as needing manual intervention.

### Detection

Detection is a graph split around repoactive's own commit, anchored on the
parents the current run computes rather than on trunk. Both inputs come from
the repository plus config alone:

- `R = <branch>` - the fetched branch tip and its ancestry.
- `P` - the parents this run will build on, from config: `[trunk()]` for a
  plain job (or the job's `base_branch`), or the dependency tips for a
  stacked job (`depends_on`). This is the same `parents` list `run_job`
  already passes to `repo.new`.

With those:

1. **Find the command commit C.**
   `C = (P..R) & <Repoactive-Job trailer == this job>`, using the same
   trailer predicate as `last_job_commit_date` (`jj.py`). For a stacked job,
   the dependency's command commit and its prerequisites and fixups are all
   ancestors of `P`, so they never enter `P..R` - _provided_ the
   dependency's own branch is not itself stuck at a stale position (see
   "Stale dependency anchors" below for when it is). Each job's slice is
   exactly `P..R`, and the trailer only has to answer a per-commit "is this
   mine?" within that slice.
2. **Prerequisites are everything below `C`:** `(P..C) & ~C`, restricted to
   commits carrying no `Repoactive-Job` trailer at all - the human commits
   inserted between the run's own parents and the command commit. A commit
   in this range that _does_ carry a trailer is not a prerequisite; see
   "Stale dependency anchors".
3. **Fixups are everything above `C`:** `C..R`, reapplied in order on top of
   the regenerated command commit.

The new command commit's parents become `[prereq-tip, *P]`: the prerequisite
tip merged with this run's parents (see "Prerequisites: merged with trunk").
For a plain job with no prerequisites this collapses to `[trunk()]`, the
current behaviour.

A commit is repoactive's iff it carries the trailer; every other commit on
the branch is human, and below-vs-above falls straight out of the DAG.

### Run ordering: absorb each job before its dependents compute theirs

> **Note (superseded mechanism).** The absorb phase this section describes
> was replaced by an in-place rewrite of the command commit in
> [ADR 0020](0020-rewrite-command-commit-in-place.md). The _ordering_
> requirement below still holds - a dependency is fully rebuilt (fixups
> included) before its dependent runs on its tip - but it now falls out of
> rewriting each job's command commit in place, in topological order, rather
> than from a separate absorb step. Read "absorb" below as "rewrite in
> place."

Detection's `P` for a stacked job must be the dependency's _true_ current
tip - fixups included - not an intermediate value. That rules out batching:
running every selected job on a fresh commit first and absorbing all of them
afterwards (the two-phase split of
[ADR 0012](0012-two-phase-commit-run-then-absorb.md)) computes a stacked
job's parents from its dependency's fresh, pre-absorb commit, which is only
the regenerated command output - any fixup on the dependency is stitched
back on top later, after the dependent already ran against content that
didn't have it.

repoactive therefore absorbs each job immediately after its own run
succeeds, before moving to the next job in topological order, rather than
running every selected job first and absorbing the whole batch at the end.
The per-job mechanics ADR 0012 established - fresh commit, restore-in-place,
rebase-and-carry descendants - are unchanged; only the phase boundary moves,
from "once per run" to "once per job." A dependent's `effective_revsets` is
therefore always the dependency's absorbed, canonical bookmark tip, and the
canonical-parent translation table ADR 0012 introduced to paper over
not-yet-absorbed dependents becomes unnecessary - a dependent is always
computed from an already-canonical parent.

### Stale dependency anchors

repoactive never fetches (see
[ADR 0005](0005-local-repository-is-the-source-of-truth.md)), and a run may
see a genuinely fresh clone. Change-id continuity - the mechanism that lets
jj auto-rebase a dependent's commits when a dependency's commit is rewritten
in place - is bookkeeping local to the jj repository instance that performed
the rewrite; it is not part of any git object and does not survive a fresh
clone. What _does_ survive, because it is ordinary git content, is commit
ancestry and the text of the `Repoactive-Job` trailer.

This matters because a job can be frozen (see "Freeze on conflict") and
therefore left unpushed while its dependency's bookmark moves on. Consider A
pushed (its bookmark now points at a new commit, call it A') while a stacked
B stays frozen on its old chain, rooted in A's previous commit A. A is not
an ancestor of A' (a rewrite-in-place is a content change, not a
fast-forward, so A and A' are git siblings under a shared parent, not parent
and child). The next run - possibly a fresh clone that never saw this run's
jj state - fetches A's bookmark at A' and B's bookmark still rooted in A.
Detection for B computes `P..R` where `P` is A' and `R` is B's tip; A falls
inside that range, carrying `Repoactive-Job: A`, not B. Naive detection
(matching only the current job's own trailer) cannot tell A apart from a
genuine human prerequisite - and prerequisites, once classified, are never
touched again. Left uncorrected, A's superseded output would be welded into
B permanently, and the dependency relationship from B to A would be silently
severed.

So detection recognizes the trailer of any job B currently depends on, not
only B's own. A commit in `P..C` carrying a dependency's trailer is a
**stale dependency anchor**: superseded output from an earlier run of that
dependency, not a prerequisite. It is discarded, and whatever sits between
it and B's own command commit (prerequisites, C itself) is rebased onto the
dependency's _current_ tip instead of preserved beneath the stale anchor. A
prerequisite, from here on, is specifically a commit carrying no recognized
`Repoactive-Job` trailer at all - not merely "anything below C."

When the stale anchor is the immediate parent of C - nothing human sits
between them - this resolves automatically: rebase C onto the dependency's
current tip and proceed as normal (subject to the ordinary conflict check in
"Freeze on conflict").

### Degenerate cases are detected states, not ambiguities

Anchoring on `P` and the trailer turns three shapes that would otherwise be
ambiguous into distinct, detectable outcomes:

- **Empty `P..R`.** The branch exists but adds nothing over the run's
  parents - `R` is an ancestor of `P`. This is what a manually merged MR
  left un-deleted looks like: its commits are now all in trunk, so the
  branch has nothing of its own. There is nothing to preserve, so regenerate
  the command commit on `P` alone (parents `[*P]`, no merge), identical to a
  branch that does not exist yet. Checking emptiness first keeps a stale
  merged branch from forcing a needless merge commit.
- **No trailer match in a non-empty `P..R`.** There are branch commits but
  no command commit for this job among them: a first run the human seeded,
  or a branch a human recreated after an empty run deleted it. The clone
  alone cannot say which, and it does not need to: the whole of `P..R` is
  human commits, so treat it all as prerequisites. The regenerated command
  commit takes `[R, *P]` as parents - the existing human tip merged with the
  run's parents - and repoactive's output lands on top, exactly as if those
  commits had been placed below by hand.
- **More than one trailer match in `P..R`.** repoactive rewrites its command
  commit in place every run, so a single job's slice should hold exactly
  one. Two or more is an anomaly - a botched earlier run that left
  duplicates. Rather than guess which to split around, freeze the branch and
  signal it (same mechanism as "Freeze on conflict").

A fourth shape is ambiguous, not merely degenerate, and is handled
separately: see "An unresolvable mix" below.

### An unresolvable mix: a dependency's fixup and this job's own prerequisite

A stale dependency anchor (previous section) resolves cleanly only when
nothing human sits between it and `C`. It can be less clean: if the
dependency itself had an unresolved fixup on top of its old commit at the
time B was last built with its own prerequisite rebased in, `P..C` contains,
in order,
`[stale anchor] -> [dependency's old fixup] -> [B's own prerequisite] -> C`.
Both middle commits carry no trailer - only the dependency's own
fixup-turned-human-commit distinguishes itself from any other human commit
by nothing at all. There is no marker anywhere - by design, ADR 0019 asks a
human to learn no new convention - that says which of two adjacent,
trailer-less commits belongs to the dependency's story versus this job's
own.

The two possible resolutions are not equally bad:

- Treat the whole range as this job's prerequisite (untouched, merged with
  trunk forever): the dependency's stale fixup is welded in permanently,
  silently diverging from whatever the dependency's own current state
  actually is. Bad, but inert and visible to anyone who looks.
- Treat the whole range as stale and discard it, rebasing `C` straight onto
  the dependency's current tip: this silently deletes a human's actual
  prerequisite commit with no warning - exactly the outcome this ADR exists
  to prevent.

Given that asymmetry, repoactive does not guess. Finding a stale dependency
anchor with additional untagged commits between it and `C` is treated like
`UnexpectedLayers`: an anomaly, not a decision repoactive can make safely on
the information available. Freeze the branch, and signal it with a note
identifying the anchor commit and the ambiguous range, so a human can
resolve it explicitly - a plain rebase settles it in one step, since the
human already knows which commits are theirs.

This case is rare by construction:
[ADR 0012](0012-two-phase-commit-run-then-absorb.md)'s successor selection
force-includes any job stacked on a selected job's bookmark into the same
run, so a healthy branch is always rebased onto its dependency's live tip in
the same run that dependency changes - there is nothing left over to be
ambiguous about. This shape can only arise as a second-order effect of an
earlier freeze that persisted across a run boundary, not in ordinary
operation.

### Prerequisites: merged with trunk, never rebased

The new command commit takes the prerequisite tip and `trunk()` as parents:

```
parents = [prereq-tip, base_branch]
```

`repo.new` already accepts multiple parents, and per-job absorb (see "Run
ordering" above) means those parents are always already-canonical by the
time this job runs - no translation step is needed. jj collapses the
degenerate case: while the prerequisite is still based on current `trunk()`,
trunk is an ancestor of the prerequisite tip and the merge is just
`[prereq-tip]`; once trunk moves, a real two-parent merge pulls trunk in
without moving the prerequisite. repoactive never rebases the prerequisite -
keeping it current is the human's job, but the merge means they only have to
act when it genuinely conflicts with trunk.

### Fixups: reapplied on top

Fixups are descendants of the command commit. The absorb phase already
mutates the command commit in place (preserving change-id continuity) and
carries its descendants along with `rebase_source`; fixups ride that path
onto the regenerated output.

### Freeze on conflict

jj refuses to push a commit that contains a conflict, so a conflict can
never be materialized onto the branch and left for CI. One rule covers every
source of one:

> If rebuilding the branch would require pushing a commit with a conflict,
> push nothing. Leave the remote branch at its last clean state, and signal
> the branch as needing a human.

The sources are the merge of the prerequisite with `trunk()`, the reapply of
a fixup onto regenerated output, and - now that a stacked job's parent is
another job's live tip - a stale-anchor rebase that no longer applies
cleanly. All three are checked the same way and produce the same outcome.

**The decision is per branch, not per cause.** Whether job X's bookmark gets
pushed is decided entirely from X's own rebuild. A conflict discovered while
processing a stacked dependent never holds back the dependency's own push:
if A's rebuild is clean, A is pushed regardless of whether a job stacked on
it freezes. Conversely, A itself may freeze while a sibling dependent of
some other, unrelated job pushes normally. Attributing a downstream conflict
to the upstream job that "caused" it does not generalize - a job can have
several dependents, and only the ones that actually conflict should freeze.

**Freezing never touches an MR's own lifecycle.** A frozen job's MR - if one
is already open from an earlier successful push - is left exactly as it was:
same content, same open/closed state. Freezing only means the current run's
rebuild is not pushed. The `repoactive:needs-rebase` label and a one-line
note naming the commit that no longer applies are applied to that existing
MR (idempotently - reapplied, not duplicated - and cleared once a later run
rebuilds the branch cleanly), so the signal reaches the human where they are
looking, not only the run log.

**Locally, nothing is held back.** jj happily represents a conflicted
commit; only pushing (exporting to git) a conflicted commit is refused. So
the local rewrite and cascade always run to completion regardless of what
will end up frozen - freezing is purely a decision about the push step, made
once the local result is known.

Freezing loses no work: nothing is pushed, so the human's commit stays live
as the branch tip, waiting to be rebased or replaced. The cost is that the
frozen branch stops advancing until the human resolves it - acceptable,
because a conflicting change already demands attention.

## Consequences

- **The refresh guarantee (ADR 0003) survives, via merge.** A branch with a
  prerequisite still tracks a moving `trunk()` because the merge pulls trunk
  in; the human owns only the resolution of a genuine prerequisite/trunk
  conflict, not routine trunk tracking.
- **repoactive is no longer the sole writer of the branch.** A human pushing
  while repoactive is running might result in race errors.
- **Idempotency holds.** With `trunk()` and the command output unchanged,
  the merge collapses, fixups reapply onto identical parents, and every
  layer keeps its id, so nothing is pushed and no CI is retriggered. The
  only unavoidable push is when the output genuinely changes and fixups are
  reapplied - which is correct.
- **A branch with a prerequisite may carry a merge commit.** This is
  familiar (it is what GitHub's "Update branch" produces), but teams on
  strictly linear history should expect it, and a squash merge additionally
  discards the `Repoactive-Job` trailer (already documented for cooldown),
  so that caveat compounds.
- **Nothing new for the human to learn.** No trailer, no keyword, no branch
  naming: a fixup is "commit on top of the PR" and a prerequisite is "rebase
  the branch onto your commit", both native jj/git operations.
- **Per-job absorb changes the phase boundary from ADR 0012, not its
  mechanics.** Run and absorb still happen per job on a fresh commit with
  restore-in-place; they simply interleave per job in topological order
  instead of batching every job's run before any absorb. This is what lets a
  stacked dependent's parent always be its dependency's true, already-
  absorbed tip.
- **Cross-run correctness rests on git content, not on jj's local state.**
  Because repoactive assumes nothing about a run's clone provenance
  (ADR 0005) and jj's own change-id continuity for a rewritten commit does
  not survive a fresh clone, detection must recognize a stale dependency
  anchor by its portable `Repoactive-Job` trailer rather than relying on jj
  to auto-rebase a dependent across separate runs.
- **An ambiguous mix is frozen, never guessed.** When a stale dependency
  anchor and this job's own prerequisite would otherwise be
  indistinguishable, repoactive freezes and signals rather than picking a
  side - the two wrong guesses are not equally bad (silent staleness versus
  silent data loss), so neither is worth risking automatically. This state
  is rare: it only compounds from an earlier freeze that survived a run
  boundary, since successor auto-inclusion (ADR 0012) keeps a healthy stack
  rebuilt in the same run as its dependency.
- **Freezing is scoped to the branch that actually conflicts.** A
  dependency's clean rebuild is pushed independently of a dependent's
  freeze, and freezing a branch never alters its MR's open/closed state -
  only the label and note change.
