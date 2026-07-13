# 15. `cooldown_on` throttles a job when a superset lands

Status: Accepted

## Context

Cooldown throttles a job by time: `_on_cooldown` looks at the base branch
for a recent landed commit carrying this job's `Repoactive-Job` trailer, and
if one lands within `cooldown_period`, the job stays quiet instead of
opening a redundant MR. Each job is throttled only by its own past landings.

That is too narrow when two jobs overlap in what they change. The motivating
case is dependency upgrades: a broad `uv lock --upgrade` job and a narrow,
group-scoped upgrade job both touch `uv.lock`. When the broad job lands, it
already contains everything the narrow job would produce, but because the
narrow job has never landed its own trailer, its own cooldown is not
tripped, so it opens an MR whose changes are already merged. Cooldown, keyed
strictly to a job's own name, cannot see that a superset has just landed.

## Decision

`Job` gains a `cooldown_on` field: a list of job names whose landings also
count toward this job's cooldown. When a job checks cooldown, it queries the
base branch for a recent commit carrying **its own trailer or any
`cooldown_on` trailer**; a match on any of them keeps the job on cooldown
for the remainder of `cooldown_period`.

So a narrow job lists the broader job(s) in `cooldown_on`. Once a superset
lands, the narrow job is throttled just as if it had landed itself, and the
window is measured from the superset's landing.

**Implementation:**

- `_on_cooldown` passes `job_names={job.name, *job.cooldown_on}` to
  `JJ.last_job_commit_date`, which matches a `Repoactive-Job` trailer equal
  to _any_ of those names within the cooldown window. This is a **query, not
  a stamp**: no extra trailers are written, so the relationship is entirely
  in the reading job's config and adds nothing to the superset's commits.
- Matching is via jj's trailer parsing (final paragraph only), so a stray
  matching line in a commit body is ignored. Job names are regex-restricted,
  so interpolating several into the revset template is as safe as one.

**Validation** (at load time):

- Names in `cooldown_on` are **not** required to refer to a configured job:
  they match trailers already landed on the base branch, which may come from
  jobs since removed or renamed from the config. They must, however, be
  syntactically valid job names (`InvalidJobNameError`), since they are
  interpolated into the cooldown revset.
- A job may not list itself (`SelfCooldownOnError`); it is always throttled
  by its own landings, so self-reference is a no-op mistake.
- `cooldown_on` requires an effective `cooldown_period` (own or inherited
  from `JobDefaults`); without a window it would silently do nothing
  (`CooldownOnWithoutCooldownPeriodError`).

**Not a dependency.** `cooldown_on` is orthogonal to `depends_on`: it
implies no ordering, no parent-stacking, and no run-selection. It only
widens the set of trailers the cooldown check reads. The named jobs need not
run in the same invocation; the check reads already-landed commits on the
base branch.

## Consequences

- A narrow job stops opening MRs that a broader landed job already subsumes,
  for the length of its cooldown. The narrow job still runs on its own
  schedule once the window elapses, catching anything the broad job did not
  cover.
- The relationship is one-directional and lives in the narrow job's config;
  the broad job is unaware of it and its commits are unchanged.
- Like all cooldown, this reads the local `trunk()` and never fetches
  ([ADR 0005](0005-local-repository-is-the-source-of-truth.md)): a stale
  clone will not see the superset's landing and the throttle silently will
  not fire.
- The suppression is time-bounded, not exact: `cooldown_on` throttles
  whenever _any_ named job landed recently, regardless of whether that
  landing genuinely contained the narrow job's changes. This is deliberately
  coarse; the precise, same-run collapse of an overlapping chain is
  [ADR 0009](0009-unless-superseded-mr-creation.md)'s job.
