"""Command-line interface for repoactive."""

import contextlib
import json
import logging
from collections.abc import Generator
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from rich.logging import RichHandler

from repoactive.config import (
    AdHocJob,
    AdHocNameCollisionError,
    Config,
    ConfigError,
    ConfigNotFoundError,
    EmptyAdHocCommandError,
    Job,
    MissingSecretError,
    default_config_paths,
    expand_config_paths,
    load_config,
    parse_duration,
)
from repoactive.graph import topological_sort
from repoactive.jj import (
    JJ,
    JJError,
    JJNotFoundError,
    JobCommit,
    NotAColocatedRepoError,
    NotColocatedGitRepoError,
    require_colocated_repo,
    require_jj_on_path,
)
from repoactive.jobtree import format_job_forest, print_job_table
from repoactive.lock import RunLockHeldError
from repoactive.platforms import (
    NoPlatformConfiguredError,
    PlatformTokenNotSetError,
    get_platform,
)
from repoactive.platforms.base import PlatformError
from repoactive.runner import (
    RunMode,
    run_all,
)
from repoactive.selection import UnknownJobsError, UnknownTagsError
from repoactive.settings import SettingsError, load_settings
from repoactive.ui import err_console, print_undo_hint

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
_SetOption = Annotated[
    list[str] | None,
    typer.Option(
        "--set",
        "-s",
        help="Override a config value: NAME=VALUE where NAME is a (dotted) TOML key and "
        "VALUE is a TOML expression. Repeatable; wins over --config.",
    ),
]
_ConfigRevsetOption = Annotated[
    str | None,
    typer.Option(
        "--config-revset",
        help="Read configuration from the merged tree of this revset instead of from the "
        "working copy. Cannot be combined with --config.",
    ),
]
_AdHocOption = Annotated[
    str | None,
    typer.Option(
        "--ad-hoc",
        help="Run this shell command as a one-off job, without configuring it. The job is "
        "added to any configuration found and selected like a job named on the command line.",
    ),
]
_AdHocNameOption = Annotated[
    str | None,
    typer.Option(
        "--ad-hoc-name",
        help="Name for the --ad-hoc job, which also names its branch. "
        "Default: derived from the command.",
    ),
]
_RepoOption = Annotated[Path, typer.Option("--repo", "-r", help="Path to the jj repository.")]
_DebugOption = Annotated[bool, typer.Option("--debug", "-d", help="Enable debug logging.")]


def _setup_logging(debug: bool) -> None:
    """Configure logging from --debug or, failing that, REPOACTIVE_LOG_LEVEL.

    Logs render through rich unless REPOACTIVE_LOG_HANDLER=plain selects the
    stdlib's default stream handler.
    """
    settings = load_settings()
    if debug:
        level: int | str = logging.DEBUG
    elif settings.log_level is not None:
        level = settings.log_level.upper()
    else:
        return
    if settings.log_handler == "plain":
        logging.basicConfig(level=level)
    else:
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(console=err_console)],
        )


class MergeStatus(StrEnum):
    """Filter for recent-commits by whether a commit has landed in trunk."""

    all = "all"
    merged = "merged"
    unmerged = "unmerged"


def _resolve_config(
    config_paths: list[Path] | None, repo: Path, *, required: bool = True
) -> list[Path]:
    """Use the given config paths, or discover defaults inside repo.

    With required=False a missing default config yields no paths instead of an
    error: --ad-hoc brings its own job and needs no configuration.
    """
    if config_paths:
        return config_paths
    try:
        return default_config_paths(repo)
    except ConfigNotFoundError:
        if required:
            raise
        return []


def _load_config_or_exit(
    config_paths: list[Path] | None,
    repo: Path,
    overrides: list[str] | None = None,
    ad_hoc: AdHocJob | None = None,
) -> Config:
    """Load the config, or print a clean error and exit non-zero."""
    try:
        paths = _resolve_config(config_paths, repo, required=ad_hoc is None)
        return load_config(paths, overrides=overrides or None, ad_hoc=ad_hoc)
    except ConfigNotFoundError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e
    except (EmptyAdHocCommandError, AdHocNameCollisionError) as e:
        _error(str(e))
        raise typer.Exit(code=1) from e
    except ConfigError as e:
        _error(f"invalid config {e}")
        raise typer.Exit(code=1) from e


