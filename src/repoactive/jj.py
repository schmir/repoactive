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

# Prefix for the temporary workspaces repoactive creates for each job. It is not
# configurable so that stale workspaces (left behind by a killed run) can always
# be recognised and reclaimed by name (see JJ.forget_stale_workspaces).
WORKSPACE_PREFIX = "repoactive-tmp-"


def workspace_name(job_name: str) -> str:
    """Workspace name repoactive uses for a job's temporary workspace."""
    return f"{WORKSPACE_PREFIX}{job_name}"


class JJError(Exception):
    pass


class NotAColocatedRepoError(Exception):
    """Raised when --repo does not point at the root of a colocated jj repository."""


def ensure_colocated_repo(repo: Path) -> None:
    """Verify ``repo`` is the root of a colocated jj repository.

    A colocated repository has a ``.jj`` directory next to a ``.git`` directory.
    Raises NotAColocatedRepoError otherwise.
    """
    has_jj = (repo / ".jj").is_dir()
    has_git = (repo / ".git").is_dir()
    if not has_jj:
        if has_git:
            raise NotAColocatedRepoError(
                f"{repo} is a git repository but not colocated with jj (no .jj directory). "
                "Run 'jj git init --colocate' to create a colocated repository."
            )
        raise NotAColocatedRepoError(
            f"{repo} is not a jj repository: no .jj directory found. "
            "--repo must point at the root of a colocated jj repository."
        )
    if not has_git:
        raise NotAColocatedRepoError(
            f"{repo} is not a colocated jj repository: no .git directory found next to .jj."
        )


@dataclass
class Bookmark:
    change_id: str
    name: str


@dataclass
class JobCommit:
    commit_id: str
    job_name: str
    subject: str
    relative_age: str


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

    def op_id(self) -> str:
        """The current operation id.

        Captured at the start of a run so the user can be told the exact
        ``jj op restore`` command that rolls the repository back to this state.
        """
        return self._run("op", "log", "--no-graph", "--limit", "1", "-T", "id.short()").strip()

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
        return self._run("log", "--no-graph", "-r", "@", "-T", "self.diff().stat(50)").strip()

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
        # Field order: commit_id, job_name, relative_age, subject
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
                        relative_age=parts[2],
                        subject=parts[3],
                    )
                )
        return result

    def unmerged_job_names(self) -> set[str]:
        """Job names that currently have an unmerged commit (a branch not in trunk).

        Returns the ``Repoactive-Job`` trailer value of every commit not yet
        landed in trunk. The default run refreshes these jobs so a stale branch
        is kept rebased on the latest trunk instead of waiting for the job's next
        scheduled run. Unbounded in time: an unmerged branch may be arbitrarily
        old. (With ``--create-prs`` such a branch has an open MR, but a branch
        may also exist without one.)
        """
        template = f"""
        if(trailers.contains_key("{JOB_TRAILER_KEY}"),
           trailers.filter(|t| t.key() == "{JOB_TRAILER_KEY}").map(|t| t.value()).join(",")
             ++ "\\n",
           ""
        )
        """
        output = self._run("log", "--no-graph", "-r", "~(::trunk())", "-T", template)
        return {line.strip() for line in output.splitlines() if line.strip()}

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
        revset = f'::{base} & committer_date(after:"{since_iso}")'
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
        bookmark_args = []
        for bookmark in bookmarks:
            bookmark_args += ["--bookmark", bookmark]
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

    def workspace_names(self) -> set[str]:
        """Names of the workspaces jj currently tracks for this repo."""
        return set(self._run("workspace", "list", "-T", 'name ++ "\\n"').splitlines())

    def forget_stale_workspaces(self) -> None:
        """Forget any leftover repoactive workspaces and prune their dead worktrees.

        A run killed before its `finally` could call workspace_forget leaves its
        temporary workspace (named with WORKSPACE_PREFIX) registered in jj. Because
        the prefix is fixed, every such workspace can be recognised and dropped here,
        even for jobs that have since been renamed or removed.
        """
        stale = sorted(n for n in self.workspace_names() if n.startswith(WORKSPACE_PREFIX))
        logger.debug("stale workspaces to forget: %s", stale)
        for name in stale:
            self.workspace_forget(name)
        if stale:
            self.git_worktree_prune()

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
