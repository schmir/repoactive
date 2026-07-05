# 13. `run_only_if_changed` gates a job on upstream diffs

Status: Accepted

## Context

Some jobs are only useful when a specific upstream job actually produced a
change. In the canonical example, `prek-run-all` (apply pre-commit fixes to
the working tree) only makes sense after `prek-autoupdate` (update hook
versions) produced a diff — if `prek-autoupdate` found nothing to update,
running `prek-run-all` would be a no-op and create a branch with an empty
diff, which repoactive silently discards anyway. There was previously no way
to express this gating in the config; the job ran unconditionally whenever
it was selected.

The `depends_on` field expresses ordering and parent-stacking, but not
conditionality: a job with `depends_on = ["a"]` always runs, regardless of
whether `a` produced a diff. The cooldown mechanism gates on time since the
last landed commit, not on whether an upstream job changed.

## Decision

`Job` gains a `run_only_if_changed` field (a list of job names). A job is
skipped if **none** of the named jobs produced a diff in the current run.
Concretely: if at least one named job has `produced_diff = True` in
`summary.results`, the job runs normally; otherwise it is skipped.

The check runs in `_dispatch_job`, after the blocking-dep guard and before
cooldown, so a job that would be blocked by a failed dependency still fails
fast.

**Runtime semantics:**

- A named job absent from `summary.results` (because it failed or was itself
  skipped) is treated as having produced no diff.
- When skipped, a no-op `JobResult` (`produced_diff=False`) is recorded in
  `summary.results` — exactly as cooldown does — so dependents still compute
  their parents through this job and are not themselves blocked.
- The job name is NOT added to `blocked`, so dependents run unimpeded.

**Validation:**

- All names in `run_only_if_changed` must refer to known jobs in the config
  (enforced by a `Config`-level `model_validator`). An unknown name raises
  `UnknownRunOnlyIfChangedError` at config load time.
- No ordering constraint is implied: `run_only_if_changed` names do not have
  to appear in `depends_on`, and no cycle check is performed on them. In
  practice most usages will name a direct dependency, but gating on any
  earlier-running job is valid.

**Possible enhancement — `run_only_if_empty`:**

A symmetric `run_only_if_empty` field (run only when the named jobs produced
_no_ diff) would cover fallback strategies — e.g. run an aggressive upgrade
only when a conservative one found nothing to change. It is not implemented
yet; add it when a real-world use case arises.

## Consequences

- Jobs that previously ran unconditionally can now be gated on whether
  specific upstream jobs produced changes, avoiding no-op runs.
- Dependents of a `run_only_if_changed`-skipped job are unaffected; they
  still run on the base branch (or the skipped job's parents), preserving
  the existing stacking behaviour.
- The field is orthogonal to `depends_on`: ordering and parent-stacking are
  controlled by `depends_on`; conditionality is controlled by
  `run_only_if_changed`. A typical gated job sets both.
