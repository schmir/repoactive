import contextlib
import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Generator
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

# The all-zeros commit_id jj reports for the virtual root commit, which has no
# git counterpart.
GIT_ROOT_COMMIT_ID = "0" * 40


def workspace_name(job_name: str) -> str:
    """Workspace name repoactive uses for a job's temporary workspace."""
    return f"{WORKSPACE_PREFIX}{job_name}"


class JJError(Exception):
    pass


class CommandFailedError(JJError):
    """Raised when an invoked ``jj`` or ``git`` command exits non-zero."""

    def __init__(self, program: str, args: tuple[str, ...], stderr: str) -> None:
        super().__init__(f"{program} {' '.join(args)} failed:\n{stderr.strip()}")


class RemoteNotFoundError(JJError):
    """Raised when a named git remote does not exist."""

    def __init__(self, remote: str) -> None:
        super().__init__(f"Remote '{remote}' not found")


class NotAColocatedRepoError(Exception):
    """Raised when --repo does not point at the root of a colocated jj repository."""


class NotColocatedGitRepoError(NotAColocatedRepoError):
    """Raised when --repo is a git repository not colocated with jj (no .jj)."""

    def __init__(self, repo: Path) -> None:
        super().__init__(
            f"{repo} is a git repository but not colocated with jj (no .jj directory). "
            "Run 'jj git init --colocate' to create a colocated repository."
        )


class NotAJJRepoError(NotAColocatedRepoError):
    """Raised when --repo is not a jj repository (no .jj directory)."""

    def __init__(self, repo: Path) -> None:
        super().__init__(
            f"{repo} is not a jj repository: no .jj directory found. "
            "--repo must point at the root of a colocated jj repository."
        )


class MissingGitDirError(NotAColocatedRepoError):
    """Raised when a jj repository has no colocated .git directory."""

    def __init__(self, repo: Path) -> None:
        super().__init__(
            f"{repo} is not a colocated jj repository: no .git directory found next to .jj."
        )


def require_colocated_repo(repo: Path) -> None:
    """Verify ``repo`` is the root of a colocated jj repository.

    A colocated repository has a ``.jj`` directory next to a ``.git`` directory.
    Raises NotAColocatedRepoError otherwise.
    """
    has_jj = (repo / ".jj").is_dir()
    has_git = (repo / ".git").is_dir()
    if not has_jj:
        if has_git:
            raise NotColocatedGitRepoError(repo)
        raise NotAJJRepoError(repo)
    if not has_git:
        raise MissingGitDirError(repo)


@dataclass
class Bookmark:
    change_id: str
    name: str


@dataclass
class JobCommit:
    commit_id: str
    change_id: str
    job_name: str
    subject: str
    relative_age: str


class JJ:
    """Wrapper around the jj CLI, bound to a repository or workspace directory."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def _run(self, *args: str) -> str:
        logger.debug("jj %s (cwd=%s)", " ".join(args), self.cwd)
        start = time.monotonic()
        try:
            result = subprocess.run(
                ["jj", "--no-pager", "--color=never", *args],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.debug(
                "jj %s failed (rc=%s):\n%s", " ".join(args), e.returncode, e.stderr.strip()
            )
            raise CommandFailedError("jj", args, e.stderr) from e
        logger.debug(
            "jj %s -> %d bytes in %.3fs",
            " ".join(args),
            len(result.stdout),
            time.monotonic() - start,
        )
        return result.stdout

    def op_id(self) -> str:
        """The current operation id.

        Captured at the start of a run for the debug log and, on a local run, to
        tell the user the exact ``jj op restore`` command that rolls the
        repository back to this state.
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

    def change_id(self, revision: str = "@") -> str:
        """Return the short change id of ``revision`` (the working copy by default)."""
        return self._run("log", "--no-graph", "-r", revision, "-T", "change_id.short()").strip()

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
        # Field order: commit_id, change_id, job_name, relative_age, subject
        template = f"""
        if(trailers.contains_key("{JOB_TRAILER_KEY}"),
           join("{sep}",
             commit_id.short(),
             change_id.short(),
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
            parts = line.split("\x1f", 4)
            if len(parts) == 5:  # noqa: PLR2004
                result.append(
                    JobCommit(
                        commit_id=parts[0],
                        change_id=parts[1],
                        job_name=parts[2],
                        relative_age=parts[3],
                        subject=parts[4],
                    )
                )
        return result

    def unmerged_job_names(self) -> set[str]:
        """Job names that currently have an unmerged commit (a branch not in trunk).

        Returns the ``Repoactive-Job`` trailer value of every commit not yet
        landed in trunk. The default run refreshes these jobs so a stale branch
        is kept rebased on the latest trunk instead of waiting for the job's next
        scheduled run. Unbounded in time: an unmerged branch may be arbitrarily
        old. (With ``--mode publish`` such a branch has an open MR, but a branch
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
        raise RemoteNotFoundError(remote)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        run_cwd = cwd or self.cwd
        logger.debug("git %s (cwd=%s)", " ".join(args), run_cwd)
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=run_cwd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.debug(
                "git %s failed (rc=%s):\n%s", " ".join(args), e.returncode, e.stderr.strip()
            )
            raise CommandFailedError("git", args, e.stderr) from e
        return result.stdout

    def is_colocated(self) -> bool:
        return (self.cwd / ".git").exists()

    def _target_git_head(self) -> str | None:
        """Commit the colocated git HEAD should point at: the first parent of @.

        Returns None if that parent is the root commit, which has no git
        counterpart.
        """
        output = self._run("log", "-r", "@-", "--no-graph", "-T", 'commit_id ++ "\\n"')
        commit_id = output.splitlines()[0] if output.strip() else ""
        if not commit_id or commit_id == GIT_ROOT_COMMIT_ID:
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
        head = self._target_git_head()
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
        head = JJ(path)._target_git_head()
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

    @contextlib.contextmanager
    def temp_workspace(self, name: str) -> Generator["JJ"]:
        """Create a workspace named ``name`` in a temp directory, cleaning up on exit.

        Adds a jj workspace inside a fresh temp directory and yields a JJ bound
        to it. On exit the workspace is forgotten, the temp directory removed,
        and the now-dead git worktree pruned. Teardown is best-effort: jj errors
        during cleanup are suppressed so they cannot mask the body's outcome.
        """
        tmp_root = Path(tempfile.mkdtemp(prefix="repoactive_"))
        workspace_path = tmp_root / "workspace"
        logger.debug("adding workspace %s at %s", name, workspace_path)
        self.workspace_add(name, workspace_path)
        try:
            yield JJ(workspace_path)
        finally:
            logger.debug("cleaning up workspace %s", name)
            with contextlib.suppress(JJError):
                self.workspace_forget(name)
            shutil.rmtree(tmp_root, ignore_errors=True)
            with contextlib.suppress(JJError):
                self.git_worktree_prune()
