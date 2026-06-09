import logging
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer

from repoactive.config import load_config
from repoactive.platforms import get_platform
from repoactive.runner import run_all

app = typer.Typer(no_args_is_help=True)

_DEFAULT_CONFIG = Path(".repoactive.toml")
_DEFAULT_REPO = Path()


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
def run(
    config: Annotated[
        list[Path] | None,
        typer.Option("--config", "-c", help="Config file; repeat to merge, later files win."),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", "-r", help="Path to the jj repository.")
    ] = _DEFAULT_REPO,
    local: Annotated[
        bool, typer.Option("--local", help="Skip pushing branches and MR creation/update.")
    ] = False,
    debug: Annotated[bool, typer.Option("--debug", "-d", help="Enable debug logging.")] = False,
    jobs: Annotated[
        list[str] | None,
        typer.Argument(help="Jobs to run (default: all); dependencies are auto-included."),
    ] = None,
) -> None:
    """Apply jobs and create or update merge requests."""
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    cfg = load_config(config or [_DEFAULT_CONFIG])
    platform = None if local else get_platform(cfg.platform, repo)
    summary = run_all(
        config=cfg, repo_path=repo, platform=platform, requested_jobs=jobs or None, local=local
    )
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command("validate-config")
def validate_config(
    config: Annotated[
        list[Path] | None,
        typer.Option("--config", "-c", help="Config file; repeat to merge, later files win."),
    ] = None,
) -> None:
    """Validate configuration and exit.

    Prints 'Config OK: N job(s) defined.' on success (exit 0).
    Prints the error to stderr and exits with code 1 on failure.
    """
    try:
        cfg = load_config(config or [_DEFAULT_CONFIG])
    except Exception as e:
        typer.echo(f"Invalid config: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"Config OK: {len(cfg.jobs)} job(s) defined.")


def main() -> None:
    app()
