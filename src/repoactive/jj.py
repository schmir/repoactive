"""Wrapper around the jj (Jujutsu) CLI for managing colocated jj+git repositories."""

import contextlib
import enum
import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Collection, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from repoactive.constants import JOB_TRAILER_KEY

logger = logging.getLogger(__name__)


class Colocation(enum.Enum):
    COLOCATED = "colocated"
    PLAIN = "plain"


# Prefix for the temporary workspaces repoactive creates for each job. It is not
# configurable so that stale workspaces (left behind by a killed run) can always
# be recognised and reclaimed by name (see JJ.forget_stale_workspaces).
WORKSPACE_PREFIX = "repoactive-tmp-"

# The all-zeros commit_id jj reports for the virtual root commit, which has no
# git counterpart.
GIT_ROOT_COMMIT_ID = "0" * 40

# Where to point users who don't have jj installed.
JJ_INSTALL_URL = "https://docs.jj-vcs.dev/latest/install-and-setup/#installation-and-setup"


def _jj_timestamp(dt: datetime) -> str:
    """Format ``dt`` for a jj date filter expression.

    jj's date parser rejects fractional seconds, so microseconds are dropped.
    """
    return dt.replace(microsecond=0).isoformat()


def workspace_name(job_name: str) -> str:
    """Workspace name repoactive uses for a job's temporary workspace."""
    return f"{WORKSPACE_PREFIX}{job_name}"


def revset_heads(revs: list[str]) -> str:
    """Build a revset selecting the heads of ``revs``.

    Wraps the (non-empty) union of ``revs`` in jj's ``heads()``, dropping any
    commit that is an ancestor of another in the set. Used to merge prerequisite
    heads with the run's parents: an unmoved trunk collapses to a single parent
    (no needless merge commit), a diverged one survives as a real second parent
    (ADR 0019, "Prerequisites: merged with trunk"). Elements are OR'd in
    verbatim, so each may be any revset expression, not just a bare revision.
    """
    return f"heads({' | '.join(revs)})"


class JJError(Exception):
    pass


class CommandFailedError(JJError):
    """Raised when an invoked ``jj`` or ``git`` command exits non-zero."""

    def __init__(self, program: str, args: tuple[str, ...], stderr: str) -> None:
        super().__init__(f"{program} {' '.join(args)} failed:\n{stderr.strip()}")


class RemoteNotFoundError(JJError):
    """Raised when a named git remote does not exist."""

    def __init__(self, remote: str) -> None:
        super().__init__(f"remote '{remote}' not found")


class JJNotFoundError(Exception):
    """Raised when the ``jj`` executable is not on PATH."""

    def __init__(self) -> None:
        super().__init__(
            f"'jj' was not found on PATH. Install jujutsu to use repoactive: {JJ_INSTALL_URL}"
        )


def require_jj_on_path() -> None:
    """Verify the ``jj`` executable is on PATH, raising JJNotFoundError otherwise."""
    if shutil.which("jj") is None:
        raise JJNotFoundError()


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
    # All Repoactive-Job trailer values on the commit. A job produced by a
    # generator records both its own name and the generator's, so there may be
    # more than one (see docs/adr/0004-job-generators.md).
    job_names: set[str]
    subject: str
    relative_age: str


# \x1f (ASCII Unit Separator) can't appear in commit subjects, job names, or
# timestamps, so it's safe as a field delimiter. jj templates use the escape
# form; Python splits on the actual byte.
_FIELD_SEP = "\x1f"

# Template body shared by the trailer-scanning log queries: the JobCommit fields
# joined by _FIELD_SEP. Interpolate the caller's predicate around it.
_JOB_COMMIT_FIELDS = (
    'join("\\x1f", '
    "commit_id.short(), "
    "change_id.short(), "
    f'trailers.filter(|t| t.key() == "{JOB_TRAILER_KEY}").map(|t| t.value()).join(","), '
    "committer.timestamp().local().ago(), "
    "description.first_line()"
    ') ++ "\\n"'
)


