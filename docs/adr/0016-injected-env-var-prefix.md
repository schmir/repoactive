# 16. Environment variables repoactive injects into commands use the `RA_` prefix

Status: Accepted

## Context

repoactive touches the process environment in two opposite directions, and
until now both used the same `REPOACTIVE_` prefix:

- **Variables repoactive reads to configure itself.** `settings.py` binds a
  handful of `REPOACTIVE_*` variables (`REPOACTIVE_UI`,
  `REPOACTIVE_LOG_LEVEL`, `REPOACTIVE_LOG_HANDLER`,
  `REPOACTIVE_PROGRESS_LINES`). These are inputs: the user sets them,
  repoactive reads them.

- **Variables repoactive sets for the commands it runs.** A job's `command`
  runs in a throwaway workspace
  ([ADR 0007](0007-colocate-job-workspaces-for-git-aware-commands.md)), and
  repoactive injects context into that command's environment via
  `runner._command_env`. The first such variable was `REPOACTIVE_JOBS_DIR`,
  the directory a generator writes its `*.toml` fragments into
  ([ADR 0004](0004-job-generators.md)).

Sharing one prefix conflates "config repoactive reads" with "context
repoactive provides," and the ambiguity only grows as more injected
variables appear. The motivating addition is a variable that hands each
command the directory its config was loaded from (`RA_CONFIG_SOURCE_DIR`),
so commands can reference helper files kept beside an out-of-repo config;
adding it under `REPOACTIVE_` would have deepened the confusion.

## Decision

Split the two directions by prefix:

- `REPOACTIVE_` is **reserved for variables repoactive reads to configure
  itself** — the `settings.py` family.
- `RA_` prefixes **variables repoactive injects into job commands**.

`REPOACTIVE_JOBS_DIR` is renamed to `RA_JOBS_DIR` to bring the pre-existing
injected variable under the convention. This is a clean rename with **no
alias**: a breaking change, acceptable pre-1.0 because the only consumers
are generator scripts, which repoactive projects own alongside their config.
A generator that read `REPOACTIVE_JOBS_DIR` must switch to `RA_JOBS_DIR`.

Injected variables:

- `RA_JOBS_DIR` — the directory a generator (`emits_jobs`) writes job
  fragments into (was `REPOACTIVE_JOBS_DIR`).
- `RA_CONFIG_SOURCE_DIR` — the directory of the config source that defined a
  job's command (the change that prompted this convention).

**`RA_CONFIG_SOURCE_DIR` semantics.** A job's command runs in a throwaway
workspace, so it cannot otherwise locate files kept beside its config (a
helper script next to an out-of-repo `.repoactive.toml`). The value is the
**physical directory of the config file that last set the job's command** —
a job in `.repoactive.d/foo.toml` gets `.repoactive.d`, a job in
`.repoactive.toml` gets the repo directory, a job from `/central/prod.toml`
gets `/central`. It is taken from the file's real location, not the path
typed on the command line, so `repoactive run` and
`repoactive run -c .repoactive.d/foo.toml` agree. When the command was last
set by a `--set` override or the built-in defaults there is no file, so the
variable is unset. Generator-emitted jobs inherit the generator's value
(their fragments live in a throwaway temp dir). The directory is carried on
a machinery-set `Job.config_source_dir` field, which — like `generated_by` —
is rejected if written in user config.

**Why `RA_`.** It is short (low noise in a command's shell) and namespaced
against unrelated variables a command already sees (`PATH`, CI variables). A
non-brand prefix such as `JOB_` or `RUN_` was rejected because CI runners
already inject `JOB_*`/`RUN*` variables, and `REPOACTIVE_JOB_` was rejected
because its `JOBS_DIR` companion stutters (`REPOACTIVE_JOB_JOBS_DIR`).

This is orthogonal to the environment hardening in
[ADR 0006](0006-job-commands-are-trusted.md): the platform API token named
by `platform.token_env` is still stripped from every command's environment,
regardless of prefix.

## Consequences

- The environment reads unambiguously: a `REPOACTIVE_*` variable configures
  repoactive; an `RA_*` variable is something repoactive handed to the
  command.
- Breaking: generator scripts reading `REPOACTIVE_JOBS_DIR` break until
  updated to `RA_JOBS_DIR`. There is no compatibility shim, so the failure
  is immediate and legible (the script sees no directory) rather than
  silent.
- New injected variables have an established home and naming rule, so the
  settings namespace stays reserved for genuine settings.
