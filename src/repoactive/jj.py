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


class JJ:
    """Wrapper around the jj CLI, bound to a repository or workspace directory."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["jj", "--no-pager", "--color=never", *args],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise JJError(f"jj {' '.join(args)} failed:\n{e.stderr.strip()}") from e

    def new(self, *parents: str) -> None:
        self._run("new", *parents)

    def edit(self, revision: str) -> None:
        self._run("edit", revision)

    def restore(self, source: str) -> None:
        self._run("restore", "--changes-in", source)

    def rebase(self, *onto: str) -> None:
        onto_args = [arg for parent in onto for arg in ("--onto", parent)]
        self._run("rebase", "-r", "@", *onto_args)

    def bookmark_set(self, name: str, revision: str = "@") -> None:
        self._run("bookmark", "set", name, "--revision", revision, "--allow-backwards")

    def bookmark_delete(self, name: str) -> None:
        self._run("bookmark", "delete", name)

    def bookmark_exists(self, name: str) -> bool:
        return any(b.name == name for b in self.bookmark_list())

    def bookmark_list(self) -> list[Bookmark]:
        output = self._run(
            "bookmark",
            "list",
            "-T",
            'if(self.remote(), "", if(self.normal_target(), self.normal_target().change_id() ++ " " ++ self.name() ++ "\\n", ""))',
        )
        result = []
        for line in output.splitlines():
            if line:
                change_id, name = line.split(" ", 1)
                result.append(Bookmark(change_id=change_id, name=name))
        return result

    def is_empty(self) -> bool:
        output = self._run("log", "-r", "@", "--no-graph", "--template", "json(self.empty())")
        result = output.strip() == "true"
        logger.debug("is_empty: jj output=%r result=%r", output.strip(), result)
        return result

    def abandon(self) -> None:
        self._run("abandon", "@")

    def diff_stat(self) -> str:
        return self._run("diff", "--stat").strip()

    def describe(self, message: str) -> None:
        self._run("describe", "--message", message)

    def git_push_bookmarks(self, *bookmarks: str) -> None:
        """Push bookmarks to the remote.

        Pushing a locally-deleted bookmark propagates the deletion; a no-op if
        the bookmark was never pushed.
        """
        bookmark_args = [arg for bookmark in bookmarks for arg in ("--bookmark", bookmark)]
        self._run("git", "push", *bookmark_args)

    def get_remote_url(self, remote: str = "origin") -> str:
        output = self._run("git", "remote", "list")
        for line in output.splitlines():
            parts = line.split()
            if parts and parts[0] == remote:
                return parts[1]
        raise JJError(f"Remote '{remote}' not found")

    def workspace_add(self, name: str, path: Path) -> None:
        self._run("workspace", "add", "--name", name, str(path))

    def workspace_forget(self, name: str) -> None:
        self._run("workspace", "forget", name)
