# repoactive - Script-driven code changes with automated merge requests

> **Warning:** This project is in an early stage of development. Use at your
> own risk.

`repoactive` runs your scripts against a git repository and optionally keeps
the corresponding merge requests up to date. You write the scripts that
produce the code changes; `repoactive` handles the rest - branches, commits,
and (with `--create-prs`) the full MR lifecycle.

## How it works

You configure one or more **jobs**, each with a script (any shell command or
executable) that modifies the repository's working tree. `repoactive` runs
each script, captures the resulting diff, and records the change locally.
With `--create-prs` it also:

- opens a new merge request if one does not already exist for that job, or
- updates the existing merge request branch if the diff has changed.

Branches and MR descriptions are managed automatically - the only code you
need to write is the script that produces the change.

```
[your script] → diff → repoactive → branch
                                       ↓ (with --push or --create-prs)
                                    git push → merge request
                                                    ↑ (with --create-prs)
                                            (create or update)
```

1. `repoactive` creates a new commit on top of the base branch or on top of
   other repoactive managed branches.
2. It runs the job's script against the working tree.
3. If the script produced a diff, it records the change. With `--push` or
   `--create-prs`, it pushes the branch; with `--create-prs`, it also
   creates or updates the merge request.
4. If the script produced no diff, the branch is reset to the base. With
   `--push` or `--create-prs`, the reset branch is pushed without opening or
   updating an MR.

## Use cases

- Keeping generated files (API clients, protobuf bindings, lock files) in
  sync with their sources
- Applying organization-wide refactors or policy changes across many
  repositories
- Automating any periodic code transformation that should go through a
  review process

## Configuration

