from io import StringIO

from rich.console import Console

from repoactive.config import Job
from repoactive.jobtree import format_job_columns, format_job_forest, print_job_table


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
        assert [(label, j.name) for label, j in rows] == [("a", "a")]

    def test_independent_roots(self) -> None:
        jobs = [_job("a"), _job("b"), _job("c")]
        rows = format_job_forest(jobs)
        assert [label for label, _ in rows] == ["a", "b", "c"]

    def test_linear_chain(self) -> None:
        a, b, c = _job("a"), _job("b", depends_on=["a"]), _job("c", depends_on=["b"])
        rows = format_job_forest([a, b, c])
        labels = [label for label, _ in rows]
        assert labels == ["a", "└── b", "    └── c"]

    def test_two_children_uses_branch_connectors(self) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["a"])
        rows = format_job_forest([a, b, c])
        labels = [label for label, _ in rows]
        assert labels == ["a", "├── b", "└── c"]

    def test_diamond(self) -> None:
        a = _job("a")
        b = _job("b", depends_on=["a"])
        c = _job("c", depends_on=["a"])
        d = _job("d", depends_on=["b", "c"])
        rows = format_job_forest([a, b, c, d])
        labels = [label for label, _ in rows]
        # d appears once under each parent; each parent has only one child so └── is used
        assert labels == ["a", "├── b", "│   └── d", "└── c", "    └── d"]

    def test_dep_not_in_jobs_treated_as_root(self) -> None:
        # b depends on "external" which is not in this job list
        b = _job("b", depends_on=["external"])
        rows = format_job_forest([b])
        assert [(label, j.name) for label, j in rows] == [("b", "b")]

    def test_jobs_preserved_in_rows(self) -> None:
        a, b = _job("a"), _job("b", depends_on=["a"])
        rows = format_job_forest([a, b])
        assert [j.name for _, j in rows] == ["a", "b"]


class TestFormatJobColumns:
    def test_empty(self) -> None:
        assert format_job_columns([]) == []

    def test_single_row(self) -> None:
        job = _job("a", tags=["mytag"])
        rows = [("a", job)]
        cols = format_job_columns(rows)
        assert len(cols) == 1
        label, title, tags = cols[0]
        assert label == "a"
        assert title == "Title a"
        assert tags == "mytag"

    def test_labels_padded_to_same_width(self) -> None:
        a, b = _job("a"), _job("b", depends_on=["a"])
        rows = [("a", a), ("└── b", b)]
        cols = format_job_columns(rows)
        widths = [len(label) for label, _, _ in cols]
        assert len(set(widths)) == 1

    def test_titles_padded_to_same_width(self) -> None:
        short = _job("x")
        long = Job(name="y", command="cmd", title="A much longer title", depends_on=[])
        rows = [("x", short), ("y", long)]
        cols = format_job_columns(rows)
        title_widths = [len(title) for _, title, _ in cols]
        assert len(set(title_widths)) == 1

    def test_tags_sorted(self) -> None:
        job = _job("a", tags=["zzz", "aaa", "mmm"])
        cols = format_job_columns([("a", job)])
        _, _, tags = cols[0]
        assert tags == "aaa, mmm, zzz"

    def test_default_tag_shown_when_no_explicit_tags(self) -> None:
        job = _job("a")
        cols = format_job_columns([("a", job)])
        _, _, tags = cols[0]
        assert tags == "enabled"

    def test_widths_from_overrides_column_sizing(self) -> None:
        narrow = _job("x")
        wide = Job(name="y", command="cmd", title="A much longer title", depends_on=[])
        narrow_rows = [("x", narrow)]
        wide_rows = [("x", narrow), ("y", wide)]
        cols = format_job_columns(narrow_rows, widths_from=wide_rows)
        _, title, _ = cols[0]
        assert len(title) == len("A much longer title")


class TestPrintJobTable:
    def _capture(self, rows, **kwargs) -> str:
        buf = StringIO()
        import repoactive.jobtree as jt
        original = jt.console
        jt.console = Console(file=buf, highlight=False, markup=False)
        try:
            print_job_table(rows, **kwargs)
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
        assert len(out.splitlines()) == 2
