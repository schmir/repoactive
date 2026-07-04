"""Dependency-tree rendering for configured jobs."""

from rich.text import Text

from repoactive.config import Job
from repoactive.ui import console


def format_job_forest(jobs: list[Job]) -> list[tuple[str, Job]]:
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

    rows: list[tuple[str, Job]] = []

    def render(job: Job, prefix: str) -> None:
        kids = children[job.name]
        for kid in kids[:-1]:
            rows.append((f"{prefix}├── {kid.name}", kid))
            render(kid, f"{prefix}│   ")
        for kid in kids[-1:]:
            rows.append((f"{prefix}└── {kid.name}", kid))
            render(kid, f"{prefix}    ")

    for root in roots:
        rows.append((root.name, root))
        render(root, "")
    return rows


def format_job_columns(
    rows: list[tuple[str, Job]], widths_from: list[tuple[str, Job]] | None = None
) -> list[tuple[str, str, str]]:
    """Align forest ``rows`` into padded (tree label, title, effective tags) columns.

    Column widths are computed over ``widths_from`` (default: ``rows``), so
    several tables can share one alignment. Kept as separate columns so callers
    can style them individually.
    """
    if not rows:
        return []
    widths_from = widths_from or rows
    label_width = max(len(label) for label, _ in widths_from)
    title_width = max(len(job.title) for _, job in widths_from)
    return [
        (
            f"{label:<{label_width}}",
            f"{job.title:<{title_width}}",
            ", ".join(sorted(job.effective_tags())),
        )
        for label, job in rows
    ]


def print_job_table(
    rows: list[tuple[str, Job]],
    widths_from: list[tuple[str, Job]] | None = None,
    *,
    indent: str = "",
) -> None:
    """Print forest ``rows`` as aligned, colorized lines on the stdout console.

    One line per row: the tree label in cyan, the title plain, the effective
    tags dimmed. Rich drops the styling when stdout is not a terminal.
    ``widths_from`` shares one alignment across several tables (see
    ``format_job_columns``).
    """
    for label, title, tags in format_job_columns(rows, widths_from):
        line = Text(indent)
        line.append(label, style="cyan")
        line.append("  ")
        line.append(title)
        line.append("  ")
        line.append(tags, style="dim")
        console.print(line, soft_wrap=True)
