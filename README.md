# repoactive - Script-driven code changes with automated merge requests

> **Warning:** This project is in an early stage of development. Use at your
> own risk.

`repoactive` runs your scripts against a git repository and keeps the
corresponding merge requests up to date. You write the scripts that produce
the code changes; `repoactive` handles the rest - branches, commits, and the
full MR lifecycle.

## How it works

You configure one or more **jobs**, each with a script (any shell command or
executable) that modifies the repository's working tree. `repoactive` runs
each script, captures the resulting diff, and then:

- opens a new merge request if one does not already exist for that job, or
- updates the existing merge request branch if the diff has changed.

Branches and MR descriptions are managed automatically - the only code you
need to write is the script that produces the change.

```
[your script] → diff → repoactive → branch → git push → merge request
                                                              ↑
                                                      (create or update)
```

1. `repoactive` creates a new commit on top of the base branch or on top of
   other repoactive managed branches.
2. It runs the job's script against the working tree.
3. If the script produced a diff, it pushes the branch and creates or
   updates the merge request.
4. If the script produced no diff, the branch is reset to the base and
   pushed without opening or updating an MR.

## Use cases

- Keeping generated files (API clients, protobuf bindings, lock files) in
  sync with their sources
- Applying organisation-wide refactors or policy changes across many
  repositories
- Automating any periodic code transformation that should go through a
  review process

## Configuration

`repoactive` is configured via `.repoactive.toml` in the repository root (or
passed via `--config`).

```toml

# [[platform]] can be used to define self-hosted gitlab/github instances.
[[platform]]
# Base URL of the platform instance
url = "https://gitlab.example.com"
# Name of the env var holding the API token
token_env = "GITLAB_TOKEN"
# type must be either "github" or "gitlab"
type = "gitlab"


[defaults]
# Prefix prepended to job.name to form the branch name
branch_prefix = "repoactive/"
# Prefix prepended to every MR/PR title (set to "" to disable)
mr_title_prefix = "[repoactive] "
# Prefix prepended to every commit title (set to "" to disable)
commit_title_prefix = "[repoactive] "
# Labels applied to every MR/PR unless overridden per job
labels = ["repoactive"]

[[job]]
# Unique identifier - branch name is always <branch_prefix><name>
name = "regenerate-api-client"
# Script run in the repo working directory; non-zero exit = failure
command = "python scripts/regen_api.py"
# MR/PR title
title = "api: regenerate API client"
# Optional: MR description
description = "Automated regeneration of the API client from the OpenAPI spec."
# Optional: override labels (merged with defaults.labels)
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

[[job]]
name = "sync-licence-headers"
command = "./scripts/add_licence_headers.sh"
title = "sync license headers"

[[job]]
name = "integration-tests-update"
command = "./scripts/update_integration_tests.py"
title = "tests: update integration tests"
# Optional: run this job on top of the merged output of the listed jobs
depends_on = ["regenerate-api-client", "sync-licence-headers"]
```

The branch for each job is always `defaults.branch_prefix + job.name`.
Secrets are kept out of the config file by referencing environment variable
names rather than inline values.

When `depends_on` is set, `repoactive` starts the job's script from a
working tree that has all listed dependency branches merged together, rather
than from the plain base branch. The resulting MR branch will therefore
include both the dependency jobs and the new job on top. Links to the parent
MRs are automatically added to the MR description.

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
# Run all jobs
repoactive run

# Run specific jobs
repoactive run regenerate-api-client sync-licence-headers

# Push branches without opening MRs
repoactive run --local

# Enable debug logging
repoactive run --debug
```

| Option          | Short | Description                               |
| --------------- | ----- | ----------------------------------------- |
| `--config PATH` | `-c`  | Config file (default: `.repoactive.toml`) |
| `--repo PATH`   | `-r`  | jj repository path (default: `.`)         |
| `--local`       |       | Push branches only, skip MR creation      |
| `--debug`       | `-d`  | Enable debug logging                      |

## Validating configuration

```
repoactive validate-config [OPTIONS]
```

Check that a config file is syntactically and semantically valid without
running any jobs:

```bash
# Validate the default .repoactive.toml
repoactive validate-config

# Validate a specific config file
repoactive validate-config --config myconfig.toml

# Validate a merged config (same merging rules as `run`)
repoactive validate-config --config base.toml --config override.toml
```

On success the command prints `Config OK: N job(s) defined.` and exits with
code 0. On failure it prints the validation error to stderr and exits with
code 1.

Validation checks include unknown keys, missing required fields, invalid
`depends_on` references, and circular job dependencies.

| Option          | Short | Description                                                |
| --------------- | ----- | ---------------------------------------------------------- |
| `--config PATH` | `-c`  | Config file (default: `.repoactive.toml`); repeat to merge |

## Requirements

- Python 3.11 or later
- [jj (Jujutsu)](https://github.com/jj-vcs/jj) - `repoactive` uses jj to
  manage branches and commits in the target repository
- A GitLab or GitHub API token exposed via the environment variable named in
  `platform.token_env`
