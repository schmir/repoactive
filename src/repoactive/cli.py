import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

from repoactive.config import (
    Config,
    ConfigError,
    ConfigNotFoundError,
    Job,
    default_config_paths,
    expand_config_paths,
    load_config,
    parse_duration,
)
from repoactive.jj import (
    JJ,
    JJError,
    JJNotFoundError,
    NotAColocatedRepoError,
    NotColocatedGitRepoError,
    require_colocated_repo,
    require_jj_on_path,
)
from repoactive.lock import RunLockHeldError
from repoactive.platforms import (
    NoPlatformConfiguredError,
    PlatformTokenNotSetError,
    get_platform,
)
from repoactive.platforms.base import PlatformError
from repoactive.runner import RunMode, UnknownJobsError, run_all, topological_sort
from repoactive.settings import SettingsError, load_settings
from repoactive.ui import print_undo_hint

# Exit code used when another repoactive run already holds the repository lock,
# kept distinct from the generic failure code (1) so a scheduler can tell
# "already running" apart from "run failed".
LOCK_HELD_EXIT_CODE = 2

app = typer.Typer(no_args_is_help=True)
info_app = typer.Typer(no_args_is_help=True)
app.add_typer(info_app, name="info", help="Show information about the configured jobs.")

_DEFAULT_REPO = Path()

_ConfigOption = Annotated[
    list[Path] | None,
    typer.Option(
        "--config",
        "-c",
        help="Config file or directory of *.toml files; repeat to merge, later files win.",
    ),
]
_RepoOption = Annotated[Path, typer.Option("--repo", "-r", help="Path to the jj repository.")]
_DebugOption = Annotated[bool, typer.Option("--debug", "-d", help="Enable debug logging.")]


def _setup_logging(debug: bool) -> None:
    """Configure logging from --debug or, failing that, REPOACTIVE_LOG_LEVEL."""
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    elif (level := load_settings().log_level) is not None:
        logging.basicConfig(level=level.upper())


class MergeStatus(StrEnum):
    """Filter for ``recent-commits`` by whether a commit has landed in trunk."""

    all = "all"
    merged = "merged"
    unmerged = "unmerged"


def _resolve_config(config_paths: list[Path] | None, repo: Path) -> list[Path]:
    """Use the given config paths, or discover defaults inside ``repo``."""
    return config_paths or default_config_paths(repo)


def _load_config_or_exit(config_paths: list[Path] | None, repo: Path) -> Config:
    """Load the config, or print a clean error and exit non-zero."""
    try:
        return load_config(_resolve_config(config_paths, repo))
    except ConfigNotFoundError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e
    except ConfigError as e:
        _error(f"Invalid config {e}")
        raise typer.Exit(code=1) from e


def _error(message: str) -> None:
    """Print ``message`` to stderr as a bold red ``Error:`` line."""
    typer.secho(f"Error: {message}", err=True, fg=typer.colors.RED, bold=True)


def _check_jj() -> None:
    """Exit with a clear error unless the jj executable is on PATH."""
    try:
        require_jj_on_path()
    except JJNotFoundError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e


def _ensure_colocated_repo(repo: Path) -> None:
    """Ensure ``repo`` is a colocated jj repository root, else exit with a clear error.

    A plain git repository (``.git`` but no ``.jj``) is converted in place by
    running ``jj git init --colocate``; other invalid states exit non-zero.
    """
    try:
        require_colocated_repo(repo)
    except NotColocatedGitRepoError:
        JJ(repo).git_init_colocate()
        abs_repo = repo.resolve()
        print_undo_hint(
            title="To undo",
            body=(
                f"{abs_repo} was a plain git repository; ran 'jj git init --colocate' "
                f"to make it a colocated jj repository.\n"
                f"To undo, remove the jj data:"
            ),
            command=f"rm -rf {abs_repo / '.jj'}",
            style="yellow",
            err=True,
        )
    except NotAColocatedRepoError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(version("repoactive"))
        raise typer.Exit()


