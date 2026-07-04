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
- [0007 — Colocate job workspaces so commands get a working git repository](0007-colocate-job-workspaces-for-git-aware-commands.md)
  — Accepted. `jj workspace add` does not colocate (jj#5252), so repoactive
  manually wires up a git worktree in each job workspace. This is for the
  job command's benefit (uv dynamic versioning, `git ls-files`, …), not
  repoactive's own operations; remove it if jj#5252 is fixed.
- [0008 — Configure jobs as a name-keyed table](0008-jobs-keyed-by-name.md)
  — Accepted. Jobs are a `[job.<name>]` table keyed by name, not a `[[job]]`
  array with a `name` field. The name is a job's identity (branch, trailer,
  `depends_on`, merge key), so the key enforces uniqueness structurally and
  drops boilerplate. A breaking change; the old array form is rejected with
  a migration hint.
- [0009 — `create_mr = "unless-superseded"` collapses a dependency chain into one MR](0009-unless-superseded-mr-creation.md)
  — Accepted. A job with this value skips its MR when a dependent's MR from
  the same run already contains its changes (dependents are stacked on their
  dependencies), so a chain yields a single MR on the topmost non-empty job.
  Per-run only: MRs from earlier runs neither supersede nor get closed.
- [0010 — Validate the merged config after each source](0010-validate-config-after-each-source.md)
  — Accepted. `load_config` validates the cumulative merge after every
  source, so errors are attributed to the file that introduced them and a
  fragment may only reference jobs defined by earlier-sorted sources —
  forward cross-file `depends_on` is rejected by design.
- [0011 — Configure platforms as a name-keyed table](0011-platforms-keyed-by-name.md)
  — Accepted (supersedes the platform note in 0008). Platforms move from a
  `[[platform]]` array merged by `url` to a `[platform.<name>]` table merged
  by name, so a field is reachable via `--set platform.<name>.<field>` and
  jobs and platforms share one merge helper. A breaking change; the old
  array form is rejected with a migration hint.
- [0012 — Run jobs on fresh commits, then absorb results into existing commits](0012-two-phase-commit-run-then-absorb.md)
  — Accepted. Jobs always run on a fresh commit so a failed command never
  touches an existing branch. Successful results are absorbed back into the
  old commits (in-place mutation, same change-id) so jj auto-rebases
  dependent branches not in this run. The rejected alternative — skip absorb
  and push new commits directly — would lose change-id continuity.
