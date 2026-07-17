# 18. A non-secret `env` map for commands - rejected

Status: Rejected

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

The motivating question is whether repoactive needs this at all. Three facts
bound the value, and together they leave nothing:

- A job's `command` already runs through a shell, so a single-var,
  single-job case is expressible today by inlining
  `LOG_LEVEL=debug ./llm-rewrite.sh`.
- The whole process environment is inherited by every command, so anything
  the operator exports before running repoactive is already visible to every
  job.
- Setting a shared value **once for many jobs** - the one case inlining
  cannot cover - is therefore also covered: export it before invoking
  repoactive (`MODEL=claude-opus-4-8 repoactive run`, or in the CI job's
  environment), and every job's command sees it.

So the one-off case is inlining and the shared case is the ambient
environment. A config-level `env` map adds no capability over either; it
would only be a second, redundant way to spell values that already have a
home. Unlike `secret_env`, it carries no scoping semantics that would
justify the surface - a non-secret literal has nothing to scope.

## Decision

**Reject.** Do not add a non-secret `env` map. The shell command string
covers a one-off variable for a single job, and the process environment
repoactive inherits covers shared values across all jobs. There is no case
`env` would enable that is not already expressible, so it would spend config
surface, schema, merge semantics, and README examples on redundancy.

Secrets remain the exception that earns dedicated config, because they need
scoping and stripping that the ambient environment cannot express - that is
[ADR 0017](0017-secret-env-redaction.md). Non-secret values need neither.

## Consequences

- No `env` field on `Job` or `JobDefaults`. A one-off non-secret variable
  goes in the shell command string (`FOO=bar ./cmd`); a shared non-secret
  value goes in the environment repoactive is launched with.
- `secret_env` (ADR 0017) stands alone rather than as one half of an
  `env`/`secret_env` pair: it exists because secrets need scoping and
  stripping, which the ambient environment cannot provide. Non-secret values
  have no such need, so the asymmetry is intended, not a gap.
- If a future need genuinely cannot be met by the shell string or the launch
  environment, open a fresh ADR that states that need concretely; this
  record is not a placeholder waiting to be flipped to Accepted.
