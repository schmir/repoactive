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
- [0004 — Job generators (dynamically created jobs)](0004-job-generators.md)
  — Accepted. An `emits_jobs` job writes `*.toml` fragments to a directory;
  the emitted jobs are force-included into the same run, inheriting the
  generator's fields (tags, `depends_on`, `cooldown_period`, …) overridably.
  A dual `Repoactive-Job` trailer gives the generator a meaningful
  `cooldown_period` over the whole fan-out.
- [0005 — The local repository view is the source of truth (no fetch)](0005-local-repository-is-the-source-of-truth.md)
  — Accepted. `repoactive` never fetches; rebasing, cooldown, and
  unmerged-branch detection all read the local `trunk()`. Keeping the clone
  current is the caller's responsibility, and skipping it silently breaks
  cooldown throttling.
- [0006 — Job commands are trusted](0006-job-commands-are-trusted.md) —
  Accepted. A job's `command` runs arbitrary code against the working tree,
  so the trust boundary is the config, not the command. repoactive does not
  sandbox commands, but does strip the platform API token from their
  environment as cheap defence-in-depth (a live credential, unlike repo
  contents, skips the MR review gate).