def _parse_job_commits(output: str) -> list[JobCommit]:
    """Parse _FIELD_SEP-delimited JobCommit lines produced by _JOB_COMMIT_FIELDS."""
    result = []
    for line in output.splitlines():
        parts = line.split(_FIELD_SEP, 4)
        if len(parts) == 5:  # noqa: PLR2004
            result.append(
                JobCommit(
                    commit_id=parts[0],
                    change_id=parts[1],
                    # Undo the template's comma-join; job names never contain a comma.
                    job_names=set(parts[2].split(",")),
                    relative_age=parts[3],
                    subject=parts[4],
                )
            )
    return result


class JJ:
    """Wrapper around the jj CLI, bound to a repository or workspace directory."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def _exec(
        self,
        program: str,
        args: tuple[str, ...],
        *,
        global_args: tuple[str, ...] = (),
        cwd: Path | None = None,
    ) -> str:
        """Run ``program`` with ``args``, raising CommandFailedError on a non-zero exit.

        ``global_args`` are inserted between the program and ``args`` but kept out
        of logs and error messages, which show only the caller's ``args``.
        """
        run_cwd = cwd or self.cwd
        logger.debug("%s %s (cwd=%s)", program, " ".join(args), run_cwd)
        start = time.monotonic()
        try:
            result = subprocess.run(
                [program, *global_args, *args],
                cwd=run_cwd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.debug(
                "%s %s failed (rc=%s):\n%s",
                program,
                " ".join(args),
                e.returncode,
                e.stderr.strip(),
            )
            raise CommandFailedError(program, args, e.stderr) from e
        logger.debug(
            "%s %s ... -> %d bytes in %.3fs",
            program,
            " ".join(args[:1]),
            len(result.stdout),
            time.monotonic() - start,
        )
        return result.stdout

    def _run(self, *args: str) -> str:
        return self._exec("jj", args, global_args=("--no-pager", "--color=never"))

    def op_id(self) -> str:
        """Return the current operation id.

        Captured at the start of a run for the debug log and, on a local run, to
        tell the user the exact ``jj op restore`` command that rolls the
        repository back to this state.
        """
        return self._run("op", "log", "--no-graph", "--limit", "1", "-T", "id.short()").strip()

    def op_restore(self, op_id: str) -> None:
        """Roll the whole repository back to operation ``op_id``.

        Used to undo a job's in-place rewrite when its command fails (ADR 0020):
        the rebase/edit/restore that regenerates the command commit are all
        recorded as operations after ``op_id``, so restoring to it leaves the
        branch byte-for-byte as it was. Safe because runs are serialised by the
        run lock and jobs execute sequentially, so no concurrent operation races
        this restore.
        """
        self._run("op", "restore", op_id)

    @contextlib.contextmanager
    def op_checkpoint(self) -> Generator[Callable[[], None]]:
        """Capture the current operation and restore it if the block raises.

        On entry the current ``op_id`` is recorded. If the ``with`` block raises,
        the whole repository is rolled back to that operation via
        :meth:`op_restore` before the exception propagates, undoing any in-place
        rewrites the block performed. The yielded callable triggers the same
        restore explicitly, for cases that need to roll back without raising::

            with repo.op_checkpoint() as restore:
                ...  # rewrites; any exception here rolls back automatically
                if no_changes:
                    restore()  # roll back on a non-error path
        """
        op_id = self.op_id()

        def restore() -> None:
            self.op_restore(op_id)

        try:
            yield restore
        except BaseException:
            restore()
            raise

    def git_init_colocate(self) -> None:
        """Initialise a jj repository colocated with the git repository at ``cwd``.

        Runs ``jj git init --colocate``, which creates a ``.jj`` directory next
        to the existing ``.git`` without touching git history.
        """
        self._run("git", "init", "--colocate")

    def new(self, *parents: str) -> None:
        self._run("new", *parents)

    def edit(self, revision: str) -> None:
        """Make ``revision`` the working-copy commit (``@``) of this workspace.

        Used to rewrite a commit in place: point ``@`` at the command commit so a
        subsequent restore + command run regenerate its content directly, keeping
        its change-id (ADR 0020). Only this workspace's ``@`` moves.
        """
        self._run("edit", revision)

    def restore(self, *, source_rev: str, destination_rev: str) -> None:
        self._run("restore", "--from", source_rev, "--into", destination_rev)

    def restore_working_copy(self) -> None:
        """Reset ``@`` to its parent's contents, discarding its own diff.

        ``jj restore`` with no paths restores every path from the parent, leaving
        ``@`` empty. Used after ``edit`` to give a command a clean tree to
        regenerate its output into, in place of the old command commit's diff
        (ADR 0020).
        """
        self._run("restore")

    def rebase(self, *onto: str) -> None:
        onto_args = [arg for parent in onto for arg in ("--onto", parent)]
        self._run("rebase", "-r", "@", *onto_args)

    def bookmark_set(self, name: str, revision: str = "@") -> None:
        self._run("bookmark", "set", name, "--revision", revision, "--allow-backwards")

    def bookmark_delete(self, name: str) -> None:
        self._run("bookmark", "delete", name)

    def bookmark_exists(self, name: str) -> bool:
        return self.bookmark_change_id(name) is not None

    def remote_bookmark_exists(self, name: str) -> bool:
        """Return True if a remote-tracking bookmark for ``name`` exists."""
        return self.remote_bookmark_commit_id(name) is not None

    def remote_bookmark_commit_id(self, name: str) -> str | None:
        """Return the git commit id the remote-tracking bookmark ``name`` points at, or None."""
        revset = f'remote_bookmarks(exact:"{name}")'
        output = self._run("log", "--no-graph", "-r", revset, "-T", 'commit_id ++ "\\n"')
        lines = [line for line in output.splitlines() if line]
        return lines[0] if lines else None

    def bookmark_track(self, *bookmarks: str) -> None:
        """Track the given remote bookmarks so local bookmarks follow them."""
        if not bookmarks:
            return
        self._run("bookmark", "track", *bookmarks)

    def bookmark_list(self) -> list[Bookmark]:
        template = """
        if(self.remote(), "",
           if(self.normal_target(),
              self.normal_target().change_id() ++ " " ++ self.name() ++ "\\n",
              ""
           )
        )
        """
        output = self._run("bookmark", "list", "-T", template)
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

    def abandon_revision(self, revision: str) -> None:
        """Abandon a specific revision (not the working copy)."""
        self._run("abandon", revision)

    def same_content(self, rev1: str, rev2: str) -> bool:
        """Return True if ``rev1`` and ``rev2`` have identical tree contents."""
        return not self._run("diff", "--git", "--from", rev1, "--to", rev2).strip()

    def bookmark_change_id(self, name: str) -> str | None:
        """Return the change-id of the local bookmark ``name``, or None if absent."""
        revset = f'bookmarks(exact:"{name}")'
        output = self._run("log", "--no-graph", "-r", revset, "-T", 'change_id ++ "\\n"')
        lines = [line for line in output.splitlines() if line]
        return lines[0] if lines else None

    def rebase_revision(self, revision: str, *onto: str) -> None:
        """Rebase a specific revision onto ``onto`` without touching ``@``.

        Uses ``-r``: only ``revision`` moves. Any existing descendant of
        ``revision`` is left behind, refilled onto ``revision``'s old
        parent(s) instead of following it to ``onto`` (jj's own "-r" gap-fill
        behaviour) — use ``rebase_source`` when descendants must follow.
        """
        onto_args = [arg for parent in onto for arg in ("--onto", parent)]
        self._run("rebase", "-r", revision, *onto_args)

    def rebase_source(self, revision: str, *onto: str) -> None:
        """Rebase ``revision`` and all its descendants onto ``onto`` without touching ``@``."""
        onto_args = [arg for parent in onto for arg in ("--onto", parent)]
        self._run("rebase", "-s", revision, *onto_args)

    def describe_revision(self, revision: str, message: str) -> None:
        """Set the commit message of a specific revision without touching ``@``."""
        self._run("describe", "-r", revision, "--message", message)

    def get_description(self, revision: str) -> str:
        """Return the commit message of a specific revision."""
        return self._run("log", "--no-graph", "-r", revision, "-T", "description")

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
        revset = f'{revset} & committer_date(after:"{_jj_timestamp(since)}")'
        template = f"""
        if(trailers.contains_key("{JOB_TRAILER_KEY}"),
           {_JOB_COMMIT_FIELDS},
           ""
        )
        """
        output = self._run("log", "--no-graph", "-r", revset, "-T", template)
        return _parse_job_commits(output)

    def job_commits_in_revset(self, revset: str, job_name: str) -> list[JobCommit]:
        """Return commits in ``revset`` carrying a ``Repoactive-Job`` trailer for ``job_name``.

        Ordered newest-first (jj's default log order). Branch-layer detection
        (ADR 0019) uses this to locate this job's command commit within a
        branch slice ``P..R`` - exactly one match on a normal branch, zero when
        the branch is all human commits, more than one only on a corrupted
        branch.
        """
        # Job names are regex-restricted (config._JOB_NAME_RE), so interpolating
        # into the template is safe.
        template = f"""
        if(trailers.any(|t| t.key() == "{JOB_TRAILER_KEY}" && t.value() == "{job_name}"),
           {_JOB_COMMIT_FIELDS},
           ""
        )
        """
        output = self._run("log", "--no-graph", "-r", revset, "-T", template)
        return _parse_job_commits(output)

    def revset_is_empty(self, revset: str) -> bool:
        """Return True if the revset contains no commits."""
        output = self._run("log", "--no-graph", "-r", revset, "-T", '"x\\n"')
        return not output.strip()

    def heads(self, revset: str) -> list[str]:
        """Return the change-id heads of ``revset``, or [] if empty.

        Used to find the prerequisite tip ``heads((P..C) & ~C)``. A linear
        prerequisite chain has one head; [] means there are no prerequisites.
        """
        output = self._run(
            "log", "--no-graph", "-r", f"heads({revset})", "-T", 'change_id.short() ++ "\\n"'
        )
        return [line for line in output.splitlines() if line]

    def roots(self, revset: str) -> list[str]:
        """Return the change-id roots of ``revset``, or [] if empty.

        Used to find the bottom fixups ``roots(C..R)`` - the commits directly
        above the command commit - so the fixup chain can be reapplied on the
        regenerated output. A linear fixup chain has one root; [] means there
        are no fixups.
        """
        output = self._run(
            "log", "--no-graph", "-r", f"roots({revset})", "-T", 'change_id.short() ++ "\\n"'
        )
        return [line for line in output.splitlines() if line]

    def commit_ids(self, revset: str) -> list[str]:
        """Return the git commit ids of the commits in ``revset``, or [] if empty."""
        output = self._run("log", "--no-graph", "-r", revset, "-T", 'commit_id ++ "\\n"')
        return [line for line in output.splitlines() if line]

    def has_conflict(self, revset: str) -> bool:
        """Return True if any commit in ``revset`` contains a materialized conflict.

        jj refuses to push a conflicted commit, so ADR 0019 checks this before
        pushing a rebuilt branch and freezes (pushes nothing) when it holds.
        """
        output = self._run("log", "--no-graph", "-r", f"({revset}) & conflicts()", "-T", '"x\\n"')
        return bool(output.strip())

    def job_names_in_revset(self, revset: str) -> set[str]:
        """Job names that appear in revset."""
        template = f"""
        if(trailers.contains_key("{JOB_TRAILER_KEY}"),
           trailers.filter(|t| t.key() == "{JOB_TRAILER_KEY}").map(|t| t.value()).join(",")
             ++ "\\n",
           ""
        )
        """
        output = self._run("log", "--no-graph", "-r", revset, "-T", template)
        return {
            name.strip()
            for line in output.splitlines()
            for name in line.split(",")
            if name.strip()
        }

    def last_job_commit_date(
        self, *, job_names: Collection[str], base: str, since: datetime
    ) -> datetime | None:
        """Return the committer date of the most recent job commit on ``base``, or ``None``.

        Matches commits that have a ``Repoactive-Job`` trailer whose value is any
        of ``job_names`` and a committer date at or after ``since``. Used to
        throttle jobs: a recent landing on the base branch means the job is still
        on cooldown. Passing more than one name lets a job be throttled by a
        superseding job's landing too (``cooldown_on``, ADR 0015).

        The trailer is matched via jj's trailer parsing, which only considers the
        final paragraph of the description, so a stray matching line in the body
        is correctly ignored.

        Returns the newest matching committer timestamp, or ``None`` if no match.
        """
        # Job names are regex-restricted (config._JOB_NAME_RE), so interpolating
        # them into the template is as safe as the single-name case.
        name_match = " || ".join(f't.value() == "{name}"' for name in sorted(job_names))
        revset = f'::{base} & committer_date(after:"{_jj_timestamp(since)}")'
        template = f"""
        if (trailers.any(|t| t.key() == "{JOB_TRAILER_KEY}" && ({name_match})),
             committer.timestamp().utc().format("%Y-%m-%dT%H:%M:%S") ++ "\\n",
             ""
        )
        """
        output = self._run("log", "--no-graph", "-r", revset, "-T", template)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return None
        # ISO-8601 strings are lexicographically ordered, so max() gives the newest.
        return datetime.fromisoformat(max(lines)).replace(tzinfo=UTC)

    def git_push_bookmarks(self, *bookmarks: str) -> None:
        """Push bookmarks to the remote.

        Pushing a locally-deleted bookmark propagates the deletion; a no-op if
        the bookmark was never pushed.
        """
        if not bookmarks:
            return
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
        return self._exec("git", args, cwd=cwd)

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
        via _workspace_add() need this after the working copy moves (new, edit,
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

    def update_stale_working_copy(self) -> None:
        """Reconcile this workspace's working copy if another workspace's rewrite staled it.

        Rewriting a commit that another workspace has checked out (e.g. the
        default workspace, when a human's ``@`` sits on the repoactive branch a
        job is rewriting in place; ADR 0020) marks that workspace stale, and jj
        then refuses further working-copy commands there, including
        ``workspace add`` for the next job. ``update-stale`` fast-forwards the
        working copy to the rewritten commit. A no-op when it is not stale.
        """
        self._run("workspace", "update-stale")

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

    def _workspace_add(
        self, name: str, path: Path, colocation: Colocation = Colocation.COLOCATED
    ) -> None:
        self._run("workspace", "add", "--name", name, str(path))
        if colocation is Colocation.COLOCATED and self.is_colocated():
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
        with tempfile.TemporaryDirectory(prefix="repoactive-worktree-", dir=path.parent) as d:
            tmp = Path(d)
            self._git("worktree", "add", "--no-checkout", "--detach", str(tmp / name), head)
            (tmp / name / ".git").rename(path / ".git")
        self._git("worktree", "repair", str(path))
        # jj writes this for colocated repos, but not for workspaces.
        (path / ".jj" / ".gitignore").write_text("/*\n")
        # --no-checkout left the index empty; jj already wrote the files.
        self._git("reset", "--quiet", head, cwd=path)

    def workspace_forget(self, name: str) -> None:
        self._run("workspace", "forget", name)

    @contextlib.contextmanager
    def temp_workspace(
        self, name: str, colocation: Colocation = Colocation.COLOCATED
    ) -> Generator["JJ"]:
        """Create a workspace named ``name`` in a temp directory, cleaning up on exit.

        Adds a jj workspace inside a fresh temp directory and yields a JJ bound
        to it. On exit the workspace is forgotten, the temp directory removed,
        and the now-dead git worktree pruned. Teardown is best-effort: jj errors
        during cleanup are suppressed so they cannot mask the body's outcome.
        """
        tmp_root = Path(tempfile.mkdtemp(prefix="repoactive-workspace-"))
        workspace_path = tmp_root / "workspace"
        logger.debug("adding workspace %s at %s", name, workspace_path)
        # A previous job's in-place rewrite (ADR 0020) may have left this (the
        # default) workspace stale; jj refuses `workspace add` while it is, so
        # reconcile it first. No-op when nothing staled it.
        self.update_stale_working_copy()
        self._workspace_add(name, workspace_path, colocation)
        try:
            yield JJ(workspace_path)
        finally:
            logger.debug("cleaning up workspace %s", name)
            with contextlib.suppress(JJError):
                self.workspace_forget(name)
            shutil.rmtree(tmp_root, ignore_errors=True)
            if colocation is Colocation.COLOCATED:
                with contextlib.suppress(JJError):
                    self.git_worktree_prune()
