# Changelog

## 0.2.5 - unreleased

- A new `cooldown_on` field on a job lists the names of broader jobs whose
  recent landing should also count as a cooldown trigger. Once any listed job
  lands, the current job stays quiet for its `cooldown_period` instead of
  opening a redundant MR. `cooldown_on` requires `cooldown_period` to be set;
  unknown names and self-references are rejected at config load time.
- **Bug fix:** a job on cooldown whose branch was still open (unmerged) was
  previously skipped entirely, leaving the branch un-rebased and its MR
  lingering. Cooldown now gates only the start of fresh work; if the job
  already has an open branch it is still refreshed and the empty-diff path
  closes the MR normally.

## 0.2.4 - 2026-07-08

- **Bug fix:** when `auto_merge = true` is set on GitLab and the branch has
  conflicts, repoactive now prints a warning instead of failing the entire
  run. The MR URL is still returned and remaining MR updates continue
  unaffected.
- **Bug fix:** restoring job results into an existing conflicted commit no
  longer fails silently. The previous `jj restore --changes-in` did not work
  against a conflicted destination; the fix switches to `--from`/`--into` so
  content is transferred correctly in all cases.
- Debug log messages for jj sub-commands are less verbose: on success, only
  the sub-command name is logged rather than the full argument list (which
  could include lengthy jj log templates).

## 0.2.3 - 2026-07-08

- Jobs can now set `auto_merge = true` to have their merge request or pull
  request automatically merged when the pipeline or checks pass. The flag is
  also available in `[job-defaults]` to enable it for all jobs at once.
  GitLab uses "merge when pipeline succeeds"; GitHub uses the native
  auto-merge feature (requires PyGithub ≥ 2.9.1).
- When a job is skipped due to its `cooldown_period`, the skip message now
  includes how long ago the job last ran alongside the cooldown period, e.g.
  `==> [uv-lock-upgrade] on cooldown (14d), last run 3d 5h ago, skipped`.
- A Docker image is now publicly available at `ghcr.io/schmir/repoactive`.

## 0.2.2 - 2026-07-06

- A new `run_only_if_changed` field on a job lists the names of other jobs
  that must have produced a diff in the current run for this job to execute.
  If none of the listed jobs changed, the job is skipped rather than run.
  Unknown names in `run_only_if_changed` are rejected at config load time.
- When a job re-runs with unchanged content but a different `title` or
  `commit_title_prefix`, the existing commit's description is now updated to
  reflect the new message. Previously the description was only updated when
  the content itself changed.
- **Bug fix:** merging job config from multiple sources no longer fails when
  one source sets `disabled = true` and another sets `tags` on the same job.
  The conflicting field from the lower-priority source is now dropped so the
  mutual-exclusion rule is still enforced only against what the user
  explicitly wrote in each source. This also means that
  `--set 'job.upgrade-deps.disabled = true'` now works to disable a job that
  carries tags in the base config.
- Newline characters in `title`, `commit_title_prefix`, and
  `mr_title_prefix` are now rejected at config load time with a clear error,
  rather than silently producing malformed commit or MR titles.

## 0.2.1 - 2026-07-05

- **Bug fix:** running with `-mpush` after a previous `-mlocal` run no
  longer leaves a stale remote bookmark when a job produces no diff.
  Previously, the `-mlocal` run deleted the local bookmark but did not push
  the deletion; the subsequent `-mpush` run saw no local bookmark and
  skipped the delete push entirely. The fix checks for a remote-tracking
  bookmark and schedules the deletion push even when the local bookmark is
  already gone.
- **Bug fix:** a failed job command no longer destroys the job's existing
  branch. Previously, `ws.abandon()` on failure removed the bookmark commit
  and left dependent branches orphaned. Jobs now run on a fresh commit; on
  failure only that throwaway commit is discarded, leaving the existing
  branch and all dependents untouched (ADR 0012).
- When a job re-runs and already has a branch, repoactive now preserves the
  existing commit's change-id (absorbing the new result into it) so that
  dependent commits not selected in the current run are automatically kept
  correctly parented by jj. Previously the branch was mutated in place but
  without this guarantee in the failure path.
- When a job is selected, any job whose existing commit is a direct child of
  that job's branch is automatically force-included in the run, keeping the
  whole stack fresh in a single pass without manual selection.
- **Bug fix:** a timeout was falsely reported when a command exited cleanly
  just as the watchdog fired. A false kill of an already-exited process left
  a non-zero returncode that was misread as a timeout; the check now
  requires the command to still be running at the moment of the watchdog's
  poll.
- `timeout = "0s"` in a job now means _no timeout_, letting a job opt out of
  a timeout set in `job-defaults` (TOML has no null literal).
