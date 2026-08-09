"""Shared constants for the repoactive contract between modules.

Kept in a dependency-free leaf module so that both config (which builds
trailers from a job) and jj (which matches them in revsets) can import it
without creating an import cycle.
"""

# Trailer key recorded on every repoactive commit so later runs can tell which
# job produced a commit (see JJ.has_recent_job_commit and Job.commit_trailers).
JOB_TRAILER_KEY = "Repoactive-Job"

# Environment variables repoactive injects into a job command (see
# Job.injected_env and docs/adr/0016-injected-env-var-prefix.md).

# Directory a generator (emits_jobs) command writes its *.toml job fragments
# into. See docs/adr/0004-job-generators.md.
RA_JOBS_DIR_ENV = "RA_JOBS_DIR"

# Directory of the config source that defined the command (Job.config_source_dir),
# so the command can reach files kept beside its config.
RA_CONFIG_SOURCE_DIR_ENV = "RA_CONFIG_SOURCE_DIR"

# Bookmark/branch repoactive uses for the job's output (Job.branch_name). The
# command runs on a fresh commit while this bookmark still points at the previous
# run's commit, so the command can inspect what it produced last time (e.g.
# `git diff $RA_JOB_BRANCH`). The bookmark may not exist yet: a first run, a run
# that produced no diff, or a generator never creates it.
RA_JOB_BRANCH_ENV = "RA_JOB_BRANCH"

# Name of the job the command belongs to (Job.name), so a command shared by
# several jobs can tell which one is running (e.g. to label its output).
RA_JOB_NAME_ENV = "RA_JOB_NAME"

# Branch the job's MR targets (Job.base_branch, or "trunk()" by default), so a
# command can diff against its target (e.g. `git diff $RA_JOB_BASE_BRANCH`). This
# is the configured base; for a stacked job (depends_on) the immediate parent
# commit is another job's output, not this value.
RA_JOB_BASE_BRANCH_ENV = "RA_JOB_BASE_BRANCH"

# Environment variable exposing to a job command the throwaway jj workspace
# repoactive created for it. This is always the command's working directory, but
# naming it explicitly lets a command that changes directory find its way back.
RA_WORKSPACE_DIR_ENV = "RA_WORKSPACE_DIR"
