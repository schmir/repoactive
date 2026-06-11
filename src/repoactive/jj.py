import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class JJError(Exception):
    pass


@dataclass
class Bookmark:
    change_id: str
    name: str


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


def edit(revision: str, cwd: Path | None = None) -> None:
    _run("edit", revision, cwd=cwd)


def restore(source: str, cwd: Path | None = None) -> None:
    _run("restore", "--changes-in", source, cwd=cwd)


def rebase(*onto: str, cwd: Path | None = None) -> None:
    onto_args = [arg for parent in onto for arg in ("--onto", parent)]
    _run("rebase", "-r", "@", *onto_args, cwd=cwd)


def bookmark_set(name: str, revision: str = "@", cwd: Path | None = None) -> None:
    _run("bookmark", "set", name, "--revision", revision, "--allow-backwards", cwd=cwd)


def bookmark_delete(name: str, cwd: Path | None = None) -> None:
    _run("bookmark", "delete", name, cwd=cwd)


def bookmark_exists(name: str, cwd: Path | None = None) -> bool:
    return any(b.name == name for b in bookmark_list(cwd=cwd))


def bookmark_list(cwd: Path | None = None) -> list[Bookmark]:
    output = _run(
        "bookmark",
        "list",
        "-T",
        'self.normal_target().change_id() ++ " " ++ self.name() ++ "\\n"',
        cwd=cwd,
    )
    result = []
    for line in output.splitlines():
        if line:
            change_id, name = line.split(" ", 1)
            result.append(Bookmark(change_id=change_id, name=name))
    return result


def is_empty(cwd: Path | None = None) -> bool:
    output = _run("log", "-r", "@", "--no-graph", "--template", "json(self.empty())", cwd=cwd)
    result = output.strip() == "true"
    logger.debug("is_empty: jj output=%r result=%r", output.strip(), result)
    return result


def abandon(cwd: Path | None = None) -> None:
    _run("abandon", "@", cwd=cwd)


def diff_stat(cwd: Path | None = None) -> str:
    return _run("diff", "--stat", cwd=cwd).strip()


def describe(message: str, cwd: Path | None = None) -> None:
    _run("describe", "--message", message, cwd=cwd)


def git_push(bookmark: str, cwd: Path | None = None) -> None:
    _run("git", "push", "--bookmark", bookmark, cwd=cwd)


def git_push_delete(bookmark: str, cwd: Path | None = None) -> None:
    _run("git", "push", "--deleted", "--bookmark", bookmark, cwd=cwd)


def get_remote_url(remote: str = "origin", cwd: Path | None = None) -> str:
    output = _run("git", "remote", "list", cwd=cwd)
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] == remote:
            return parts[1]
    raise JJError(f"Remote '{remote}' not found")