@app.callback()
def callback(
    _version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """Script-driven code changes with automated merge requests."""
    # Validate the REPOACTIVE_* environment before any command runs, so a
    # misconfigured variable fails immediately instead of mid-run.
    try:
        load_settings()
    except SettingsError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e


@app.command()
def run(  # noqa: PLR0913
    config_paths: _ConfigOption = None,
    repo: _RepoOption = _DEFAULT_REPO,
    mode: Annotated[
        RunMode,
        typer.Option(
            "--mode",
            "-m",
            help="How far to publish: 'local' (default) applies only to the local repo, "
            "'push' also pushes bookmarks, 'publish' also creates/updates MRs/PRs.",
        ),
    ] = RunMode.local,
    debug: _DebugOption = False,
    tags: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            "-t",
            help="Run jobs carrying any of these tags (repeatable). Default run targets 'enabled'.",
        ),
    ] = None,
    jobs: Annotated[
        list[str] | None,
        typer.Argument(help="Jobs to run (default: all); dependencies are auto-included."),
    ] = None,
) -> None:
    """Apply jobs locally; pass --mode push or --mode publish to publish."""
    _setup_logging(debug)
    _check_jj()
    cfg = _load_config_or_exit(config_paths, repo)
    _ensure_colocated_repo(repo)
    try:
        platform = get_platform(cfg, repo) if mode is RunMode.publish else None
        summary = run_all(
            config=cfg,
            repo_path=repo,
            platform=platform,
            requested_names=jobs or None,
            requested_tags=tags or None,
            mode=mode,
        )
    except RunLockHeldError as e:
        _error(str(e))
        raise typer.Exit(code=LOCK_HELD_EXIT_CODE) from e
    except (
        UnknownJobsError,
        JJError,
        NoPlatformConfiguredError,
        PlatformTokenNotSetError,
        PlatformError,
    ) as e:
        # Anticipated failures (a mistyped job name, no matching platform, an
        # unset or rejected token, a failing jj/git invocation) get a clean
        # error line, not a traceback.
        _error(str(e))
        raise typer.Exit(code=1) from e
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command("validate-config")
def validate_config(
    config_paths: _ConfigOption = None,
    repo: _RepoOption = _DEFAULT_REPO,
    debug: _DebugOption = False,
) -> None:
    """Validate configuration and exit.

    Lists the configuration files used and prints 'Config OK: N job(s)
    defined.' on success (exit 0). Prints the error to stderr and exits with
    code 1 on failure.
    """
    _setup_logging(debug)
    try:
        paths = _resolve_config(config_paths, repo)
        files = expand_config_paths(paths)
        typer.echo("Configuration files:")
        for file in files:
            typer.echo(f"  {file}")
        cfg = load_config(paths)
    except Exception as e:
        _error(f"Invalid config {e}")
        raise typer.Exit(code=1) from e
    typer.echo(f"Config OK: {len(cfg.jobs)} job(s) defined.")


def _format_job_forest(jobs: list[Job]) -> list[tuple[str, Job]]:
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


def _format_job_table(
    rows: list[tuple[str, Job]], widths_from: list[tuple[str, Job]] | None = None
) -> list[str]:
    """Align forest ``rows`` into columns: tree label, title, effective tags.

    Column widths are computed over ``widths_from`` (default: ``rows``), so
    several tables can share one alignment.
    """
    if not rows:
        return []
    widths_from = widths_from or rows
    label_width = max(len(label) for label, _ in widths_from)
    title_width = max(len(job.title) for _, job in widths_from)
    return [
        f"{label:<{label_width}}  {job.title:<{title_width}}  "
        + ", ".join(sorted(job.effective_tags()))
        for label, job in rows
    ]


@info_app.command("jobs")
def info_jobs(
    config_paths: _ConfigOption = None,
    repo: _RepoOption = _DEFAULT_REPO,
    debug: _DebugOption = False,
) -> None:
    """Show all configured jobs as a dependency tree.

    Jobs are printed in topological order, each nested under its depends_on
    targets (once per parent); jobs without dependencies are roots. Each line
    also shows the job's title and effective tags in aligned columns.
    """
    _setup_logging(debug)
    cfg = _load_config_or_exit(config_paths, repo)
    for line in _format_job_table(_format_job_forest(topological_sort(cfg.jobs))):
        typer.echo(line)