def _error(message: str) -> None:
    """Print message to stderr as a bold red Error: line."""
    typer.secho(f"Error: {message}", err=True, fg=typer.colors.RED, bold=True)


def _warn(message: str) -> None:
    """Print message to stderr as a bold yellow Warning: line."""
    typer.secho(f"Warning: {message}", err=True, fg=typer.colors.YELLOW, bold=True)


def _check_jj() -> None:
    """Exit with a clear error unless the jj executable is on PATH."""
    try:
        require_jj_on_path()
    except JJNotFoundError as e:
        _error(str(e))
        raise typer.Exit(code=1) from e


def _ensure_colocated_repo(repo: Path) -> None:
    """Ensure repo is a colocated jj repository root, else exit with a clear error.

    A plain git repository (.git but no .jj) is converted in place by
    running jj git init --colocate; other invalid states exit non-zero.
    """
    try:
        require_colocated_repo(repo)
    except NotColocatedGitRepoError:
        try:
            JJ(repo).git_init_colocate()
        except JJError as e:
            _error(str(e))
            raise typer.Exit(code=1) from e
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


@contextlib.contextmanager
def _config_root(
    repo: Path, config_paths: list[Path] | None, config_revset: str | None
) -> Generator[Path]:
    """Yield the directory to discover configuration in.

    The context lifetime keeps configuration-relative paths valid as required by
    ADR 0021.
    """
    if config_revset is None:
        yield repo
        return
    if config_paths:
        _error("--config-revset cannot be combined with --config")
        raise typer.Exit(code=1)
    _check_jj()
    # Create a colocated jj repository before the workspace. See ADR 0021.
    _ensure_colocated_repo(repo)
    with contextlib.ExitStack() as stack:
        try:
            workspace = stack.enter_context(JJ(repo).revset_workspace(config_revset))
            _warn_about_conflicts(workspace, config_revset)
        except JJError as e:
            # Report an invalid revset without a traceback.
            _error(str(e))
            raise typer.Exit(code=1) from e
        yield workspace.cwd


def _warn_about_conflicts(workspace: JJ, config_revset: str) -> None:
    """Warn about conflicted files, but continue to read the configuration."""
    if not workspace.has_conflict("@"):
        return
    paths = workspace.conflicted_paths()
    _warn(f"merging revset {config_revset!r} produced file conflicts:")
    for path in paths:
        typer.echo(f"  {path}", err=True)


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


def _ad_hoc_job(command: str | None, name: str | None) -> AdHocJob | None:
    """Build the AdHocJob for --ad-hoc, or exit when only --ad-hoc-name was given."""
    if command is None:
        if name is not None:
            _error("--ad-hoc-name requires --ad-hoc")
            raise typer.Exit(code=1)
        return None
    return AdHocJob(command=command, name=name)


