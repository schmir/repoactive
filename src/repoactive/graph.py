"""Graph algorithms operating on configured jobs."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repoactive.config import Job


class CircularDependencyError(ValueError):
    """Raised when jobs form a dependency cycle."""

    def __init__(self, name: str) -> None:
        super().__init__(f"circular dependency involving '{name}'")


def detect_dependency_cycle(jobs: Iterable[Job]) -> None:
    """Raise CircularDependencyError if the dependency graph contains a cycle.

    A dependency naming a job outside ``jobs`` is ignored: it belongs to an
    already-validated job set, which cannot depend back into this one (used
    when checking a generator's emitted jobs against the running set).
    """
    deps_by_name = {j.name: j.depends_on for j in jobs}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise CircularDependencyError(name)
        if name in visited:
            return
        visiting.add(name)
        for dep in deps_by_name[name]:
            if dep in deps_by_name:
                visit(dep)
        visiting.discard(name)
        visited.add(name)

    for name in deps_by_name:
        visit(name)


def topological_sort(jobs: list[Job]) -> list[Job]:
    """Order ``jobs`` so every job comes after its dependencies.

    Jobs without an ordering constraint keep their relative input order. All
    ``depends_on`` targets must be present in ``jobs``.
    """
    by_name = {j.name: j for j in jobs}
    visited: set[str] = set()
    result: list[Job] = []

    def visit(job: Job) -> None:
        if job.name in visited:
            return
        visited.add(job.name)
        for dep_name in job.depends_on:
            visit(by_name[dep_name])
        result.append(job)

    for job in jobs:
        visit(job)
    return result