@info_app.command("tags")
def info_tags(
    config_paths: _ConfigOption = None,
    repo: _RepoOption = _DEFAULT_REPO,
    debug: _DebugOption = False,
) -> None:
    """List tags with the jobs carrying each tag.

    Jobs are grouped by their effective tags, i.e. the tags driving job
    selection: a job's explicit tags, or the implicit 'enabled'/'disabled'
    tag when it has none. Within each tag, jobs are shown as a dependency
    tree in topological order: a job is nested under its dependencies that
    carry the same tag, and dependencies in other tags leave it at the root.
    Each line also shows the job's title and effective tags in aligned columns.
    """
    _setup_logging(debug)
    cfg = _load_config_or_exit(config_paths, repo)
    jobs_by_tag: dict[str, list[Job]] = {}
    # Sort all jobs at once: a per-tag sort would break on dependencies whose
    # tags differ from the dependent's.
    for job in topological_sort(cfg.jobs):
        for tag in job.effective_tags():
            jobs_by_tag.setdefault(tag, []).append(job)
    forests = {tag: _format_job_forest(jobs) for tag, jobs in jobs_by_tag.items()}
    # Share one alignment across all tag tables.
    all_rows = [row for rows in forests.values() for row in rows]
    for tag in sorted(forests):
        typer.echo(f"{tag}:")
        for line in _format_job_table(forests[tag], all_rows):
            typer.echo(f"  {line}")


@app.command("dump-schema")
def dump_schema(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="File to write the JSON schema to."),
    ],
) -> None:
    """Write the JSON schema of the TOML config to a file."""
    schema = Config.model_json_schema()
    output.write_text(json.dumps(schema, indent=2) + "\n")
    typer.echo(f"Wrote schema to {output}")


@app.command("recent-commits")
def recent_commits(
    within: Annotated[
        str,
        typer.Option(
            "--within",
            help="How far back to look, e.g. '7d', '2w', '24h'. Same format as cooldown_period.",
        ),
    ] = "2w",
    repo: _RepoOption = _DEFAULT_REPO,
    merge_status: Annotated[
        MergeStatus,
        typer.Option("--status", "-s", help="Filter by merge status into trunk."),
    ] = MergeStatus.all,
    jobs: Annotated[
        list[str] | None,
        typer.Argument(help="Job names to filter on (default: all)."),
    ] = None,
    debug: _DebugOption = False,
) -> None:
    """List commits produced by repoactive within a time window.

    Each commit carries a Repoactive-Job trailer written by repoactive. Pass
    one or more job names to narrow the output; omit them to show all jobs.
    By default shows all commits; pass --status merged or --status unmerged to
    filter by whether the commit has landed in trunk.
    """
    _setup_logging(debug)
    _check_jj()
    _ensure_colocated_repo(repo)
    try:
        delta = parse_duration(within)
    except ValueError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e

    match merge_status:
        case MergeStatus.merged:
            revset = "::trunk()"
        case MergeStatus.unmerged:
            revset = "~(::trunk())"
        case MergeStatus.all:
            revset = "all()"

    cutoff = datetime.now(UTC) - delta
    try:
        commits = JJ(repo).recent_job_commits(cutoff, revset=revset)
    except JJError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e

    filter_names = set(jobs) if jobs else None
    shown = [c for c in commits if filter_names is None or (c.job_names & filter_names)]

    if not shown:
        typer.echo("No matching commits found.")
        return

    names_column = [",".join(sorted(c.job_names)) for c in shown]
    id_width = max(len(c.commit_id) for c in shown)
    change_width = max(len(c.change_id) for c in shown)
    name_width = max(len(names) for names in names_column)
    age_width = max(len(c.relative_age) for c in shown)
    for c, names in zip(shown, names_column, strict=True):
        typer.echo(
            f"{c.commit_id:<{id_width}}  {c.change_id:<{change_width}}  "
            f"{names:<{name_width}}  {c.relative_age:<{age_width}}  {c.subject}"
        )


def main() -> None:
    app()
