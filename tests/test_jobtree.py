"""Tests for job tree rendering."""

from io import StringIO

from rich.console import Console

import repoactive.jobtree as jt
from repoactive.config import Job
from repoactive.jobtree import (
    JobRow,
    _format_job_columns,
    format_job_forest,
    print_job_table,
)


def _job(name: str, *, depends_on: list[str] | None = None, tags: list[str] | None = None) -> Job:
    return Job(
        name=name,
        command=f"cmd-{name}",
        title=f"Title {name}",
        depends_on=depends_on or [],
        tags=tags or [],
    )


class TestFormatJobForest:
    def test_single_root(self) -> None:
        job = _job("a")
        rows = format_job_forest([job])
        assert [(r.label, r.job.name) for r in rows] == [("a", "a")]

    def test_independent_roots(self) -> None:
        jobs = [_job("a"), _job("b"), _job("c")]
        rows = format_job_forest(jobs)
        assert [r.label for r in rows] == ["a", "b", "c"]

    def test_linear_chain(self) -> None:
        a, b, c = _job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"])
        rows = format_job_forest([a, b, c])
        assert [r.label for r in rows] == ["a", "└── b", "    └── c"]

    def test_two_children_uses_branch_connectors(self) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["a"])
        rows = format_job_forest([a, b, c])
        assert [r.label for r in rows] == ["a", "├── b", "└── c"]

    def test_diamond(self) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["a"])
        d = _job("d", depends_on=["b", "c"])
        rows = format_job_forest([a, b, c, d])
        # d appears once under each parent; each parent has only one child so └── is used
        assert [r.label for r in rows] == ["a", "├── b", "│   └── d", "└── c", "    └── d"]

    def test_dep_not_in_jobs_treated_as_root(self) -> None:
        # b depends on "external" which is not in this job list
        b = _job("b", depends_on=["external"])
        rows = format_job_forest([b])
        assert [(r.label, r.job.name) for r in rows] == [("b", "b")]

    def test_jobs_preserved_in_rows(self) -> None:
        a, b = _job("a"), _job("b", depends_on=["a"])
        rows = format_job_forest([a, b])
        assert [r.job.name for r in rows] == ["a", "b"]


class TestFormatJobColumns:
    def test_empty(self) -> None:
        assert _format_job_columns([]) == []

    def test_single_row(self) -> None:
        job = _job("a", tags=["mytag"])
        cols = _format_job_columns(format_job_forest([job]))
        assert len(cols) == 1
        assert cols[0].label == "a"
        assert cols[0].title == "Title a"
        assert cols[0].tags == "mytag"

    def test_labels_padded_to_same_width(self) -> None:
        a, b = _job("a"), _job("b", depends_on=["a"])
        cols = _format_job_columns(format_job_forest([a, b]))
        widths = [len(c.label) for c in cols]
        assert len(set(widths)) == 1

    def test_titles_padded_to_same_width(self) -> None:
        short = _job("x")
        long = Job(name="y", command="cmd", title="A much longer title", depends_on=[])
        cols = _format_job_columns(format_job_forest([short, long]))
        title_widths = [len(c.title) for c in cols]
        assert len(set(title_widths)) == 1

    def test_tags_sorted(self) -> None:
        job = _job("a", tags=["zzz", "aaa", "mmm"])
        cols = _format_job_columns(format_job_forest([job]))
        assert cols[0].tags == "aaa, mmm, zzz"

    def test_default_tag_shown_when_no_explicit_tags(self) -> None:
        job = _job("a")
        cols = _format_job_columns(format_job_forest([job]))
        assert cols[0].tags == "enabled"

    def test_widths_from_overrides_column_sizing(self) -> None:
        narrow = _job("x")
        wide = Job(name="y", command="cmd", title="A much longer title", depends_on=[])
        all_rows = format_job_forest([narrow, wide])
        cols = _format_job_columns(format_job_forest([narrow]), widths_from=all_rows)
        assert len(cols[0].title) == len("A much longer title")


class TestPrintJobTable:
    def _capture(self, rows: list[JobRow], *, indent: str = "") -> str:
        buf = StringIO()
        original = jt.console
        jt.console = Console(file=buf, highlight=False, markup=False)
        try:
            print_job_table(rows, indent=indent)
        finally:
            jt.console = original
        return buf.getvalue()

    def test_output_contains_job_name(self) -> None:
        job = _job("myjob")
        rows = format_job_forest([job])
        out = self._capture(rows)
        assert "myjob" in out

    def test_output_contains_title(self) -> None:
        job = _job("myjob")
        rows = format_job_forest([job])
        out = self._capture(rows)
        assert "Title myjob" in out

    def test_output_contains_tags(self) -> None:
        job = _job("myjob", tags=["special"])
        rows = format_job_forest([job])
        out = self._capture(rows)
        assert "special" in out

    def test_indent_prepended(self) -> None:
        job = _job("x")
        rows = format_job_forest([job])
        out = self._capture(rows, indent="  ")
        assert out.startswith("  ")

    def test_one_line_per_row(self) -> None:
        a, b = _job("a"), _job("b", depends_on=["a"])
        rows = format_job_forest([a, b])
        out = self._capture(rows)
        assert len(out.splitlines()) == len(rows)
