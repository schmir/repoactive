# 18. A non-secret `env` map for commands - deferred until a concrete need

Status: Proposed (recommend deferring)

## Context

[ADR 0017](0017-secret-env-redaction.md) adds `secret_env` for the names of
sensitive variables. It deliberately leaves out the mirror case: setting
**static, non-secret values** for a command from config. The obvious shape
is an `env` map on a job and in `[job-defaults]`:

```toml
[job.rewrite-docs]
command = "./llm-rewrite.sh"
env = { LOG_LEVEL = "debug", MODEL = "claude-opus-4-8" }
```

The motivating question is whether repoactive needs this at all. Two facts
bound the value:

- A job's `command` already runs through a shell, so a single-var,
  single-job case is expressible today by inlining
  `LOG_LEVEL=debug ./llm-rewrite.sh`.
- The whole process environment is inherited by every command, so anything
  the operator exports before running repoactive is already visible.

What inlining cannot do is set a value **once in `[job-defaults]`** for many
jobs, and a long `FOO=… BAR=… command` string reads worse than a map. So
`env` is a legibility and shared-defaults convenience, not a capability gap.

## Decision

**Defer.** Do not add `env` until a concrete job needs it - specifically,
until there is a real case for setting a shared non-secret value across
several jobs via `[job-defaults]`, which is the one thing inlining cannot
cover. Adding it speculatively spends config surface, schema, merge
semantics, and README examples on a convenience the shell string already
covers for the common case.

When a concrete need arrives, this is the intended design, recorded so the
follow-up is a small step rather than a fresh debate:

- **Shape:** `env: dict[str, str]` on `Job` and `JobDefaults`, for literal
  non-secret values only.
- **Merge:** `[job-defaults].env` and `job.env` merge per key, the job
  winning on a conflict - the normal defaulted-field inheritance, unlike
  `secret_env`'s union-of-names.
- **Layering:** applied to the command environment _before_ the injected
  `RA_*` variables, so repoactive's own variables always win and `env`
  cannot shadow them.
- **Validation:** reject keys with the reserved `RA_` and `REPOACTIVE_`
  prefixes ([ADR 0016](0016-injected-env-var-prefix.md)); reject a key that
  also appears in the same job's `secret_env`. This keeps the rule clean:
  literal values go in `env`, secret names go in `secret_env`, never the
  reverse.
- **Non-secret only, documented:** `env` values live in the repo's TOML, so
  a secret must never go there - that is exactly what `secret_env` is for.

## Consequences

- No new config surface today; the shell command string remains the way to
  set a one-off variable for a single job.
- If and when `env` lands, `secret_env` (ADR 0017) is its counterpart and
  the two read as one rule - values in `env`, secret names in `secret_env` -
  with the split in inheritance behaviour (`env` inherits to all jobs,
  `secret_env` marks-without-granting) being the one thing to call out.
- Revisit this ADR when a job configuration genuinely wants shared
  non-secret environment at the `[job-defaults]` level; flip it to Accepted
  and implement the design above.
