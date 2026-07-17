# repoactive - Script-driven code changes with automated merge requests

> **Warning:** This project is in an early stage of development. Use at your
> own risk.

`repoactive` runs your scripts against a git repository and optionally keeps
the corresponding merge requests up to date. You write the scripts that
produce the code changes; `repoactive` handles the rest - branches, commits,
and (with `--mode publish`) the full MR lifecycle.

## Contents

- [Use cases](#use-cases)
- [Installation](#installation)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
  - [Keeping the local clone current](#keeping-the-local-clone-current)
- [Usage](#usage)
  - [Environment variables](#environment-variables)
- [Inspecting repoactive commits](#inspecting-repoactive-commits)
- [Validating configuration](#validating-configuration)
- [Listing jobs](#listing-jobs)
- [Listing tags](#listing-tags)
- [Configuration](#configuration)
  - [`[job-defaults]`](#job-defaults)
  - [`[job.<name>]`](#jobname)
  - [Stacking MRs](#stacking-mrs)
  - [Example](#example)
  - [`[platform.<name>]`](#platformname)
  - [Config file locations](#config-file-locations)
  - [Variables passed to job commands](#variables-passed-to-job-commands)
  - [Passing secrets to commands with `secret_env`](#passing-secrets-to-commands-with-secret_env)
  - [Overriding values on the command line](#overriding-values-on-the-command-line)
- [Selecting jobs with tags](#selecting-jobs-with-tags)
  - [Keeping unmerged branches current](#keeping-unmerged-branches-current)
- [Disabling jobs](#disabling-jobs)
- [Running a job on a schedule](#running-a-job-on-a-schedule)
  - [One run at a time per repository](#one-run-at-a-time-per-repository)
- [Gating jobs with `run_only_if_changed`](#gating-jobs-with-run_only_if_changed)
- [Throttling jobs with `cooldown_period`](#throttling-jobs-with-cooldown_period)
- [Throttling a job when a superset lands with `cooldown_on`](#throttling-a-job-when-a-superset-lands-with-cooldown_on)
- [Limiting job runtime with `timeout`](#limiting-job-runtime-with-timeout)
- [Generating jobs dynamically](#generating-jobs-dynamically)
- [Requirements](#requirements)
- [Appendix](#appendix)
  - [jj revset aliases](#jj-revset-aliases)

## Use cases

- Keeping generated files (API clients, protobuf bindings, lock files) in
  sync with their sources
- Applying organization-wide refactors or policy changes across many
  repositories
- Automating any periodic code transformation that should go through a
  review process

## Installation

`repoactive` is published on [PyPI](https://pypi.org/project/repoactive/).
Install it as a standalone command-line tool with
[uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/):

```bash
uv tool install repoactive
# or
pipx install repoactive
```

`repoactive` drives the [jj (Jujutsu)](https://github.com/jj-vcs/jj) CLI, so
a `jj` binary must be on your `PATH`. Publishing merge requests additionally
needs a GitHub or GitLab API token in the environment. See
[Requirements](#requirements) for the complete list.

## Quick start

1. **Point repoactive at a git repository.** Any local git checkout works;
   if it is not already a colocated jj repository, repoactive runs
   `jj git init --colocate` for you on the first run (and prints how to undo
   that). If you have not used jj before, set the identity it records on
   commits once: `jj config set --user user.name "My Name"` and
   `jj config set --user user.email "me@example.com"`.

2. **Add a minimal `.repoactive.toml`** in the repository root with a single
   job. This one keeps `uv`'s lock file current:

   ```toml
   [job.uv-lock-upgrade]
   command = "uv lock --upgrade"
   title = "build: upgrade dependencies"
   ```

3. **Check the config** without running anything:

   ```bash
   repoactive validate-config
   ```

4. **Run the job locally.** In the default `local` mode nothing is pushed
   and no MR is created - repoactive just records the diff your script
   produced on the branch `repoactive/uv-lock-upgrade` and prints a
   `jj op restore` command to undo the run:

   ```bash
   repoactive run
   ```

5. **Publish it.** Put the API token your platform uses in the environment
   (`GITHUB_TOKEN` for GitHub.com, `GITLAB_TOKEN` for GitLab.com by
   default), fetch the latest base branch, then let repoactive push the
   branch and open - or update - the merge request:

   ```bash
   jj git fetch          # repoactive never fetches on its own
   repoactive run --mode publish
   ```

   From here, re-running `repoactive run --mode publish` on a schedule keeps
   that MR in sync with both your script's output and the base branch. The
   rest of this document covers the full configuration and command surface.

## How it works

You configure one or more **jobs**, each with a script (any shell command or
executable) that modifies the repository's working tree. `repoactive` runs
each script, captures the resulting diff, and records the change locally.
With `--mode publish` it also:

- opens a new merge request if one does not already exist for that job, or
- updates the existing merge request branch if the diff has changed.

Branches and MR descriptions are managed automatically - the only code you
need to write is the script that produces the change.

```
[your script] → diff → repoactive → branch
                                       ↓ (with --mode push or --mode publish)
                                    git push → merge request
                                                    ↑ (with --mode publish)
                                            (create or update)
```

1. `repoactive` creates a new commit on top of the base branch or on top of
   other repoactive managed branches.
2. It runs the job's script against the working tree.
3. If the script produced a diff, it records the change. With `--mode push`
   or `--mode publish`, it pushes the branch; with `--mode publish`, it also
   creates or updates the merge request. On a re-run, if the diff matches
   what is already on the branch, the commit and branch are left untouched -
   avoiding an unnecessary push that would re-trigger CI pipelines. The
   commit is updated (and the branch pushed) only when the diff itself
   changes or when `title`/`commit_title_prefix` changed. Command output
   alone changing does not update the commit. The MR title and description
   are always brought up to date, including any changed command output.
4. If the script produced no diff, the empty commit is discarded and no MR
   is opened. A branch left over from an earlier run that did produce a diff
   is now stale, so it is deleted; with `--mode push` or `--mode publish`,
   the deletion is pushed to the remote.

> **jj commits the whole working tree.** Because `repoactive` uses jj, every
> new file your script creates inside the working directory is added to the
> commit unless it is git-ignored. There is no way to select which
> working-tree changes become part of the commit - they all will. Keep
> `.gitignore` up to date so build artifacts, caches, and other stray files
> your script produces do not end up in the diff.

### Keeping the local clone current

`repoactive` works entirely from the **local** repository view and never
fetches from the remote on its own. Rebasing onto `trunk()`, cooldown
throttling, and the unmerged-branch refresh all read the local `trunk()` /
base branches, so a merge that happened on the remote is invisible until the
local clone advances past it.

**Fetch before each run.** Run `jj git fetch` (or `git fetch --prune`) in
the same cron job or CI pipeline that invokes `repoactive`, before it. If
you do not, jobs rebase onto a stale base and - most importantly -
[`cooldown_period`](#throttling-jobs-with-cooldown_period) never engages,
because the commit that would trigger it has not reached the local base
branch. See
[ADR 0005](docs/adr/0005-local-repository-is-the-source-of-truth.md).

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
repoactive run --mode push

# Push branches and create or update merge requests
repoactive run --mode publish

# Enable debug logging
repoactive run --debug
```

| Option                          | Short | Description                                                                                                                  |
| ------------------------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------- |
| `--config PATH`                 | `-c`  | Config file or directory of `*.toml` files; repeat to merge. Default: `.repoactive.d/` and `.repoactive.toml` under `--repo` |
| `--set NAME=VALUE`              | `-s`  | Override a config value (repeatable); `NAME` is a dotted TOML key, `VALUE` a TOML expression. Wins over `--config`           |
| `--repo PATH`                   | `-r`  | jj repository path (default: `.`)                                                                                            |
| `--mode [local\|push\|publish]` | `-m`  | How far to publish: `local` (default) applies only locally, `push` also pushes branches, `publish` also creates/updates MRs  |
| `--tag TAG`                     | `-t`  | Run jobs carrying any of these tags (repeatable). With no tags/jobs the default run targets the `enabled` tag                |
| `--debug`                       | `-d`  | Enable debug logging                                                                                                         |

### Environment variables

Besides the command-line options, repoactive reads a few `REPOACTIVE_*`
environment variables that tune how it presents itself in the current
environment (as opposed to the [configuration](#configuration), which
describes the jobs to run). They are handled uniformly: all of them go
through a single settings model and are validated together at startup, so a
misconfigured variable fails immediately with a one-line error naming it
instead of surfacing mid-run or being silently ignored. Values are
case-insensitive.

#### `REPOACTIVE_UI`

`interactive` (default) or `noninteractive`.

Every `run` captures the jj operation id beforehand and prints a
`jj op restore <id>` command at the end of the run (last, since a run can
produce a lot of output). Run it to roll the local repository - commits,
bookmarks and colocated git refs - back to the state it was in before the
run. It only undoes local changes: a branch already pushed or an MR already
created by a `--mode push`/`--mode publish` run is not affected, as the hint
panel itself points out.

`noninteractive` suppresses these "how to undo" hint panels. Set it where
nobody is at the keyboard, say an unattended CI job. This is an explicit
switch rather than CI auto-detection, because a CI container someone has
logged in to _is_ interactive.

#### `REPOACTIVE_LOG_LEVEL`

`debug`, `info`, `warning`, `error`, or `critical`; unset by default
(logging off).

Enables logging at that level without passing `--debug` on every invocation;
an explicit `--debug` takes precedence.

#### `REPOACTIVE_LOG_HANDLER`

`rich` or `plain`; defaults to `rich`, or `plain` when `REPOACTIVE_UI` is
`noninteractive`.

How log records are rendered: a colorized column layout via
[rich](https://github.com/Textualize/rich), or the stdlib's plain stream
handler (e.g. when the output is collected by a log aggregator).

#### `REPOACTIVE_PROGRESS_LINES`

An integer (default: `8`).

While a job's command runs in an interactive terminal, repoactive shows a
live, scrolling block of its most recent output lines. The block stays on
screen once the command finishes, with the job's status line printed below
it. `REPOACTIVE_PROGRESS_LINES` sets how many lines that live tail shows;
`0` or less disables the live block entirely. When output is not a terminal
(piped or in CI) the block is disabled and the command's output is left
untouched.

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
repoactive recent-commits --status merged

# Only commits still on open branches
repoactive recent-commits --status unmerged
```

| Option                             | Short | Description                                            |
| ---------------------------------- | ----- | ------------------------------------------------------ |
| `--within`                         |       | How far back to look (default: `2w`; e.g. `7d`, `24h`) |
| `--repo PATH`                      | `-r`  | jj repository path (default: `.`)                      |
| `--status [all\|merged\|unmerged]` | `-s`  | Filter by merge status into trunk (default: `all`)     |
| `--debug`                          | `-d`  | Enable debug logging                                   |

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

The command accepts the same `--config`, `--set`, `--repo`, and `--debug`
options as [`repoactive run`](#usage).

## Listing jobs

```
repoactive info jobs [OPTIONS]
```

Show all configured jobs (including disabled ones) as a dependency tree, in
topological order: each job is nested under its `depends_on` targets (once
per parent, so a job with several dependencies appears several times), and
jobs without dependencies are roots. Each line also shows the job's title
and effective tags in aligned columns:

```bash
repoactive info jobs
```

```
build           Build the project   enabled
├── test        Run the test suite  nightly
│   └── deploy  Deploy to staging   nightly, risky
└── docs        Build the docs      enabled
    └── deploy  Deploy to staging   nightly, risky
```

The command accepts the same `--config`, `--set`, `--repo`, and `--debug`
options as [`repoactive run`](#usage).

## Listing tags

```
repoactive info tags [OPTIONS]
```

Group the configured jobs by tag and print each tag with the jobs carrying
it. Jobs are grouped by their effective tags - the tags driving job
selection: a job's explicit `tags`, or the implicit `enabled`/`disabled` tag
when it has none. Within each tag, jobs are shown as a dependency tree in
topological order: a job is nested under its `depends_on` targets carrying
the same tag (once per parent, so a job with several dependencies appears
several times), while a job whose dependencies all carry other tags stays at
the root. Each line also shows the job's title and effective tags in aligned
columns:

```bash
# List tags from the discovered defaults (.repoactive.d/ and .repoactive.toml)
repoactive info tags

# List tags from a specific config file or directory
repoactive info tags --config myconfig.toml
```

```
enabled:
  uv-lock-upgrade      Upgrade uv.lock         enabled
  └── prek-autoupdate  Autoupdate prek hooks   enabled
nightly:
  benchmark            Run nightly benchmarks  nightly
```

The command accepts the same `--config`, `--set`, `--repo`, and `--debug`
options as [`repoactive run`](#usage).

## Configuration

`repoactive` is configured via `.repoactive.toml` in the repository root (or
passed via `--config`). See [Config file locations](#config-file-locations)
for how the defaults are discovered and how to split the config across
several files.

A minimal config with a single job:

```toml
[job.uv-lock-upgrade]
command = "uv lock --upgrade"
title = "build: upgrade dependencies"
```

Every key in `[job-defaults]` supplies the default for the matching per-job
key; any job may override it by setting the same key in its `[job.<name>]`
block.

### `[job-defaults]`

**MR/PR options:**

- **`labels`** (default: `[]`) - Labels applied to every MR/PR. Per-job
  `labels` are merged with, not replaced by, this list.
- **`auto_merge`** (default: `false`) - When `true`, enable auto-merge on
  every MR/PR so it merges automatically once its pipeline passes. On
  GitHub, the repository must have "Allow auto-merge" enabled in its
  settings.
- **`required_approvals`** (default: none) - Minimum number of approvals
  required before the MR can be merged. **GitLab only** - this sets the
  per-MR approval requirement. GitHub has no per-PR equivalent (required
  approvals are a repository-wide branch protection setting), so a job that
  sets `required_approvals` against a GitHub repository fails with an error.

**Branch and commit options:**

- **`branch_prefix`** (default: `"repoactive/"`) - Prefix prepended to the
  job name to form the branch name. Set to `""` to use the job name alone.
  With an empty prefix the branch name is exactly the job name, so make sure
  no job is named after your base branch (e.g. a job named `main`) - the two
  branches would otherwise collide.
- **`mr_title_prefix`** (default: `"[repoactive] "`) - Prefix prepended to
  every MR/PR title. Set to `""` to disable.
- **`commit_title_prefix`** (default: `"[repoactive] "`) - Prefix prepended
  to every commit title. Set to `""` to disable.
- **`base_branch`** (default: repo default) - Target branch for all jobs
  that do not set their own. May also be a jj revset expression such as
  `trunk()`, `root()`, or a user-defined revset alias.

**Run control:**

- **`cooldown_period`** (default: none) - Minimum time between a landed
  change and the next run for any job. Format: `<number><unit>` where unit
  is `s`, `m`, `h`, `d`, or `w` (e.g. `"7d"`). See
  [Throttling jobs with `cooldown_period`](#throttling-jobs-with-cooldown_period).
- **`timeout`** (default: `"2m"`) - Maximum runtime for a job's command.
  Same format as `cooldown_period`. See
  [Limiting job runtime with `timeout`](#limiting-job-runtime-with-timeout).
- **`shell`** (default: `/bin/sh`) - Interpreter used to run job commands. A
  bare name is resolved on `PATH` (e.g. `"bash"`); an absolute path also
  works. The command runs as `<shell> -c <command>`. Arguments are not
  allowed - the value must be a single binary.

**Secrets:**

- **`secret_env`** (default: `[]`) - Names of environment variables to mark
  as secrets config-wide. Each marked name is stripped from every job
  command's environment, but a job still reads one only by listing it in its
  own `secret_env`; `job-defaults` marks names, it does not grant them. See
  [Passing secrets to commands with `secret_env`](#passing-secrets-to-commands-with-secret_env).

### `[job.<name>]`

The table key is the job's unique name; the branch is always
`branch_prefix + name`. Job names may contain letters, digits, hyphens, and
underscores.

**Required:**

- **`command`** - Shell command (or executable path) run in the repository
  working directory. A non-zero exit is a failure.
- **`title`** - MR/PR title (also the commit subject, after
  `commit_title_prefix`).

**MR/PR options:**

- **`description`** - Body text of the MR/PR.
- **`labels`** (default: `[]`) - Extra labels for this job's MR/PR, merged
  with `job-defaults.labels`.
- **`draft`** (default: `false`) - Open the MR/PR as a draft. On GitHub,
  draft state cannot be changed after creation.
- **`create_mr`** (default: `true`) - Whether to create an MR/PR: `true`
  (always), `false` (push the branch but skip the MR), or
  `"unless-superseded"` (skip when a dependent's MR from the same run
  already contains this job's changes - see [Stacking MRs](#stacking-mrs)).
- **`auto_merge`** (default: inherited from `job-defaults`) - When `true`,
  enable auto-merge on this job's MR/PR.
- **`required_approvals`** (default: inherited from `job-defaults`) -
  Minimum number of approvals required before this job's MR can be merged.
  GitLab only; see `job-defaults.required_approvals`.

**Branch and commit options:**

- **`base_branch`** (default: inherited) - Target branch for this job's
  MR/PR. May also be a jj revset expression such as `trunk()`, `root()`, or
  a user-defined revset alias.
- **`branch_prefix`** (default: inherited) - Override the branch-name prefix
  for this job only.
- **`mr_title_prefix`** (default: inherited) - Override the MR/PR title
  prefix for this job only.
- **`commit_title_prefix`** (default: inherited) - Override the commit title
  prefix for this job only.
- **`output_in_commit`** (default: `true`) - Append the job's command and
  its captured output to the commit message. Set to `false` to keep the
  commit message clean.

**Run control:**

- **`disabled`** (default: `false`) - Exclude this job from the bare
  `repoactive run`. Sugar for `tags = ["disabled"]`; mutually exclusive with
  `tags`. See [Disabling jobs](#disabling-jobs).
- **`tags`** (default: none) - Tags for job selection. A job with no tags
  carries the implicit `enabled` tag and runs in the bare `repoactive run`;
  setting any explicit tag removes `enabled`. See
  [Selecting jobs with tags](#selecting-jobs-with-tags).
- **`depends_on`** (default: `[]`) - Jobs whose output this job builds on.
  See [Stacking MRs](#stacking-mrs).
- **`run_only_if_changed`** (default: `[]`) - Only run this job if at least
  one listed job produced a diff in the current run. See
  [Gating jobs with `run_only_if_changed`](#gating-jobs-with-run_only_if_changed).
- **`cooldown_period`** (default: inherited) - Minimum time between a landed
  change and the next run. See
  [Throttling jobs with `cooldown_period`](#throttling-jobs-with-cooldown_period).
- **`cooldown_on`** (default: `[]`) - Broader jobs that subsume this one; a
  recent landing of any of them also throttles this job. Requires a
  `cooldown_period`. See
  [Throttling a job when a superset lands with `cooldown_on`](#throttling-a-job-when-a-superset-lands-with-cooldown_on).
- **`timeout`** (default: inherited) - Maximum runtime for this job's
  command. Set to `"0s"` to disable the timeout entirely. See
  [Limiting job runtime with `timeout`](#limiting-job-runtime-with-timeout).
- **`shell`** (default: inherited) - Interpreter used to run this job's
  command, overriding `job-defaults.shell`. See
  [`job-defaults.shell`](#job-defaults).
- **`emits_jobs`** (default: `false`) - Generator job: the command writes
  `*.toml` job fragments into `$RA_JOBS_DIR` instead of producing a diff.
  See [Generating jobs dynamically](#generating-jobs-dynamically).

**Secrets:**

- **`secret_env`** (default: `[]`) - Names of environment variables holding
  secrets this job's command may read. A marked name is stripped from every
  command's environment; only a job that lists it here has it injected back
  for its own command. See
  [Passing secrets to commands with `secret_env`](#passing-secrets-to-commands-with-secret_env).

### Stacking MRs

When `depends_on` is set, `repoactive` starts the job's script from a
working tree that already contains all listed dependencies' diffs, rather
than from the plain base branch. The resulting MR branch includes both the
dependency changes and the new job's changes on top, and links to the parent
MRs are automatically added to the MR description.

Because a dependent's MR already contains its dependencies' changes, a
dependency chain normally opens one MR per job that produced a diff. Setting
`create_mr = "unless-superseded"` on the earlier jobs collapses that: such a
job skips its MR when a dependent job produced an MR in the same run, so the
chain yields a single MR on the topmost job that actually changed something

- falling back to the job below it when the jobs above came up empty. The
  branch is still pushed either way. Only the current run counts: a
  dependent that is empty, failed, on cooldown, or not selected does not
  suppress anything. See
  [ADR 0009](docs/adr/0009-unless-superseded-mr-creation.md) for details and
  limitations.

### Example

```toml
[job-defaults]
base_branch = "main"
labels = ["repoactive"]
cooldown_period = "7d"
auto_merge = true
branch_prefix = "bot/"
commit_title_prefix = "[bot] "
timeout = "10m"

[job.regenerate-api-client]
command = "python scripts/regen_api.py"
title = "api: regenerate API client"
description = "Automated regeneration of the API client from the OpenAPI spec."
labels = ["api"]
mr_title_prefix = "[api] "
create_mr = "unless-superseded"

[job.sync-license-headers]
command = "./scripts/add_license_headers.sh"
title = "sync license headers"
draft = true

[job.integration-tests-update]
command = "./scripts/update_integration_tests.py"
title = "tests: update integration tests"
depends_on = ["regenerate-api-client", "sync-license-headers"]
timeout = "30m"
```

### `[platform.<name>]`

For public GitHub.com or GitLab.com repositories no platform declaration is
needed - `repoactive` detects the remote URL automatically. To use a
self-hosted instance, add a `[platform.<name>]` section (the name is a label
of your choosing; platforms are matched to a repository by their `url`):

- **`url`** - Base URL of the platform instance (e.g.
  `"https://gitlab.example.com"`).
- **`type`** - `"github"` or `"gitlab"`.
- **`token_env`** - Name of the environment variable holding the API token.

```toml
[platform.company-gitlab]
url = "https://gitlab.example.com"
token_env = "GITLAB_TOKEN"
type = "gitlab"
```

Secrets are kept out of the config file by referencing environment variable
names rather than inline values. The token named by `token_env` is
**stripped from the environment job commands run in**, so a script cannot
read the credential `repoactive` uses to push and create MRs. A job that
needs its own credential must be given a separate one. `repoactive`
otherwise trusts job commands - they run arbitrary code against the working
tree - so the trust boundary is the config that defines them; see
[ADR 0006](docs/adr/0006-job-commands-are-trusted.md).

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

### Variables passed to job commands

`repoactive` injects a few `RA_`-prefixed environment variables into every
job command's environment. These are distinct from the `REPOACTIVE_`
variables [above](#environment-variables), which configure `repoactive`
itself.

| Variable               | Value                                                                                  | Set when                                                                            |
| ---------------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `RA_JOB_NAME`          | The name of the job the command belongs to.                                            | Always.                                                                             |
| `RA_WORKSPACE_DIR`     | The temporary jj workspace created for the job (also the command's working directory). | Always.                                                                             |
| `RA_JOB_BRANCH`        | The bookmark/branch repoactive uses for the job's output.                              | Always.                                                                             |
| `RA_JOB_BASE_BRANCH`   | The branch the job's MR targets (`base_branch`, or `trunk()` by default).              | Always.                                                                             |
| `RA_CONFIG_SOURCE_DIR` | The directory of the config file that defined the command.                             | The command came from a config file (not a `--set` override or a built-in default). |
| `RA_JOBS_DIR`          | The directory a generator writes its `*.toml` job fragments into.                      | The command is a [generator](#generating-jobs-dynamically) (`emits_jobs`).          |

#### `RA_JOB_NAME`

`RA_JOB_NAME` holds the name of the job the command belongs to. A command
shared by several jobs (for instance one set in `[job-defaults]` or emitted
by a generator) can read it to tell which job is running:

```toml
[job.lint]
command = "./run-check.sh $RA_JOB_NAME"
title = "run the shared check script for this job"
```

#### `RA_WORKSPACE_DIR`

A job's command runs in a temporary jj workspace, which is also its working
directory. `RA_WORKSPACE_DIR` names that directory explicitly, so a command
that changes directory can still find its way back:

```toml
[job.build]
command = "cd subdir && make && cp result $RA_WORKSPACE_DIR/subdir/"
title = "build in a subdirectory"
```

#### `RA_JOB_BRANCH`

`RA_JOB_BRANCH` holds the bookmark (git branch) repoactive uses for the
job's output, e.g. `repoactive/uv-lock-upgrade`. The command runs on a fresh
commit while that bookmark still points at the **previous** run's commit, so
a command can inspect what it produced last time - for example to build on
it or diff against it:

```toml
[job.notes]
command = "jj diff --from $RA_JOB_BRANCH --to @ > /tmp/delta || true; ./make-notes.sh"
title = "regenerate release notes from what changed since last run"
```

The bookmark may not exist yet: a job's first run, a run that produced no
diff, and a generator all leave it unset on the remote, so a command that
reads it should tolerate a missing ref.

#### `RA_JOB_BASE_BRANCH`

`RA_JOB_BASE_BRANCH` holds the branch the job's merge request targets - its
`base_branch`, or `trunk()` when that is unset. A command can use it to look
at only what changed relative to the target, for instance to lint or
reformat just the affected files:

```toml
[job.format]
command = "treefmt $(git diff --name-only $RA_JOB_BASE_BRANCH)"
title = "format files changed since the base branch"
```

This is the branch you _configured_ as the base. When a job stacks on
another via `depends_on`, its commit actually sits on top of that other
job's output, so `RA_JOB_BASE_BRANCH` names the ultimate target of the
stack, not the immediate parent.

#### `RA_CONFIG_SOURCE_DIR`

Because the command runs in that throwaway workspace rather than in the
directory its config lives in, a relative path cannot reach a helper file
kept beside the config. `RA_CONFIG_SOURCE_DIR` bridges that: it holds the
directory of the config file that defined the job's command:

- a job defined in `.repoactive.toml` gets the directory holding it (the
  repo directory, by default);
- a job defined in `.repoactive.d/foo.toml` gets the `.repoactive.d`
  directory.

```toml
[job.fixup]
command = "$RA_CONFIG_SOURCE_DIR/fixup.sh"
title = "run the fixup script kept beside the config"
```

The value is the config file's real location on disk, so it is the same
whether the file was discovered automatically or named with `-c`. A command
whose value came from a `--set` override has no config file, so
`RA_CONFIG_SOURCE_DIR` is unset for it.

### Passing secrets to commands with `secret_env`

A job command inherits `repoactive`'s environment, so a secret exported
before the run (an LLM key, a registry token) reaches it. Left unmanaged
that secret reaches **every** job, whether it needs it or not. `secret_env`
scopes secrets to the jobs that ask for them.

The field separates two things:

- **Marking** - naming a variable in _any_ `secret_env` (a job's or
  `[job-defaults]`') marks it a secret and **removes it from every job
  command's environment**.
- **Granting** - a job reads a marked secret back only by listing it in
  **its own** `secret_env`. `[job-defaults].secret_env` marks names but
  grants to no job.

So a secret is present only in the jobs that name it, never ambient:

```toml
[job-defaults]
secret_env = ["OPENAI_API_KEY"]

[job.rewrite-docs]
command = "./llm-rewrite.sh"
title = "docs: rewrite with the LLM"
secret_env = ["OPENAI_API_KEY"]

[job.format]
command = "treefmt"
title = "format the tree"
```

Here `OPENAI_API_KEY` is marked in `[job-defaults]`, so `format` (which does
not grant it) runs without it in its environment; only `rewrite-docs`, which
lists it in its own `secret_env`, can read it.

A job that grants a secret which is **unset** in `repoactive`'s environment
fails at its start with a clear error, rather than letting the command fail
obscurely later. Secret **values** must never be written in config - a
`.repoactive.toml` is checked into the repo; `secret_env` names variables,
your environment (or your CI's secret store) supplies the values. The `RA_`
and `REPOACTIVE_` prefixes are reserved and rejected.

A [generated job](#generating-jobs-dynamically) may only grant a secret that
the static config already marks; an emitted `secret_env` naming a variable
no static job or `[job-defaults]` marked is rejected. Mark such a secret up
front in `[job-defaults].secret_env` (or on the generator), and the emitted
jobs grant it.

A platform's `token_env` is normally stripped from every command
([the platform token](#platformname) is kept out of jobs). A job that
genuinely needs it can opt back in by naming that variable in its own
`secret_env`; only that job then receives it.

> **Note:** `secret_env` scopes _which jobs_ can read a secret. It does not
> yet redact secret values from a command's captured output, so a command
> that echoes a secret it was granted can still write it into the commit
> message. Avoid printing granted secrets. See
> [ADR 0017](docs/adr/0017-secret-env-redaction.md).

### Overriding values on the command line

`--set NAME=VALUE`/`-s` tweaks individual config values without editing a
file. It is merged as the last, highest-priority source, so it wins over
everything discovered or passed with `--config`. `NAME` is a TOML key -
dotted keys reach into tables - and `VALUE` is a TOML expression, so strings
need quoting:

```bash
# override a job-defaults scalar and disable one job for this run
repoactive run \
  --set 'job-defaults.cooldown_period = "24h"' \
  --set 'job.lint.disabled = true'
```

Platforms are name-keyed tables (`[platform.<name>]`) merged by name, so a
single field is reachable with a dotted key. The built-in defaults define
`github` and `gitlab`, so pointing GitHub.com at a different token
environment variable is just:

```bash
repoactive run --set 'platform.github.token_env = "MY_TOKEN"'
```

A platform name that does not already exist is created as a new platform,
which then needs the full set of fields (`url`, `type`, `token_env`).

`--set` is available on `run`, `validate-config`, `info jobs`, and
`info tags`. Each `--set` is validated as its own source (like a separate
config file), so it can amend existing jobs but cannot introduce a brand-new
job across several flags - put a new job's required fields in one expression
(e.g. `--set 'job.x = {command = "…", title = "…"}'`) or a config file. An
unknown key or malformed value is reported with the offending `--set`
argument named.

## Selecting jobs with tags

Which jobs a `repoactive run` touches is decided by **tags**. Every job has
a set of tags, with a smart default:

- a plain job (no `tags`, not `disabled`) carries the implicit `enabled`
  tag;
- `disabled = true` is sugar for `tags = ["disabled"]`;
- setting `tags = [...]` uses exactly those tags - and, importantly, **drops
  the implicit `enabled` tag** unless you list it yourself.

`repoactive run` with no arguments is shorthand for
`repoactive run --tag enabled`: it runs every job carrying `enabled`. Pass
`--tag`/`-t` (repeatable) to select a different set; a job runs if it
carries **any** of the requested tags. Naming jobs and passing tags can be
combined - the selection is the union of the two.

```bash
# Run all jobs tagged "weekly" (regardless of whether they also have "enabled")
repoactive run --tag weekly

# Union: the "nightly" jobs plus one named job
repoactive run --tag nightly regenerate-api-client
```

Requesting a tag that no job carries is an error, exactly like naming an
unknown job - a typo in a crontab's `--tag` fails loudly instead of silently
running zero jobs. This also means a scheduled entry whose tag has lost its
last member fails until you remove the entry (or re-tag a job); a tag only
exists as a value on jobs, so an empty tag is indistinguishable from a
mistyped one.

Because assigning a tag removes the implicit `enabled` tag, **tags are
load-bearing, not free-form labels**: tagging a job takes it out of the bare
`repoactive run`. If you want a job to stay in the default run _and_ belong
to a group, list both: `tags = ["enabled", "weekly"]`. (For MR/PR labels,
use `labels` - a separate concept.)

Tag selection is _explicit_ selection, so - like naming a job - it ignores
the `enabled`/`disabled` defaults and force-includes dependencies. The bare
`repoactive run` is _implicit_ selection: a job whose dependency is not
itself selected is dropped
(`==> [name] skipped (dependency not in default run)`).

### Keeping unmerged branches current

The bare `repoactive run` additionally refreshes **any job that currently
has an unmerged branch**, regardless of its tags. A branch is "unmerged"
when the job's last commit has not yet landed in `trunk()`; repoactive finds
these via the `Repoactive-Job` trailer on unmerged commits and pulls those
jobs (and their dependencies) into the run, so each branch is rebased on the
latest `trunk()` and the command is re-run against it. (With
`--mode publish` such a branch has an open MR; with a plain `run` or
`--mode push` it is just a branch.)

This means a job's schedule tag governs when a _new_ branch is created,
while the default run keeps an existing branch rebased and current - you
don't have to wait for the next weekly run to resolve a conflict with
`trunk()`. Once the branch lands, its commit becomes an ancestor of
`trunk()`, so the job drops back to its normal tag-driven cadence. (A
disabled job's unmerged branch is refreshed too: it was most likely created
by an explicit run, and letting it drift out of date helps no one.)

## Disabling jobs

Set `disabled = true` on a `[job.<name>]` to keep it in the config but leave
it out of normal runs; it is exactly sugar for `tags = ["disabled"]` (so the
two are mutually exclusive). The flag only affects the bare
`repoactive run`:

```toml
[job.experimental-refactor]
command = "./scripts/refactor.sh"
title = "chore: apply experimental refactor"
disabled = true
```

- On `repoactive run`, disabled jobs are skipped, along with any job that
  `depends_on` one (its dependency would not be produced).
- Naming a job explicitly overrides it: `repoactive run my-job` runs
  `my-job` even when it is disabled. So does
  `repoactive run --tag disabled`, which runs everything currently turned
  off.

## Running a job on a schedule

`repoactive` is not a daemon and has no built-in scheduler - the cadence of
a job is whatever cadence you invoke it with. To run a job on a fixed
schedule, tag it and have an OS cron job select that tag. The tag keeps the
job out of the bare `repoactive run`, and the crontab decides the membership
in one place - add or remove `weekly` jobs by editing the config, not the
crontab:

```toml
[job.uv-lock-upgrade]
command = "uv lock --upgrade"
title = "build: upgrade all dependencies"
# Not in the bare `repoactive run`; runs only via `--tag weekly`.
tags = ["weekly"]
```

```cron
# Run every job tagged "weekly" each Sunday at 03:00
0 3 * * 0  repoactive run --tag weekly --mode publish
```

Because the cron is the sole trigger, the command runs exactly when cron
fires - once, whether or not it produces a diff. This is more reliable than
inferring a schedule from `repoactive`'s own history: real cron is stateful
and excludes the other days, whereas `repoactive` only ever sees what has
_landed_ (see `cooldown_period` below).

### One run at a time per repository

A `repoactive run` takes an exclusive per-repository lock for its duration,
so two runs against the same repository never interleave (and corrupt each
other's branches and temporary workspaces). If a run is started while
another is still in progress - a slow run overrunning the next cron tick,
say - the second one **exits immediately with status code 2** instead of
waiting or racing. That code is distinct from the generic failure code (1),
so a wrapper can treat "already running" as benign:

```cron
0 3 * * 0  repoactive run --tag weekly --mode publish || test $? -eq 2
```

The lock is an advisory `flock` on `.jj/repoactive.lock`; the OS releases it
automatically if a run is killed, so a crashed run never leaves the
repository locked.

## Gating jobs with `run_only_if_changed`

`run_only_if_changed` lets a job declare that it should only run when
specific upstream jobs actually produced a diff. If none of the named jobs
changed anything in the current run, the job is skipped.

```toml
[job.prek-autoupdate]
command = "uvx prek autoupdate"
title = "ci: update pre-commit hooks"
tags = ["weekly"]

[job.prek-run-all]
command = "sh -c 'uvx prek run -a; true'"
title = "ci: apply pre-commit fixes"
depends_on = ["prek-autoupdate"]
run_only_if_changed = ["prek-autoupdate"]
tags = ["weekly"]
```

Here `prek-run-all` is only useful when `prek-autoupdate` changed something:
if `prek-autoupdate` found no hooks to update, running `prek-run-all` would
produce an empty diff anyway. With `run_only_if_changed`, the skip is
explicit and immediate - `prek-run-all` never even starts.

Key behaviour:

- **At least one must have changed.** The job runs if any named job produced
  a diff; it is skipped only when _all_ of them were empty.
- **Missing results count as no diff.** If a named job failed or was itself
  skipped, it is treated as having produced no diff.
- **Dependents are unaffected.** A skipped job records a no-op result (like
  a job on cooldown), so any job that `depends_on` it still runs on the base
  branch - the skip does not cascade.
- **No ordering constraint.** Names in `run_only_if_changed` do not have to
  appear in `depends_on`. Any job that runs before this one in topological
  order can be listed; in practice most usages name a direct dependency, as
  in the example above.

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

The trailer must also be present in the _local_ base branch when the job
runs: `repoactive` does not fetch, so a clone that has not pulled the merge
will not see the cooldown and will re-run the job. See
[Keeping the local clone current](#keeping-the-local-clone-current).

## Throttling a job when a superset lands with `cooldown_on`

Some jobs are strict supersets of others. `uv lock --upgrade` refreshes
every dependency group, so its diff already contains everything
`uv lock --upgrade-group=dev` would produce. Re-running the narrower job
right after the broad one landed just opens a redundant MR.

Listing the broader jobs in `cooldown_on` widens the narrow job's cooldown
check: it is throttled when **its own trailer or any listed job's trailer**
last landed within its `cooldown_period`. So once the full upgrade lands,
the group-scoped job stays quiet for the cooldown window instead of
re-running.

```toml
[job.full-lock]
command = "uv lock --upgrade"
title = "chore: update all dependencies"
cooldown_period = "7d"

[job.dev-lock]
command = "uv lock --upgrade-group=dev"
title = "chore: update dev dependencies"
cooldown_period = "7d"
cooldown_on = ["full-lock"]
```

`cooldown_on` requires a `cooldown_period` on the same job (its own or one
inherited from `[job-defaults]`); without a window there is nothing to
throttle against, so a config that sets one without the other is rejected.
The relationship is one-directional and needs no change to the broad job -
it still writes only its own trailer. The listed names need not match a job
in the current config: they match trailers already landed on the base
branch, which may come from jobs that have since been removed or renamed.

This only suppresses _starting_ a redundant run. A narrow job that already
has an open, unmerged branch is always refreshed regardless of cooldown (see
[Keeping unmerged branches current](#keeping-unmerged-branches-current)), so
its branch is rebased and, if the change is now redundant, self-closes on
the next run.

## Limiting job runtime with `timeout`

A job's `command` can hang or run away. Setting `timeout` caps how long the
command may run; when the limit is reached `repoactive` kills the command's
whole process group - the shell and any child processes it spawned - and the
job fails (its workspace is abandoned, no branch or MR is created). The
command runs in its own session/process group to make this possible. The
value uses the same `<number><unit>` format as `cooldown_period` (e.g.
`"30m"`, `"2h"`). `timeout` may be set per job or in `job-defaults`; a
per-job value overrides the default. The built-in default is `"2m"`; set
`timeout` to a larger value in `job-defaults` for longer-running commands. A
zero duration (`timeout = "0s"`) disables the timeout entirely; since TOML
has no null value, this is how a job opts out of a timeout set in
`job-defaults`.

```toml
[job-defaults]
timeout = "10m"       # raise the built-in 2m default for all jobs

[job.slow-codegen]
command = "./scripts/slow_codegen.sh"
title = "chore: regenerate bindings"
timeout = "30m"       # this job needs extra time

[job.quick-lint]
command = "ruff check ."
title = "chore: fix lint"
timeout = "0s"        # opt out of the timeout entirely
```

## Generating jobs dynamically

Sometimes the useful set of jobs depends on the repository's contents - one
job per package in a monorepo, one per entry in a manifest - and you don't
want to hand-maintain them. A **generator** is an ordinary `[job.<name>]`
with `emits_jobs = true`. Instead of producing a diff, its command writes
one or more `*.toml` job fragments into the directory named by the
`RA_JOBS_DIR` environment variable, and the jobs it emits run in the same
invocation.

```toml
[job.per-package]
command = "./scripts/emit_upgrade_jobs.sh"
title = "discover package upgrade jobs"
emits_jobs = true
# Inherited by every emitted job unless the job overrides it (see below).
tags = ["weekly"]
cooldown_period = "7d"
```

The command writes fragments using the normal `[job.<name>]` syntax, e.g.

```toml
# $RA_JOBS_DIR/pkg-a.toml
[job.upgrade-pkg-a]
command = "uv lock --upgrade --package pkg-a"
title = "build: upgrade pkg-a"
```

Fragments may only contain `[job.<name>]` tables; anything else (e.g.
`[job-defaults]`) fails the generator. To set defaults for the emitted jobs,
set them on the generator itself — they are inherited (see below).

Key points:

- **Selection is unchanged.** A generator is selected like any job (by the
  bare run, by name, or by `--tag`); selecting it runs it _and_ everything
  it emits.
- **Inheritance with override.** Each emitted job inherits the generator's
  `tags`, `cooldown_period`, `base_branch`, `timeout`, `labels`,
  `branch_prefix`/title prefixes, `draft` and `create_mr` unless its own
  fragment sets them. It also defaults to `depends_on = ["<generator>"]`
  (i.e. built flat on `trunk()`); override `depends_on` to a sibling emitted
  job to stack them into an MR chain.
- **Stable names are your responsibility.** Cooldown, branches and the
  `Repoactive-Job` trailer all key on a job's `name`, so derive emitted
  names deterministically from repository state (`upgrade-pkg-a`, not a
  random id).
- **The generator never commits.** Any change its command leaves in the
  working copy is discarded; its only output is the job list.
- **Batch cooldown.** Each emitted commit carries a second `Repoactive-Job`
  trailer with the generator's name, so a `cooldown_period` on the generator
  throttles the whole fan-out: it is skipped until enough time has passed
  since the most recent emitted job landed.

> **The generator's cooldown is a floor for its jobs.** An emitted job is
> only re-run when the generator re-emits it, and the generator is gated by
> the same landed commits (via the shared trailer). So overriding an emitted
> job's `cooldown_period` only matters when you make it **longer** than the
> generator's - then the job stays throttled even after the generator has
> run again. Making it **shorter** has no effect: while the generator is on
> its own cooldown the job is never re-emitted, and by the time the
> generator runs again the job's shorter window has long since elapsed
> (nothing landed for that job during the generator's cooldown). To upgrade
> an individual dependency more often than the batch, lower the generator's
> `cooldown_period`, not the emitted job's.

Emitted jobs may not themselves be generators (no recursion), may not reuse
an existing job's name, and may only `depends_on` jobs that are part of the
same run. See [ADR 0004](docs/adr/0004-job-generators.md) for the full
design.

## Requirements

- Python 3.11 or later
- [jj (Jujutsu)](https://github.com/jj-vcs/jj) - `repoactive` uses jj to
  manage branches and commits in the target repository
- A configured jj user name and email - jj records them as the commit
  author. Set them once with:

  ```bash
  jj config set --user user.name "My Name"
  jj config set --user user.email "me@example.com"
  ```

- A GitLab or GitHub API token exposed via the environment variable named in
  `platform.token_env` (default: `GITHUB_TOKEN` for GitHub.com,
  `GITLAB_TOKEN` for GitLab.com)

## Appendix

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
