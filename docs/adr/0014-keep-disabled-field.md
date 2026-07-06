# 14. Keep the `disabled` field rather than requiring `tags = ["disabled"]`

Status: Accepted

## Context

`disabled = true` is syntactic sugar for `tags = ["disabled"]` (see
[ADR 0002](0002-tag-based-job-selection.md)). Because one implies the other,
the two are mutually exclusive: setting both is a validation error. This
mutual exclusion adds a small but real complexity cost — a dedicated
validator, and a merge-time fix (see below).

The question arose whether to remove `disabled` entirely and tell users to
write `tags = ["disabled"]` directly, eliminating the special case.

An additional complication surfaced when multi-source config merging was
introduced: merging a base that sets `tags` with an override that sets
`disabled = true` (or vice versa) produces a combined job that fails the
mutual-exclusion validator. A targeted fix in `merge_jobs` was required to
strip the base-inherited field when the override introduces the other side —
but only when the override does not itself supply both (so explicit user
errors are still caught).

## Decision

Keep `disabled` as a first-class field.

The primary reason is ergonomics. `disabled = true` reads naturally and is
trivially settable from the command line (`--set job.lint.disabled = true`).
The equivalent tag form requires quoting and array syntax
(`--set 'job.lint.tags = ["disabled"]'`), which is noticeably worse for the
most common override use-case.

The secondary reason is typo safety. The model uses `extra="forbid"`, so a
misspelled field name (`dissabled`, `disable`) is caught immediately with a
clear error. A free-form tag string offers no such protection —
`tags = ["dissabled"]` drops the implicit `enabled` tag and puts the job in
a phantom group nobody ever selects, silently removing it from all runs.

Removing the field would also be a breaking change for all existing configs.

## Consequences

- The mutual-exclusion validator (`disabled` and `tags` cannot both be set)
  remains necessary and must be preserved.
- Multi-source merges must resolve the conflict when one source sets `tags`
  and another sets `disabled = true`. `merge_jobs` strips the base-inherited
  field when the override introduces the other side, but leaves both
  untouched when the override itself sets both (preserving the user-error
  path).
- Any future field that is sugar for a reserved tag should be evaluated
  against the same typo-safety criterion before deciding whether to expose
  it as a dedicated boolean or require the tag form directly.
