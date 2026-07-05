"""Tests for dependency graph topological sorting and cycle detection."""

import pytest

from repoactive.config import Job
from repoactive.graph import CircularDependencyError, detect_dependency_cycle, topological_sort


def _job(name: str, *, depends_on: list[str] | None = None) -> Job:
    return Job(
        name=name,
        command=f"cmd-{name}",
        title=f"Change {name}",
        depends_on=depends_on or [],
    )


class TestDetectDependencyCycle:
    def test_no_cycle(self) -> None:
        detect_dependency_cycle(
            [_job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"])]
        )

    def test_self_loop(self) -> None:
        with pytest.raises(CircularDependencyError):
            detect_dependency_cycle([_job("a", depends_on=["a"])])

    def test_two_node_cycle(self) -> None:
        with pytest.raises(CircularDependencyError):
            detect_dependency_cycle([_job("a", depends_on=["b"]), _job("b", depends_on=["a"])])

    def test_indirect_cycle(self) -> None:
        with pytest.raises(CircularDependencyError):
            detect_dependency_cycle(
                [
                    _job("a", depends_on=["c"]),
                    _job("b", depends_on=["a"]),
                    _job("c", depends_on=["b"]),
                ]
            )

    def test_dep_outside_jobs_is_ignored(self) -> None:
        # "external" is not in jobs — represents an already-validated job
        detect_dependency_cycle([_job("a", depends_on=["external"]), _job("b", depends_on=["a"])])

    def test_diamond_no_cycle(self) -> None:
        detect_dependency_cycle(
            [
                _job("a"),
                _job("b", depends_on=["a"]),
                _job("c", depends_on=["a"]),
                _job("d", depends_on=["b", "c"]),
            ]
        )


class TestTopologicalSort:
    def test_no_deps_preserves_order(self) -> None:
        jobs = [_job("a"), _job("b"), _job("c")]
        assert [j.name for j in topological_sort(jobs)] == ["a", "b", "c"]

    def test_linear_chain(self) -> None:
        a, b, c = _job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"])
        result = [c.name for c in topological_sort([c, b, a])]
        assert result.index("a") < result.index("b") < result.index("c")

    def test_diamond(self) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["a"])
        d = _job("d", depends_on=["b", "c"])
        names = [x.name for x in topological_sort([d, b, c, a])]
        assert names.index("a") < names.index("b")
        assert names.index("a") < names.index("c")
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")
