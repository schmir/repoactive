# Architecture Decision Records

This directory records significant design decisions for repoactive, one per
file, in [MADR](https://adr.github.io/madr/) style (Context / Decision /
Consequences). Records are numbered sequentially and never deleted; a
superseded decision is marked as such and points to the record that replaces
it.

## Index

- [0001 — No per-job cron `schedule` field](0001-no-schedule-field.md) —
  Rejected. A cron schedule cannot be gated correctly on top of the
  stateless, trailer-based design.
- [0002 — Tag-based job selection](0002-tag-based-job-selection.md) —
  Accepted. Per-job `tags` and a `--tag` selector; the sanctioned answer to
  "run this job on a schedule" (real cron decides _when_, tags decide
  _which_).
- [0003 — Refresh unmerged branches in the default run](0003-refresh-unmerged-branches-in-default-run.md)
  — Accepted. The bare `repoactive run` rebases any job with an unmerged
  branch onto the latest `trunk()`, regardless of tags, so a stale branch
  isn't stuck until the job's next scheduled run.