`repoactive` is configured via `.repoactive.toml` in the repository root (or
passed via `--config`). See [Config file locations](#config-file-locations)
for how the defaults are discovered and how to split the config across
several files.

Every key in `[job-defaults]` supplies the default for the matching per-job
key; any job may override it by setting the same key in its `[[job]]` block.

```toml
[job-defaults]
# Prefix prepended to job.name to form the branch name
branch_prefix = "repoactive/"
# Prefix prepended to every MR/PR title (set to "" to disable)
mr_title_prefix = "[repoactive] "
# Prefix prepended to every commit title (set to "" to disable)
commit_title_prefix = "[repoactive] "
# Labels applied to every MR/PR unless overridden per job
labels = ["repoactive"]
# Optional: default target branch for jobs that do not set their own
# (default: repo default branch)
base_branch = "main"
# Optional: default cooldown_period applied to jobs that do not set their own
# (default: none). See "Throttling jobs with cooldown_period" below.
cooldown_period = "7d"

[[job]]
# Unique identifier - branch name is always <branch_prefix><name>
name = "regenerate-api-client"
# Script run in the repo working directory; non-zero exit = failure
command = "python scripts/regen_api.py"
# MR/PR title
title = "api: regenerate API client"
# Optional: MR description
description = "Automated regeneration of the API client from the OpenAPI spec."
# Optional: extra labels (merged with job-defaults.labels)
labels = ["automated", "api"]
# Optional: target branch (default: repo default branch)
base_branch = "main"
# Optional: open the MR/PR as a draft (default: false)
draft = false
# Optional: create an MR/PR for this job (default: true). Set to false to
# push the branch without opening an MR/PR.
create_mr = true
# Optional: append the job's command and its output to the commit message
# (default: true). Set to false to keep the commit message clean.
output_in_commit = true
# Optional: skip this job on "run all" invocations (default: false). Sugar for
# tags = ["disabled"]; mutually exclusive with tags. See "Disabling jobs" below.
disabled = false
# Optional: tags for job selection (default: none -> the job carries the
# implicit "enabled" tag and runs in the bare `repoactive run`). Setting any tag
# removes "enabled", so the job runs only via `repoactive run --tag <tag>`. See
# "Selecting jobs with tags" below.
# tags = ["nightly"]
# Optional: override branch_prefix/mr_title_prefix/commit_title_prefix from
# job-defaults for this job only.
mr_title_prefix = "[api] "
# Optional: minimum time between landed changes for this job. If a commit
# from this job landed on the base branch within this window, the job is
# skipped. Format: <number><unit>, unit one of s, m, h, d, w (e.g. "7d").
cooldown_period = "7d"

[[job]]
name = "sync-license-headers"
command = "./scripts/add_license_headers.sh"
title = "sync license headers"

[[job]]
name = "integration-tests-update"
command = "./scripts/update_integration_tests.py"
title = "tests: update integration tests"
# Optional: run this job on top of the merged output of the listed jobs
depends_on = ["regenerate-api-client", "sync-license-headers"]
```

For public GitHub.com or GitLab.com repositories no platform declaration is
needed — `repoactive` detects the remote URL automatically. To use a
self-hosted instance, add a `[[platform]]` section:

```toml
[[platform]]
# Base URL of the platform instance
url = "https://gitlab.example.com"
# Name of the env var holding the API token
token_env = "GITLAB_TOKEN"
# type must be either "github" or "gitlab"
type = "gitlab"
```

The branch for each job is always `branch_prefix + job.name`, where
`branch_prefix` is the job's own value if set, otherwise
`job-defaults.branch_prefix`. Secrets are kept out of the config file by
referencing environment variable names rather than inline values.

When `depends_on` is set, `repoactive` starts the job's script from a
working tree that has all listed dependency branches merged together, rather
than from the plain base branch. The resulting MR branch will therefore
include both the dependency jobs and the new job on top. Links to the parent
MRs are automatically added to the MR description.

### Config file locations

When no `--config`/`-c` option is given, `repoactive` looks for
configuration inside the `--repo` directory (the current directory by
default):

- the `.repoactive.d/` directory, if present, contributes every `*.toml`
  file it contains, merged in sorted filename order;
- the `.repoactive.toml` file, if present, is merged last so it overrides
  the directory.

If neither exists, `repoactive` exits with an error. Splitting configuration
across `.repoactive.d/*.toml` is handy for dropping in per-job files without
touching a single large config.

`--config`/`-c` overrides this discovery. It may point at a file or at a
directory of `*.toml` files, and may be repeated to merge several sources;
later sources win. Explicit paths are resolved relative to the current
directory, not `--repo`.

## Selecting jobs with tags

Which jobs a `repoactive run` touches is decided by **tags**. Every job has
a set of tags, with a smart default:

- a plain job (no `tags`, not `disabled`) carries the implicit `enabled`
  tag;
- `disabled = true` is sugar for `tags = ["disabled"]`;
- setting `tags = [...]` uses exactly those tags — and, importantly, **drops
  the implicit `enabled` tag** unless you list it yourself.

`repoactive run` with no arguments is shorthand for
`repoactive run --tag enabled`: it runs every job carrying `enabled`. Pass
`--tag`/`-t` (repeatable) to select a different set; a job runs if it
carries **any** of the requested tags. Naming jobs and passing tags can be
combined — the selection is the union of the two.

```bash
# Run all jobs tagged "weekly" (regardless of whether they also have "enabled")
repoactive run --tag weekly

# Union: the "nightly" jobs plus one named job
repoactive run --tag nightly regenerate-api-client
```

Because assigning a tag removes the implicit `enabled` tag, **tags are
load-bearing, not free-form labels**: tagging a job takes it out of the bare
`repoactive run`. If you want a job to stay in the default run _and_ belong
to a group, list both: `tags = ["enabled", "weekly"]`. (For MR/PR labels,
use `labels` — a separate concept.)

Tag selection is _explicit_ selection, so — like naming a job — it ignores
the `enabled`/`disabled` defaults and force-includes dependencies. The bare
`repoactive run` is _implicit_ selection: a job whose dependency is not
itself selected is dropped
(`==> [name] skipped (dependency not in default run)`).

## Disabling jobs

Set `disabled = true` on a `[[job]]` to keep it in the config but leave it
out of normal runs; it is exactly sugar for `tags = ["disabled"]` (so the
two are mutually exclusive). The flag only affects the bare
`repoactive run`:

- On `repoactive run`, disabled jobs are skipped, along with any job that
  `depends_on` one (its dependency would not be produced).
- Naming a job explicitly overrides it: `repoactive run my-job` runs
  `my-job` even when it is disabled. So does
  `repoactive run --tag disabled`, which runs everything currently turned
  off.

### Running a job on a schedule

`repoactive` is not a daemon and has no built-in scheduler — the cadence of
a job is whatever cadence you invoke it with. To run a job on a fixed
schedule, tag it and have an OS cron job select that tag. The tag keeps the
job out of the bare `repoactive run`, and the crontab decides the membership
in one place — add or remove `weekly` jobs by editing the config, not the
crontab:

```toml
[[job]]
name = "uv-lock-upgrade"
command = "uv lock --upgrade"
title = "build: upgrade all dependencies"
# Not in the bare `repoactive run`; runs only via `--tag weekly`.
tags = ["weekly"]
```

```cron
# Run every job tagged "weekly" each Sunday at 03:00
0 3 * * 0  repoactive run --tag weekly --create-prs
```

Because the cron is the sole trigger, the command runs exactly when cron
fires — once, whether or not it produces a diff. This is more reliable than
inferring a schedule from `repoactive`'s own history: real cron is stateful
and excludes the other days, whereas `repoactive` only ever sees what has
_landed_ (see `cooldown_period` below).

## Throttling jobs with `cooldown_period`

Every commit `repoactive` creates carries a `Repoactive-Job: <name>` trailer
identifying the job that produced it. When a job sets `cooldown_period`,
`repoactive` looks at the base branch for a commit with that job's trailer
and a committer date inside the window before running. If one is found the
job is on cooldown and is skipped for this run (dependents proceed as if it
produced no changes); otherwise the job runs normally. This keeps recurring
jobs - for example a dependency upgrade - from landing more often than the
configured interval.

The signal is the trailer on the base branch, so the cooldown only starts
once a change has _landed_. An open, unmerged MR does not trigger it (the
existing MR keeps being updated as usual). Because the check relies on the
commit trailer reaching the base branch, MRs for throttled jobs must be
merged with a merge commit or rebase - a **squash merge discards the commit
message** and with it the trailer, so the cooldown would never trigger.

## Usage

```bash
# Print the installed version and exit
repoactive --version
```

```
repoactive run [OPTIONS] [JOBS]...
```

Run all configured jobs (or a named subset - dependencies are
auto-included):

```bash
# Apply all jobs locally (no push, no MR creation)
repoactive run

# Apply specific jobs locally
repoactive run regenerate-api-client sync-license-headers

# Run every job carrying a given tag (see "Selecting jobs with tags")
repoactive run --tag weekly

# Push branches to the remote without creating MRs
repoactive run --push

# Push branches and create or update merge requests
repoactive run --create-prs

# Enable debug logging
repoactive run --debug
```

| Option          | Short | Description                                                                                                                  |
| --------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------- |
| `--config PATH` | `-c`  | Config file or directory of `*.toml` files; repeat to merge. Default: `.repoactive.d/` and `.repoactive.toml` under `--repo` |
| `--repo PATH`   | `-r`  | jj repository path (default: `.`)                                                                                            |
| `--push`        |       | Push branches to the remote repository                                                                                       |
| `--create-prs`  |       | Push branches and create or update pull requests                                                                             |
| `--tag TAG`     | `-t`  | Run jobs carrying any of these tags (repeatable). With no tags/jobs the default run targets the `enabled` tag                |
| `--debug`       | `-d`  | Enable debug logging                                                                                                         |

## Inspecting repoactive commits

```
repoactive recent-commits [OPTIONS] [JOBS]...
```

List commits produced by repoactive, filtered by a time window and
optionally by job name or merge status:

```bash
# Show all repoactive commits from the last 2 weeks (default window)
repoactive recent-commits --repo /path/to/repo

# Narrow to a specific window
repoactive recent-commits --within 30d --repo /path/to/repo

# Filter by one or more job names
repoactive recent-commits --within 7d uv-lock-upgrade prek-autoupdate

# Only commits that have landed in trunk
repoactive recent-commits --merged

# Only commits still on open branches
repoactive recent-commits --unmerged
```

| Option        | Short | Description                                            |
| ------------- | ----- | ------------------------------------------------------ |
| `--within`    |       | How far back to look (default: `2w`; e.g. `7d`, `24h`) |
| `--repo PATH` | `-r`  | jj repository path (default: `.`)                      |
| `--merged`    |       | Only commits that are ancestors of trunk               |
| `--unmerged`  |       | Only commits not yet in trunk                          |

### jj revset aliases

To query repoactive commits directly in jj, add these aliases to your
repository config (`jj config set --repo`) or your global config
(`jj config set --user`):

```toml
[revset-aliases]
'repoactive()' = 'description(regex:"(?m)^Repoactive-Job: ")'
'repoactive_merged()' = 'repoactive() & ::trunk()'
'repoactive_unmerged()' = 'repoactive() & ~(::trunk())'
```

Then use them directly in jj:

```bash
jj log -r 'repoactive()'
jj log -r 'repoactive_unmerged()'
jj log -r 'repoactive() & committer_date(after:"2025-01-01")'
jj log -r 'repoactive() & description(regex:"(?m)^Repoactive-Job: uv-lock-upgrade$")'
```

## Validating configuration

```
repoactive validate-config [OPTIONS]
```

Check that a config file is syntactically and semantically valid without
running any jobs:

```bash
# Validate the discovered defaults (.repoactive.d/ and .repoactive.toml)
repoactive validate-config

# Validate a specific config file or directory
repoactive validate-config --config myconfig.toml

# Validate a merged config (same merging rules as `run`)
repoactive validate-config --config base.toml --config override.toml
```

On success the command prints `Config OK: N job(s) defined.` and exits with
code 0. On failure it prints the validation error to stderr and exits with
code 1.

Validation checks include unknown keys, missing required fields, invalid
`depends_on` references, and circular job dependencies.

| Option          | Short | Description                                                                                                                  |
| --------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------- |
| `--config PATH` | `-c`  | Config file or directory of `*.toml` files; repeat to merge. Default: `.repoactive.d/` and `.repoactive.toml` under `--repo` |
| `--repo PATH`   | `-r`  | jj repository path (default: `.`)                                                                                            |

## Requirements

- Python 3.11 or later
- [jj (Jujutsu)](https://github.com/jj-vcs/jj) - `repoactive` uses jj to
  manage branches and commits in the target repository
- A GitLab or GitHub API token exposed via the environment variable named in
  `platform.token_env`
