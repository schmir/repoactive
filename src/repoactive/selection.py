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


def _include_dependencies(jobs: list[Job], preselected: frozenset[str]) -> frozenset[str]:
    """Add the transitive dependencies of every selected job.

    ``jobs`` must be topologically sorted; iterating in reverse propagates
    dependencies of dependencies in a single pass.
    """
    selected = set(preselected)
    for j in reversed(jobs):
        if j.name in selected:
            selected.update(j.depends_on)
    return frozenset(selected)


def _drop_jobs_with_unselected_deps(
    jobs: list[Job], preselected: frozenset[str]
) -> frozenset[str]:
    """Drop from ``preselected`` any job that depends on a job not selected.

    The drop cascades to further dependents because ``jobs`` is topologically
    sorted: a dependency removed earlier is already gone by the time its
    dependent is checked.
    """
    selected = set(preselected)
    for j in jobs:
        if j.name in selected and any(dep not in selected for dep in j.depends_on):
            print(f"==> [{j.name}] skipped (dependency not in default run)")
            selected.remove(j.name)
    return frozenset(selected)


def _select_jobs(
    *,
    jobs: list[Job],
    requested_names: frozenset[str],
    requested_tags: frozenset[str] = frozenset(),
    refresh_names: frozenset[str] = frozenset(),
) -> list[Job]:
    """Return the filtered, topologically sorted jobs to run.

    Selection is by tag. With no names and no tags this is the default run:
    every job carrying ``DEFAULT_TAG`` (see ``Job.effective_tags``), with a job
    dropped if any dependency is not itself selected. Naming jobs or passing
    tags is explicit selection: the union of the named jobs and the jobs
    matching any requested tag (``DEFAULT_TAG`` is not implied), with all
    dependencies force-included. Names and tags are assumed valid - the caller
    (``JobSelector``) validates them.

    ``refresh_names`` names the jobs that currently have an unmerged branch;
    they are force-included regardless of tag, along with their dependencies,
    so the default run keeps unmerged branches rebased on trunk rather than
    waiting for the job's next run.
    """
    jobs = topological_sort(jobs)

    selected: frozenset[str]
    if requested_names or requested_tags:
        selected = requested_names | {j.name for j in jobs if j.effective_tags() & requested_tags}
        selected = _include_dependencies(jobs, selected)
    else:
        selected = frozenset(j.name for j in jobs if DEFAULT_TAG in j.effective_tags())
        selected = _drop_jobs_with_unselected_deps(jobs, selected)

    if refresh_names:
        selected = selected | refresh_names
        selected = _include_dependencies(jobs, selected)

    selected_jobs = [j for j in jobs if j.name in selected]
    logger.debug(
        "selected jobs: %s (requested=%s, tags=%s, refresh=%s)",
        [j.name for j in selected_jobs],
        sorted(requested_names),
        sorted(requested_tags),
        sorted(refresh_names),
    )
    return selected_jobs


@dataclass
class JobSelection:
    """The outcome of ``select_run_jobs``: the ordered jobs and the force-included subsets.

    ``refreshed`` names the jobs pulled in because they have an unmerged branch
    (empty for explicit selection). It lets the run bypass the cooldown skip for
    those jobs so their branches are rebased (ADR 0003) without a second
    unmerged-branch query.

    ``successors`` names the jobs pulled in because their commits sit above a
    selected job's bookmark (``_expand_successors``). They exist to be rebuilt
    when the stack below them moves: they bypass their own cooldown, but are
    skipped when every dependency was itself skipped this run — an unchanged
    stack needs no rebuild (see ``_dispatch_job``).
    """

    jobs: list[Job]
    refreshed: frozenset[str]
    successors: frozenset[str] = frozenset()


def _expand_successors(*, selection: JobSelection, config: Config, repo: JJ) -> JobSelection:
    """Force-include jobs whose commits sit anywhere in the stack above a selected job's bookmark.

    Queries all unmerged descendants of the selected bookmarks in one shot and
    merges any named jobs into the selection. This keeps stacks fresh in one run —
    when A is selected and B (or C, stacked on B) has its last commit above A's
    bookmark, it is included so its result is rebuilt on top of A's new output.
    Whether a successor actually runs is decided at dispatch time: if A turns out
    to be cooldown-skipped, its successors are skipped too (see ``_dispatch_job``).
    See docs/adr/0012-two-phase-commit-run-then-absorb.md.
    """
    known_names = {j.name for j in config.jobs}
    selected_names = {j.name for j in selection.jobs}
    bookmarks = [j.resolve(config.job_defaults).branch_name() for j in selection.jobs]
    revset = " | ".join(f"present({b})" for b in bookmarks)
    successor_names = (repo.pending_job_names(revset=revset) & known_names) - selected_names
    if not successor_names:
        return selection
    # selected_names already contains the refreshed jobs (the first _select_jobs
    # call force-included them), so only the successors need force-including here.
    return JobSelection(
        jobs=_select_jobs(
            jobs=config.jobs,
            requested_names=frozenset(selected_names),
            refresh_names=frozenset(successor_names),
        ),
        refreshed=selection.refreshed,
        successors=frozenset(successor_names),
    )


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
        self._all_job_names = frozenset(j.name for j in self.config.jobs)
        self._all_tags = frozenset(t for j in self.config.jobs for t in j.effective_tags())
        self._validate_selection()

    def _validate_selection(self) -> None:
        """Reject unknown job names and tags in the request (a tag carried by no job is a typo)."""
        unknown_jobs = self.requested_names - self._all_job_names
        if unknown_jobs:
            raise UnknownJobsError(unknown_jobs)
        unknown_tags = self.requested_tags - self._all_tags
        if unknown_tags:
            raise UnknownTagsError(unknown_tags)

    def select_run_jobs(self, repo: JJ) -> JobSelection:
        """Pick and order the jobs to run, accounting for unmerged-branch refresh and successors.

        Returns a ``JobSelection`` carrying the ordered jobs and the refreshed
        subset; the caller reuses ``refreshed`` so a job being refreshed bypasses
        the cooldown skip (ADR 0003) without a second unmerged-branch query.
        """
        # On the bare default run, also refresh jobs with an unmerged branch so a
        # stale branch is rebased on trunk now rather than at the job's next run.
        refresh_names: set[str] = set()
        if not self.requested_names and not self.requested_tags:
            refresh_names = repo.pending_job_names() & self._all_job_names
            if refresh_names:
                print(f"==> refreshing unmerged branches: {', '.join(sorted(refresh_names))}")
            else:
                print("==> no unmerged branches to refresh")

        selected = _select_jobs(
            jobs=self.config.jobs,
            requested_names=self.requested_names,
            requested_tags=self.requested_tags,
            refresh_names=frozenset(refresh_names),
        )

        return _expand_successors(
            selection=JobSelection(jobs=selected, refreshed=frozenset(refresh_names)),
            config=self.config,
            repo=repo,
        )
