import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Trailer key recorded on every repoactive commit so later runs can tell which
# job produced a commit (see JJ.has_recent_job_commit and runner._publish_job).
JOB_TRAILER_KEY = "Repoactive-Job"


class JJError(Exception):
    pass


@dataclass
class Bookmark:
    change_id: str
    name: str


@dataclass
class JobCommit:
    commit_id: str
    job_name: str
    subject: str
    ago: str


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

    def recent_job_commits(self, since: datetime, revset: str = "all()") -> list[JobCommit]:
        """Return commits matching ``revset`` within ``since`` that carry a repoactive job trailer.

        Results are ordered newest-first (jj's default log order).
        Pass ``revset="::trunk()"`` for merged commits only,
        ``revset="~(::trunk())"`` for unmerged only.
        """
        since_iso = since.replace(microsecond=0).isoformat()
        revset = f'{revset} & committer_date(after:"{since_iso}")'
        # \x1f (ASCII Unit Separator) can't appear in commit subjects, job names, or timestamps,
        # so it's safe as a field delimiter. jj templates use the escape form; Python splits on the
        # actual byte.
        sep = "\\x1f"
        # Field order: commit_id, job_name, ago, subject
        template = f"""
        if(trailers.contains_key("{JOB_TRAILER_KEY}"),
           join("{sep}",
             commit_id.short(),
             trailers.filter(|t| t.key() == "{JOB_TRAILER_KEY}").map(|t| t.value()).join(","),
             committer.timestamp().local().ago(),
             description.first_line()
           ) ++ "\\n",
           ""
        )
        """
        output = self._run("log", "--no-graph", "-r", revset, "-T", template)
        result = []
        for line in output.splitlines():
            if not line:
                continue
            parts = line.split("\x1f", 3)
            if len(parts) == 4:  # noqa: PLR2004
                result.append(
                    JobCommit(
                        commit_id=parts[0],
                        job_name=parts[1],
                        ago=parts[2],
                        subject=parts[3],
                    )
                )
        return result

    def has_recent_job_commit(self, job_name: str, base: str, since: datetime) -> bool:
        """Whether ``base`` already carries a recent commit from the given job.

        Matches commits that have a ``Repoactive-Job: <job_name>`` trailer and a
        committer date at or after ``since``. Used to throttle jobs: a recent
        landing on the base branch means the job is still on cooldown.

        The trailer is matched via jj's trailer parsing, which only considers the
        final paragraph of the description, so a stray matching line in the body
        is correctly ignored.
        """
        # jj's date parser rejects fractional seconds, so drop microseconds.
        since_iso = since.replace(microsecond=0).isoformat()
        revset = f'{base} & committer_date(after:"{since_iso}")'
        template = f"""
        if (trailers.any(|t| t.key() == "{JOB_TRAILER_KEY}" && t.value() == "{job_name}"),
             "x",
             ""
        )
        """
        output = self._run("log", "--no-graph", "-r", revset, "-T", template)
        return bool(output.strip())

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

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd or self.cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise JJError(f"git {' '.join(args)} failed:\n{e.stderr.strip()}") from e

    def is_colocated(self) -> bool:
        return (self.cwd / ".git").exists()

    def _git_head_commit(self) -> str | None:
        """Commit the colocated git HEAD should point at: the first parent of @.

        Returns None if that parent is the root commit, which has no git
        counterpart.
        """
        output = self._run("log", "-r", "@-", "--no-graph", "-T", 'commit_id ++ "\\n"')
        commit_id = output.splitlines()[0] if output.strip() else ""
        if not commit_id or set(commit_id) == {"0"}:
            return None
        return commit_id

    def git_sync_head(self) -> None:
        """Sync the colocated git checkout (HEAD and index) to the jj working copy.

        jj only exports git HEAD in the default workspace; workspaces colocated
        via workspace_add() need this after the working copy moves (new, edit,
        rebase). No-op if the workspace is not colocated.
        """
        if not self.is_colocated():
            return
        head = self._git_head_commit()
        if head is None:
            return
        # Mixed reset: moves the detached HEAD and index, leaves the
        # jj-managed files alone.
        self._git("reset", "--quiet", head)

    def git_worktree_prune(self) -> None:
        """Drop git worktree registrations of workspaces whose directory is gone."""
        if self.is_colocated():
            self._git("worktree", "prune")

    def workspace_add(self, name: str, path: Path) -> None:
        self._run("workspace", "add", "--name", name, str(path))
        if self.is_colocated():
            self._colocate_workspace(name, path)

    def _colocate_workspace(self, name: str, path: Path) -> None:
        """Register the new workspace as a git worktree of the colocated repo.

        jj's `workspace add` never colocates the new workspace, even when the
        main repository is colocated (https://github.com/jj-vcs/jj/issues/5252),
        so git commands would not work inside it. Both `git worktree add` and
        `jj workspace add` refuse a non-empty existing directory, so the
        worktree is created next to the workspace and its .git file moved into
        place.
        """
        head = JJ(path)._git_head_commit()
        if head is None:
            logger.debug("not colocating workspace %r: parent is the root commit", name)
            return
        tmp = Path(tempfile.mkdtemp(prefix="repoactive-worktree-", dir=path.parent))
        try:
            self._git("worktree", "add", "--no-checkout", "--detach", str(tmp / name), head)
            (tmp / name / ".git").rename(path / ".git")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._git("worktree", "repair", str(path))
        # jj writes this for colocated repos, but not for workspaces.
        (path / ".jj" / ".gitignore").write_text("/*\n")
        # --no-checkout left the index empty; jj already wrote the files.
        self._git("reset", "--quiet", head, cwd=path)

    def workspace_forget(self, name: str) -> None:
        self._run("workspace", "forget", name)
