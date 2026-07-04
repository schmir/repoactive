"""Dependency-tree rendering for configured jobs."""

from dataclasses import dataclass

from rich.text import Text

from repoactive.config import Job
from repoactive.ui import console


@dataclass(frozen=True)
class JobRow:
    """One row in a dependency-forest display: a tree branch prefix and its job."""

    label: str
    job: Job


@dataclass(frozen=True)
class _ColumnRow:
    """A ``JobRow`` rendered into fixed-width, print-ready columns."""

    label: str
    title: str
    tags: str


def format_job_forest(jobs: list[Job]) -> list[JobRow]:
    """Render ``jobs`` as a dependency forest: one (tree label, job) row per line.

    ``jobs`` must be topologically sorted. A job is nested under each of its
    dependencies present in ``jobs`` (so a job with several parents yields one
    row per parent); a job none of whose dependencies are present is a root.
    """
    children: dict[str, list[Job]] = {j.name: [] for j in jobs}
    roots: list[Job] = []
    for job in jobs:
        parents = [name for name in job.depends_on if name in children]
        for parent in parents:
            children[parent].append(job)
        if not parents:
            roots.append(job)

    rows: list[JobRow] = []

    def render(job: Job, prefix: str) -> None:
        kids = children[job.name]
        for kid in kids[:-1]:
            rows.append(JobRow(f"{prefix}├── {kid.name}", kid))
            render(kid, f"{prefix}│   ")
        for kid in kids[-1:]:
            rows.append(JobRow(f"{prefix}└── {kid.name}", kid))
            render(kid, f"{prefix}    ")

    for root in roots:
        rows.append(JobRow(root.name, root))
        render(root, "")
    return rows


def _format_job_columns(
    rows: list[JobRow], widths_from: list[JobRow] | None = None
) -> list[_ColumnRow]:
    """Align forest ``rows`` into padded (tree label, title, effective tags) columns.

    Column widths are computed over ``widths_from`` (default: ``rows``), so
    several tables can share one alignment. Kept as separate columns so callers
    can style them individually.
    """
    if not rows:
        return []
    widths_from = widths_from or rows
    label_width = max(len(r.label) for r in widths_from)
    title_width = max(len(r.job.title) for r in widths_from)
    return [
        _ColumnRow(
            f"{r.label:<{label_width}}",
            f"{r.job.title:<{title_width}}",
            ", ".join(sorted(r.job.effective_tags())),
        )
        for r in rows
    ]


def print_job_table(
    rows: list[JobRow],
    widths_from: list[JobRow] | None = None,
    *,
    indent: str = "",
) -> None:
    """Print forest ``rows`` as aligned, colorized lines on the stdout console.

    One line per row: the tree label in cyan, the title plain, the effective
    tags dimmed. Rich drops the styling when stdout is not a terminal.
    ``widths_from`` shares one alignment across several tables (see
    ``_format_job_columns``).
    """
    for col in _format_job_columns(rows, widths_from):
        line = Text(indent)
        line.append(col.label, style="cyan")
        line.append("  ")
        line.append(col.title)
        line.append("  ")
        line.append(col.tags, style="dim")
        console.print(line, soft_wrap=True)
