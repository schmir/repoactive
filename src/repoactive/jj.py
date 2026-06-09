import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class JJError(Exception):
    pass


def _run(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["jj", "--no-pager", "--color=never", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise JJError(f"jj {' '.join(args)} failed:\n{e.stderr.strip()}") from e


def new(*parents: str, cwd: Path | None = None) -> None:
    _run("new", *parents, cwd=cwd)


def bookmark_set(name: str, revision: str = "@", cwd: Path | None = None) -> None:
    _run("bookmark", "set", name, "--revision", revision, "--allow-backwards", cwd=cwd)


def bookmark_exists(name: str, cwd: Path | None = None) -> bool:
    try:
        _run("bookmark", "list", name, cwd=cwd)
        return True
    except JJError:
        return False


def is_empty(cwd: Path | None = None) -> bool:
    output = _run("log", "-r", "@", "--no-graph", "--template", "json(self.empty())", cwd=cwd)
    result = output.strip() == "true"
    logger.debug("is_empty: jj output=%r result=%r", output.strip(), result)
    return result


def abandon(cwd: Path | None = None) -> None:
    _run("abandon", "@", cwd=cwd)


def describe(message: str, cwd: Path | None = None) -> None:
    _run("describe", "--message", message, cwd=cwd)


def git_push(bookmark: str, cwd: Path | None = None) -> None:
    _run("git", "push", "--bookmark", bookmark, cwd=cwd)


def get_remote_url(remote: str = "origin", cwd: Path | None = None) -> str:
    output = _run("git", "remote", "list", cwd=cwd)
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] == remote:
            return parts[1]
    raise JJError(f"Remote '{remote}' not found")
