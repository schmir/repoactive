"""Shared constants for the repoactive contract between modules.

Kept in a dependency-free leaf module so that both ``config`` (which builds
trailers from a job) and ``jj`` (which matches them in revsets) can import it
without creating an import cycle.
"""

# Trailer key recorded on every repoactive commit so later runs can tell which
# job produced a commit (see JJ.has_recent_job_commit and Job.commit_trailers).
JOB_TRAILER_KEY = "Repoactive-Job"
