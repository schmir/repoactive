# 2. Tag-based job selection

Status: Accepted

## Context

[ADR 0001](0001-no-schedule-field.md) rejected a per-job cron `schedule`
field: gating on the landed trailer cannot bound how often a command _runs_,
only how often a change _lands_. The sanctioned answer to "run this job on a
schedule" is to let real OS cron decide _when_ and have repoactive decide
only _which_ jobs — a pure selection problem with none of the
stateless-trailer pitfalls.

The pre-existing `disabled` boolean already hinted at this: it excludes a
job from the bare `repoactive run` while still allowing
`repoactive run my-job` to run it explicitly. But it is a single on/off axis
— there is no way to say "this job belongs to the weekly set" and select
that set from cron without editing the crontab every time the set changes.

## Decision

Add a per-job `tags: list[str]` and a repeatable `--tag/-t` selector on
`repoactive run`. Selection is by tag, with a smart default for each job:

- plain job (no `tags`, not `disabled`) → implicit tag `enabled`;
- `disabled = true` → sugar for `tags = ["disabled"]` (the two are mutually
  exclusive; setting both is a validation error);
- explicit `tags = [...]` → exactly those tags, which **drops** the implicit
  `enabled` unless the user lists it.

`repoactive run` with no names/tags is shorthand for `run --tag enabled`.
With names and/or tags it is explicit selection: the union of named jobs and
jobs matching any requested tag (OR), `enabled` not implied, dependencies
force-included. The bare run remains implicit selection: a job whose
dependency is not itself selected is dropped.

`disabled = true` is kept as typed sugar (rather than removed in favour of
`tags = ["disabled"]`) so `extra="forbid"` still catches a misspelled key
for the highest-stakes flag; a misspelled free-form tag would silently leave
a job enabled. See the decision recorded in the conversation that introduced
this ADR.

## Consequences

- Scheduling is now first-class without statefulness: tag a job (e.g.
  `tags = ["weekly"]`) and run `repoactive run --tag weekly` from cron. The
  set is edited in config, not in the crontab.
- **Tags are load-bearing, not free-form labels.** Assigning any tag removes
  the implicit `enabled` tag, so a tagged job leaves the default run. To
  keep a job in the default run _and_ in a group, list both:
  `tags = ["enabled", "weekly"]`. MR/PR labels remain a separate concept
  (`labels`).
- Backward compatible: existing configs carry no `tags`, so plain jobs stay
  `enabled` and `disabled = true` jobs stay out — identical to prior
  behavior.
- The default-run tag is hardcoded to `enabled`; making it configurable
  (e.g. via `job-defaults`) is a possible later addition.
