# 1. No per-job cron `schedule` field

Status: Rejected (decision: do not add a `schedule` field)

## Context

A natural feature request is a per-job cron field ("only run this job on
Sundays"). repoactive already has `cooldown_period` for rate-limiting, so a
cron-style `schedule` looks like a small, complementary addition.

It is not. repoactive's gating is **stateless and derived solely from the
landed trailer**: `cooldown_period` and `recent-commits` work by looking for
a `Repoactive-Job: <name>` trailer on the _base branch_ — i.e. on a commit
that has actually **landed**. repoactive keeps no record of its own runs (no
database, no state file, no marker). This is deliberate: it makes runs
idempotent and lets you point repoactive at any clone.

The hard consequence:

> repoactive cannot distinguish "the command ran and produced no diff" from
> "the command never ran." A run that produces no diff writes no commit and
> no trailer, so it leaves no trace anywhere.

## Decision

Do not add a `schedule` field. It cannot be implemented correctly on top of
the current stateless design, and a plausible-looking implementation is
actively misleading:

- The only available signal is "did a change with this job's trailer land
  since the most recent cron tick?" (`croniter(...).get_prev()` + the
  trailer check). That gates on _landings_, not on _command executions_.
- So the due-state **latches**: once a tick passes with nothing landed, the
  job stays "due" on _every_ subsequent invocation until something lands. A
  Sunday-only job whose command produces no diff (the common case for
  upgrade/sync jobs) would run again on Monday, Tuesday, … — the exact
  opposite of what the field promises. It would bound how often a change
  _lands_, never how often the _command runs_.
- Cron's day-of-week field does **not** exclude days either: repoactive is
  not a daemon, so the day a change actually lands is governed entirely by
  when `repoactive run` is invoked, not by the cron expression.

## Consequences

- The lever that actually bounds command execution is the **invocation
  schedule**: run `repoactive run <job>` from your external cron/CI only
  when you want the command to run.
- **Recommended pattern for "run this job on a schedule":** set
  `disabled = true` on the job so it stays out of the bare `repoactive run`
  (all-jobs) invocations, then have an OS cron job invoke it by name
  (`repoactive run weekly-job`) on the desired cadence. Naming a job
  explicitly overrides `disabled`, so the cron is the sole trigger and the
  command runs exactly when cron fires — once, whether or not it produces a
  diff. This is strictly better than a `schedule` field would have been: the
  gate is real, stateful OS cron rather than schedule state inferred from
  landed commits. Caveat: a daily job that `depends_on` a disabled weekly
  job will be skipped (`dependency disabled`), so don't put a daily job
  downstream of one driven this way.
- A future, correct `schedule` would require persisting last-run state (a
  marker ref/commit/state file) — exactly the statefulness the trailer-based
  design avoids. Revisit this decision only together with that change.
