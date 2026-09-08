# 22. Run a command ad-hoc without configuring a job

Status: Accepted

## Context

Every job has to be written into `.repoactive.d/` or `.repoactive.toml`
before `repoactive run` will touch it, and a repository without either file
fails with "no configuration found". Trying a single command therefore means
writing a config file, running, and deleting the file again.

## Decision

`repoactive run` accepts `--ad-hoc COMMAND`. The option builds one job
internally and runs it as if it had been configured and named on the command
line.

The ad-hoc job is an ordinary config source, merged after the files and
before the `--set` overrides. Configuration that is present still supplies
`[job-defaults]` and `[platform.*]`, and `--set` remains the last word on
every field. Configuration that is absent is no longer an error: with
`--ad-hoc`, the missing default config yields no config paths instead of
`ConfigNotFoundError`. An explicitly passed `--config` path must still
exist.

The job's name is derived from the command: every run of characters a job
name may not contain collapses into a single dash, and the result is
truncated. `just update-flake` becomes `just-update-flake`. The name is
deterministic, so repeating the same ad-hoc command reuses its branch and
its `Repoactive-Job` trailer stays readable. `--ad-hoc-name NAME` overrides
the derived name, for a command whose slug is unwieldy or already taken.

A derived or given name that a configured job already uses is rejected. The
merge would otherwise replace that job's command while keeping its tags,
labels, `depends_on` and cooldown, which is not what either the ad-hoc
command or the configured job asked for.

The job's title names the command it ran, so the commit subject reads
`Run 'just update-flake'`. The ad-hoc job clears `commit_title_prefix`: the
subject already says the command was run by a tool, and the prefix would
only repeat it. The merge request title keeps its prefix, where the marker
still distinguishes repoactive's requests from a human's. A multi-line
command contributes its first line followed by `...`.

`repoactive` selects the ad-hoc job by name, so it runs immediately and
ignores cooldown like any other explicitly named job. Selection stays the
union defined in [ADR 0002](0002-tag-based-job-selection.md): the ad-hoc
job, the jobs named as arguments, and the jobs matching `--tag`. `--ad-hoc`
on its own therefore runs that one job, and it suppresses neither a tag
selection nor a named job.

Only `run` gets the option. The other config-reading commands describe
configuration that exists; an ad-hoc command has nothing to describe beyond
what the invocation already shows.

## Consequences

- `repoactive run --ad-hoc 'just update-flake'` works in a repository that
  has no repoactive configuration at all.
- The ad-hoc job belongs to no config file, so its command gets no
  `RA_CONFIG_SOURCE_DIR`, the same as a command defined by `--set`.
- Two different commands can derive the same name and thus share a branch,
  for example `a/b` and `a-b`. `--ad-hoc-name` separates them.
- A command is a poor branch name. `--ad-hoc` is for one-off work; a command
  worth running repeatedly is worth a `[job.<name>]` block.
- The ad-hoc job takes the configured `[job-defaults]`, including `timeout`,
  which defaults to two minutes.
