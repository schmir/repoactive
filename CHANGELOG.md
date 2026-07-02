# Changelog

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