@app.command()
def run(  # noqa: PLR0913, PLR0917
    config_paths: _ConfigOption = None,
    config_revset: _ConfigRevsetOption = None,
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
    overrides: _SetOption = None,
    debug: _DebugOption = False,
    ad_hoc_command: _AdHocOption = None,
    ad_hoc_name: _AdHocNameOption = None,
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
    ad_hoc = _ad_hoc_job(ad_hoc_command, ad_hoc_name)
    # An ad-hoc job is requested by name, so it runs at once like any job named
    # on the command line, alongside whatever else was requested.
    requested_names = frozenset(jobs or []) | ({ad_hoc.job_name} if ad_hoc else frozenset())
    # The config workspace is held for the whole run, so a job command can still
    # reach the files RA_CONFIG_SOURCE_DIR points at (ADR 0021).
    with _config_root(repo, config_paths, config_revset) as config_root:
        cfg = _load_config_or_exit(config_paths, config_root, overrides, ad_hoc)
        _ensure_colocated_repo(repo)
        try:
            platform = get_platform(cfg, repo) if mode is RunMode.publish else None
            summary = run_all(
                config=cfg,
                repo_path=repo,
                platform=platform,
                requested_names=requested_names,
                requested_tags=frozenset(tags or []),
                mode=mode,
            )
        except RunLockHeldError as e:
            _error(str(e))
            raise typer.Exit(code=LOCK_HELD_EXIT_CODE) from e
        except (
            UnknownJobsError,
            UnknownTagsError,
            JJError,
            MissingSecretError,
            NoPlatformConfiguredError,
            PlatformTokenNotSetError,
            PlatformError,
        ) as e:
            # Anticipated failures (a mistyped job name or tag, a job granting an
            # unset secret, no matching platform, an unset or rejected token, a
            # failing jj/git invocation) get a clean error line, not a traceback.
            _error(str(e))
            raise typer.Exit(code=1) from e
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command("validate-config")
def validate_config(
    config_paths: _ConfigOption = None,
    config_revset: _ConfigRevsetOption = None,
    repo: _RepoOption = _DEFAULT_REPO,
    overrides: _SetOption = None,
    debug: _DebugOption = False,
) -> None:
    """Validate configuration and exit.

    Lists the configuration files used and prints 'Config OK: N job(s)
    defined.' on success (exit 0). Prints the error to stderr and exits with
    code 1 on failure.
    """
    _setup_logging(debug)
    with _config_root(repo, config_paths, config_revset) as config_root:
        try:
            paths = _resolve_config(config_paths, config_root)
            files = expand_config_paths(paths)
            if config_revset is not None:
                # Say where the listed paths come from; they are inside a
                # temporary workspace that is gone by the time this returns.
                typer.echo(f"Configuration files from revset {config_revset!r}:")
            else:
                typer.echo("Configuration files:")
            for file in files:
                typer.echo(f"  {file}")
            cfg = load_config(paths, overrides=overrides or None)
        except ConfigNotFoundError as e:
            _error(str(e))
            raise typer.Exit(code=1) from e
        except ConfigError as e:
            _error(f"invalid config {e}")
            raise typer.Exit(code=1) from e
    typer.echo(f"Config OK: {len(cfg.jobs)} job(s) defined.")


@info_app.command("jobs")
def info_jobs(
    config_paths: _ConfigOption = None,
    config_revset: _ConfigRevsetOption = None,
    repo: _RepoOption = _DEFAULT_REPO,
    overrides: _SetOption = None,
    debug: _DebugOption = False,
) -> None:
    """Show all configured jobs as a dependency tree.

    Jobs are printed in topological order, each nested under its depends_on
    targets (once per parent); jobs without dependencies are roots. Each line
    also shows the job's title and effective tags in aligned columns.
    """
    _setup_logging(debug)
    with _config_root(repo, config_paths, config_revset) as config_root:
        cfg = _load_config_or_exit(config_paths, config_root, overrides)
    print_job_table(format_job_forest(topological_sort(cfg.jobs)))


@info_app.command("tags")
def info_tags(
    config_paths: _ConfigOption = None,
    config_revset: _ConfigRevsetOption = None,
    repo: _RepoOption = _DEFAULT_REPO,
    overrides: _SetOption = None,
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
    with _config_root(repo, config_paths, config_revset) as config_root:
        cfg = _load_config_or_exit(config_paths, config_root, overrides)
    jobs_by_tag: dict[str, list[Job]] = {}
    # Sort all jobs at once: a per-tag sort would break on dependencies whose
    # tags differ from the dependent's.
    for job in topological_sort(cfg.jobs):
        for tag in job.effective_tags():
            jobs_by_tag.setdefault(tag, []).append(job)
    forests = {tag: format_job_forest(jobs) for tag, jobs in jobs_by_tag.items()}
    # Share one alignment across all tag tables.
    all_rows = [row for rows in forests.values() for row in rows]
    for tag in sorted(forests):
        typer.echo(f"{tag}:")
        print_job_table(forests[tag], all_rows, indent="  ")


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


def _print_commit_table(commits: list[JobCommit]) -> None:
    """Print commits as columns padded to their widest value."""
    names_column = [",".join(sorted(c.job_names)) for c in commits]
    commit_width = max(len(c.commit_id) for c in commits)
    change_width = max(len(c.change_id) for c in commits)
    names_width = max(len(names) for names in names_column)
    age_width = max(len(c.relative_age) for c in commits)
    for c, names in zip(commits, names_column, strict=True):
        typer.echo(
            f"{c.commit_id:<{commit_width}}  {c.change_id:<{change_width}}  "
            f"{names:<{names_width}}  {c.relative_age:<{age_width}}  {c.subject}"
        )


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

    _print_commit_table(shown)


def main() -> None:
    app()