- **Bug fix:** a generator could emit a job whose name matched a disabled or
  untagged config job, silently overwriting its branch. The collision check
  now covers all configured jobs, not only selected ones.
- Setting `generated_by` in a `[job.<name>]` config table is now rejected
  with a clear error; the field is set by repoactive internally on
  generator-emitted jobs and is not user-facing.

## 0.2.0 - 2026-07-03

- **Breaking:** platforms are now configured as name-keyed tables
  (`[platform.<name>]`) instead of a `[[platform]]` array; an old
  `[[platform]]` array is rejected with a clear error. The name is a label
  (platforms are still matched to a repository by `url`), and it makes a
  single field reachable from the command line, e.g.
  `--set 'platform.github.token_env = "MY_TOKEN"'`.
- A new `--set NAME=VALUE`/`-s` option overrides individual config values on
  the command line without editing a file. `NAME` is a (dotted) TOML key and
  `VALUE` a TOML expression; the override is merged as the last,
  highest-priority source. Repeatable and available on `run`,
  `validate-config`, `info jobs` and `info tags`.
- A new `info` subcommand inspects the configured jobs:
  `repoactive info jobs` shows all jobs as a dependency tree in topological
  order, and `repoactive info tags` groups jobs by effective tag.
- `run` now opens with the selected jobs rendered as the same dependency
  tree `info jobs` shows. The trees printed by `run`, `info jobs` and
  `info tags` are colorized when stdout is a terminal.
- `run --tag` now fails with a clean error when a requested tag is carried
  by no configured job, exactly like naming an unknown job — previously a
  mistyped tag silently selected zero jobs and exited successfully.
- Setting `REPOACTIVE_UI=noninteractive` now turns off the "how to undo"
  hint panels, e.g. for unattended CI runs.
- Log output is now rendered with rich. Set `REPOACTIVE_LOG_HANDLER=plain`
  to get the plain stdlib handler back (e.g. when the output is collected by
  a log aggregator); `REPOACTIVE_UI=noninteractive` also implies the plain
  handler when `REPOACTIVE_LOG_HANDLER` is unset.
- The log level can now be set from the environment with
  `REPOACTIVE_LOG_LEVEL` (`debug`, `info`, `warning`, `error` or `critical`,
  case-insensitive); an explicit `--debug` takes precedence.
- A failing `jj git init --colocate` (run when pointing repoactive at a
  plain git repository) is now reported as a clean error line instead of a
  traceback.
- `validate-config` now reports a missing configuration with the same
  message as `run`, instead of wrapping it in "invalid config".
- A non-integer `REPOACTIVE_PROGRESS_LINES` value now fails at startup with
  a one-line error naming the variable (like the other `REPOACTIVE_*`
  variables), instead of being silently ignored.
- The Docker image's default jj version is now 0.43.0.

## 0.1.1 - 2026-07-02

- `create_mr = "unless-superseded"` is now supported: such a job skips its
  MR/PR when a dependent job's MR from the same run already contains its
  changes, so a dependency chain yields a single MR on the topmost job that
  produced a diff, falling back to the job below when the jobs above came up
  empty (ADR 0009).
- Anticipated failures — a mistyped job name, no platform matching the git
  remote, an unset or rejected platform token, an inaccessible repository,
  or a failing jj/git invocation — are now reported as a clean error line
  instead of a traceback.
- An existing MR/PR is now retargeted when a job's `base_branch` changes;
  previously GitLab silently kept the MR on the old target branch and GitHub
  opened a duplicate PR next to the stale one.
- A failing MR create/update is now reported cleanly instead of crashing the
  run: the failure is recorded for the job, the remaining MR updates are
  listed as not attempted (the next run re-attempts them; branches are
  pushed regardless), and the run report still prints before the non-zero
  exit.
- A dependency cycle among generator-emitted jobs is now rejected with a
  clear error naming the generator, instead of crashing the run.
- `--debug` is now also available on `validate-config` and `recent-commits`.

## 0.1.0 - 2026-06-27

- Jobs are now configured as a `[job.<name>]` table keyed by name instead of
  a `[[job]]` array with a `name` field; generator fragments use the same
  form.
- Dynamic job generation with scripts is now supported (ADR 0004).
- A per-dependency `upgrade-deps` generator is now available as an example
  implementation of a job generator.
- A live view of command output is shown while a job is running.
- Simultaneous runs against the same repository are now prevented with a
  per-repository lock.
- Command output is now rendered as a boxquote in commit messages.
- The "how to undo" hints are rendered as rich panels.
- Error messages are printed in a consistent error style.
- Configuration is loaded before the repository is colocated.
- Platform tokens are stripped from job command environments.
- Configuration validation errors now report the offending file.
