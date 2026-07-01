# Changelog

## 0.1.1 - 2026-07-02

- `create_mr = "unless-superseded"` is now supported: such a job skips its
  MR/PR when a dependent job's MR from the same run already contains its
  changes, so a dependency chain yields a single MR on the topmost job that
  produced a diff, falling back to the job below when the jobs above came up
  empty (ADR 0009).

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
