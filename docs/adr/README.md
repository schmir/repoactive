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
  — Superseded by [0020](0020-rewrite-command-commit-in-place.md). Jobs ran
  on a fresh commit so a failed command never touched an existing branch,
  and successful results were absorbed back into the old commits (in-place
  mutation, same change-id). The change-id continuity requirement it
  established still holds; 0020 keeps it while dropping the fresh commit and
  the absorb phase.
- [0013 — `run_only_if_changed` gates a job on upstream diffs](0013-run-only-if-changed.md)
  — Accepted. A job listing upstream job names in `run_only_if_changed` is
  skipped (with a no-op result, not a block) when none of those jobs
  produced a diff in the current run.
- [0014 — Keep the `disabled` field](0014-keep-disabled-field.md) —
  Accepted. `disabled = true` stays as sugar for the `disabled` tag rather
  than requiring `tags = ["disabled"]`, keeping the common on/off toggle
  legible.
- [0015 — `cooldown_on` throttles a job when a superset lands](0015-cooldown-on.md)
  — Accepted. A job lists broader jobs in `cooldown_on`; its cooldown check
  then also counts a recent landing of any named job, so once a superset
  lands, the narrower job stays quiet for its `cooldown_period` instead of
  opening a redundant MR. A read-only query, not an extra trailer.
- [0016 — Injected environment variables use the `RA_` prefix](0016-injected-env-var-prefix.md)
  — Accepted. `REPOACTIVE_` is reserved for variables repoactive reads to
  configure itself; variables it injects into job commands use `RA_`.
  Renames `REPOACTIVE_JOBS_DIR` → `RA_JOBS_DIR` (breaking, no alias).
- [0017 — Declare command secrets with `secret_env`](0017-secret-env-redaction.md)
  — Accepted (Phase 1 implemented; Phase 2 redaction deferred). A name in
  any `secret_env` is marked a managed secret and stripped from the base
  environment; a job reads it only by granting it in its own `secret_env`,
  while `[job-defaults]` marks names but grants to no job, so a secret is
  never present in a job that did not ask for it. Generalizes the
  platform-token strip of ADR 0006. Redacting secret values from captured
  output is deferred to Phase 2.
- [0018 — A non-secret `env` map for commands](0018-non-secret-env-map.md) —
  Rejected. The mirror of `secret_env` (0017) for static non-secret values
  written in config. Rejected because it adds no capability: the shell
  command string covers a one-off variable for a single job, and the
  environment repoactive is launched with covers shared values across all
  jobs. Unlike secrets, a non-secret literal has nothing to scope, so it
  earns no dedicated config surface.
- [0019 — Preserve human commits on a repoactive branch](0019-preserve-human-commits.md)
  — Accepted. repoactive preserves a human commit by its position relative
  to the `Repoactive-Job` command commit: below it (a prerequisite, reached
  via `jj rebase`) becomes a merge parent alongside `trunk()` and is never
  rebased; above it (a fixup, committed on top) is reapplied on the
  regenerated output. No trailer or keyword for the human. A conflict jj
  cannot push freezes the branch at its last clean state and labels the MR.
  The refresh guarantee (0003) survives via the merge; repoactive stops
  being the branch's sole writer, a departure from 0005. The absorb-based
  "Run ordering" mechanism is superseded by
  [0020](0020-rewrite-command-commit-in-place.md).
- [0020 — Rewrite the command commit in place; no absorb phase](0020-rewrite-command-commit-in-place.md)
  — Accepted (supersedes 0012, replaces 0019's absorb mechanics). `run_job`
  rewrites the command commit in place — rebase it and its fixups onto the
  run's parents, empty it, regenerate the command's output into it — so the
  change-id is preserved and there is nothing to absorb. Failure and
  empty-diff roll back with an op-log restore captured before any mutation
  (safe: the run lock serialises runs and jobs run sequentially). The
  default workspace is reconciled with `jj workspace update-stale`. The
  content-unchanged push-skip is a deferred follow-up.
- [0021 — Read the configuration from a revset with a temporary workspace](0021-read-config-from-a-revset.md)
  — Accepted. `--config-revset` uses `jj new` to merge the selected
  revisions in a temporary jj workspace. The command discovers the
  configuration in that workspace instead of the working copy. The workspace
  exists for the complete command so that `RA_CONFIG_SOURCE_DIR` remains
  valid while jobs run. The command rejects the use of `--config` with
  `--config-revset`. It reports a merge conflict before it reports a related
  TOML parse error.
