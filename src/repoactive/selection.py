"""Choose and order the jobs a run operates on.

``JobSelector`` is the entry point: constructed from a config and the requested
names/tags (validating them), its ``select_run_jobs`` resolves an ordered
``JobSelection`` for a repo, folding in unmerged-branch refresh and stacked
successors. ``run_all`` builds the selector before taking the lock (so a bad
request fails early) and calls ``select_run_jobs`` once the repository is prepared.
"""

import logging
from dataclasses import dataclass

from repoactive.config import DEFAULT_TAG, Config, Job
from repoactive.graph import topological_sort
from repoactive.jj import JJ

logger = logging.getLogger(__name__)


class UnknownJobsError(ValueError):
    """Raised when requested job names do not match any configured job."""

    def __init__(self, unknown: frozenset[str]) -> None:
        super().__init__(f"unknown job(s): {', '.join(sorted(unknown))}")


class UnknownTagsError(ValueError):
    """Raised when a requested tag is carried by no configured job.

    A tag only exists as a value on jobs, so a tag matching nothing is
    indistinguishable from a typo; failing loudly keeps a mistyped --tag in a
    crontab from silently running zero jobs.
    """

    def __init__(self, unknown: frozenset[str]) -> None:
        super().__init__(f"unknown tag(s): {', '.join(sorted(unknown))}")


@dataclass
class JobSelection:
    """The outcome of ``select_run_jobs``: the ordered jobs and the force-included subsets.

    ``refreshed`` names the jobs pulled in because they have an unmerged branch
    (empty for explicit selection). It lets the run bypass the cooldown skip for
    those jobs so their branches are rebased (ADR 0003) without a second
    unmerged-branch query.

    ``successors`` names the jobs pulled in because their commits sit above a
    selected job's bookmark (``select_run_jobs``). They exist to be rebuilt
    when the stack below them moves: they bypass their own cooldown, but are
    skipped when every dependency was itself skipped this run — an unchanged
    stack needs no rebuild (see ``_dispatch_job``).
    """

    jobs: list[Job]
    refreshed: frozenset[str]
    successors: frozenset[str] = frozenset()


class JobSelector:
    """Resolves a requested ``(names, tags)`` selection against a config into a run.

    Construction validates the request (``_validate_selection``) so a mistyped job
    name or tag fails before any repository work - run_all builds the selector
    before taking the lock or touching the repo, so the failure comes before any
    state changes and before the undo hint is printed for a run that did nothing.
    ``select_run_jobs`` then produces the ordered ``JobSelection`` for a repo.
    """

    def __init__(
        self,
        *,
        config: Config,
        requested_names: frozenset[str],
        requested_tags: frozenset[str],
    ) -> None:
        self.requested_names = requested_names
        self.requested_tags = requested_tags
        self._all_jobs = topological_sort(
            [job.resolve(config.job_defaults) for job in config.jobs]
        )
        self._all_job_names = frozenset(j.name for j in self._all_jobs)
        self._all_tags = frozenset(t for j in self._all_jobs for t in j.effective_tags())
        self._validate_selection()

    def _include_dependencies(self, preselected: frozenset[str]) -> frozenset[str]:
        """Add the transitive dependencies of every selected job.

        Iterating ``_all_jobs`` in reverse propagates dependencies of dependencies
        in a single pass (relies on topological order).
        """
        selected = set(preselected)
        for j in reversed(self._all_jobs):
            if j.name in selected:
                selected.update(j.depends_on)
        return frozenset(selected)

    def _drop_jobs_with_unselected_deps(self, preselected: frozenset[str]) -> frozenset[str]:
        """Drop from ``preselected`` any job that depends on a job not selected.

        The drop cascades to further dependents because ``_all_jobs`` is topologically
        sorted: a dependency removed earlier is already gone by the time its
        dependent is checked.
        """
        selected = set(preselected)
        for j in self._all_jobs:
            if j.name in selected and any(dep not in selected for dep in j.depends_on):
                print(f"==> [{j.name}] skipped (dependency not in default run)")
                selected.remove(j.name)
        return frozenset(selected)

    def _validate_selection(self) -> None:
        """Reject unknown job names and tags in the request (a tag carried by no job is a typo)."""
        unknown_jobs = self.requested_names - self._all_job_names
        if unknown_jobs:
            raise UnknownJobsError(unknown_jobs)
        unknown_tags = self.requested_tags - self._all_tags
        if unknown_tags:
            raise UnknownTagsError(unknown_tags)

    def select_run_jobs(self, repo: JJ) -> JobSelection:
        """Pick and order the jobs to run.

        Selection is by tag. With no names and no tags this is the default run:
        every job carrying ``DEFAULT_TAG`` (see ``Job.effective_tags``), with a
        job dropped if any dependency is not itself selected, plus any job with
        an unmerged branch (refreshed so its stale branch is rebased on trunk
        now rather than at the job's next run, ADR 0003). A job whose dependency
        is refreshed into the run this way is kept, not dropped - the dependency
        is present, so the job stacks on its fresh output. Naming jobs or passing
        tags is explicit selection: the union of the named jobs and the jobs
        matching any requested tag (``DEFAULT_TAG`` is not implied), with all
        dependencies force-included and no unmerged-branch refresh. Either way,
        jobs whose commits sit in the stack above a selected job's bookmark are
        pulled in as successors so they are rebuilt on the new output (ADR 0012).

        Returns a ``JobSelection`` carrying the ordered jobs and the refreshed
        and successor subsets; the caller reuses ``refreshed`` so a job being
        refreshed bypasses the cooldown skip without a second unmerged-branch
        query.
        """
        # On the bare default run, also refresh jobs with an unmerged branch so a
        # stale branch is rebased on trunk now rather than at the job's next run.
        refresh_names: frozenset[str] = frozenset()
        selected: frozenset[str]

        if self.requested_names or self.requested_tags:
            selected = self.requested_names | {
                j.name for j in self._all_jobs if j.effective_tags() & self.requested_tags
            }
            selected = self._include_dependencies(selected)
        else:
            refresh_names = frozenset(repo.pending_job_names() & self._all_job_names)
            if refresh_names:
                print(f"==> refreshing unmerged branches: {', '.join(sorted(refresh_names))}")
            else:
                print("==> no unmerged branches to refresh")

            selected = frozenset(
                j.name for j in self._all_jobs if DEFAULT_TAG in j.effective_tags()
            ) | self._include_dependencies(refresh_names)
            selected = self._drop_jobs_with_unselected_deps(selected)

        bookmarks = [j.branch_name() for j in self._all_jobs if j.name in selected]

        revset = " | ".join(f"present({b})" for b in bookmarks)
        successor_names = (repo.pending_job_names(revset=revset) & self._all_job_names) - selected
        selected = selected | successor_names
        selected = self._include_dependencies(selected)

        selected_jobs = [j for j in self._all_jobs if j.name in selected]
        logger.debug(
            "selected jobs: %s (requested=%s, tags=%s, refresh=%s, successors=%s)",
            [j.name for j in selected_jobs],
            sorted(self.requested_names),
            sorted(self.requested_tags),
            sorted(refresh_names),
            sorted(successor_names),
        )

        return JobSelection(
            jobs=selected_jobs,
            refreshed=refresh_names,
            successors=frozenset(successor_names),
        )
