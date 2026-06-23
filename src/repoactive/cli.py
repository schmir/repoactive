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
    ConfigNotFoundError,
    default_config_paths,
    expand_config_paths,
    load_config,
    parse_duration,
)
from repoactive.jj import (
    JJ,
    JJNotFoundError,
    NotAColocatedRepoError,
    NotColocatedGitRepoError,
    require_colocated_repo,
    require_jj_on_path,
)
from repoactive.platforms import get_platform
from repoactive.runner import RunMode, run_all

app = typer.Typer(no_args_is_help=True)

_DEFAULT_REPO = Path()


class MergeStatus(StrEnum):
    """Filter for ``recent-commits`` by whether a commit has landed in trunk."""

    all = "all"
    merged = "merged"
    unmerged = "unmerged"


def _resolve_config(config: list[Path] | None, repo: Path) -> list[Path]:
    """Use the given config paths, or discover defaults inside ``repo``."""
    return config or default_config_paths(repo)


def _check_jj() -> None:
    """Exit with a clear error unless the jj executable is on PATH."""
    try:
        require_jj_on_path()
    except JJNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e


def _check_repo(repo: Path) -> None:
    """Ensure ``repo`` is a colocated jj repository root, else exit with a clear error.

    A plain git repository (``.git`` but no ``.jj``) is converted in place by
    running ``jj git init --colocate``; other invalid states exit non-zero.
    """
    try:
        require_colocated_repo(repo)
    except NotColocatedGitRepoError:
        JJ(repo).git_init_colocate()
        abs_repo = repo.resolve()
        typer.secho(
            f"\n!! {abs_repo} was a plain git repository; ran 'jj git init --colocate' to make "
            f"it a colocated jj repository.\n"
            f"!! To undo, remove the jj data: rm -rf {abs_repo / '.jj'}\n",
            err=True,
            fg=typer.colors.YELLOW,
            bold=True,
        )
    except NotAColocatedRepoError as e:
        typer.echo(str(e), err=True)
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


@app.command()
def run(  # noqa: PLR0913
    config: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            "-c",
            help="Config file or directory of *.toml files; repeat to merge, later files win.",
        ),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", "-r", help="Path to the jj repository.")
    ] = _DEFAULT_REPO,
    mode: Annotated[
        RunMode,
        typer.Option(
            "--mode",
            "-m",
            help="How far to publish: 'local' (default) applies only to the local repo, "
            "'push' also pushes bookmarks, 'publish' also creates/updates MRs/PRs.",
        ),
    ] = RunMode.local,
    debug: Annotated[bool, typer.Option("--debug", "-d", help="Enable debug logging.")] = False,
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
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    _check_jj()
    _check_repo(repo)
    try:
        cfg = load_config(_resolve_config(config, repo))
    except ConfigNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    platform = get_platform(cfg, repo) if mode is RunMode.publish else None
    summary = run_all(
        config=cfg,
        repo_path=repo,
        platform=platform,
        requested_jobs=jobs or None,
        requested_tags=tags or None,
        mode=mode,
    )
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command("validate-config")
def validate_config(
    config: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            "-c",
            help="Config file or directory of *.toml files; repeat to merge, later files win.",
        ),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", "-r", help="Path to the jj repository.")
    ] = _DEFAULT_REPO,
) -> None:
    """Validate configuration and exit.

    Lists the configuration files used and prints 'Config OK: N job(s)
    defined.' on success (exit 0). Prints the error to stderr and exits with
    code 1 on failure.
    """
    try:
        paths = _resolve_config(config, repo)
        files = expand_config_paths(paths)
        cfg = load_config(paths)
    except Exception as e:
        typer.echo(f"Invalid config: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo("Configuration files:")
    for file in files:
        typer.echo(f"  {file}")
    typer.echo(f"Config OK: {len(cfg.jobs)} job(s) defined.")


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
    repo: Annotated[
        Path, typer.Option("--repo", "-r", help="Path to the jj repository.")
    ] = _DEFAULT_REPO,
    merge_status: Annotated[
        MergeStatus,
        typer.Option("--status", "-s", help="Filter by merge status into trunk."),
    ] = MergeStatus.all,
    jobs: Annotated[
        list[str] | None,
        typer.Argument(help="Job names to filter on (default: all)."),
    ] = None,
) -> None:
    """List commits produced by repoactive within a time window.

    Each commit carries a Repoactive-Job trailer written by repoactive. Pass
    one or more job names to narrow the output; omit them to show all jobs.
    By default shows all commits; pass --status merged or --status unmerged to
    filter by whether the commit has landed in trunk.
    """
    _check_jj()
    _check_repo(repo)
    try:
        delta = parse_duration(within)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    match merge_status:
        case MergeStatus.merged:
            revset = "::trunk()"
        case MergeStatus.unmerged:
            revset = "~(::trunk())"
        case MergeStatus.all:
            revset = "all()"

    cutoff = datetime.now(UTC) - delta
    commits = JJ(repo).recent_job_commits(cutoff, revset=revset)

    filter_names = set(jobs) if jobs else None
    shown = [c for c in commits if filter_names is None or c.job_name in filter_names]

    if not shown:
        typer.echo("No matching commits found.")
        return

    id_width = max(len(c.commit_id) for c in shown)
    change_width = max(len(c.change_id) for c in shown)
    name_width = max(len(c.job_name) for c in shown)
    age_width = max(len(c.relative_age) for c in shown)
    for c in shown:
        typer.echo(
            f"{c.commit_id:<{id_width}}  {c.change_id:<{change_width}}  "
            f"{c.job_name:<{name_width}}  {c.relative_age:<{age_width}}  {c.subject}"
        )


def main() -> None:
    app()
