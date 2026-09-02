"""Per-repository run lock.

A `repoactive run` mutates repository-global state: it forgets and recreates
temporary workspaces, tracks bookmarks, pushes, and creates MRs. Two overlapping
runs against the same repository race on that state — in particular
JJ.forget_stale_workspaces would forget a concurrent run's live
repoactive-tmp-* workspaces. run_lock serialises runs so only one holds
the repository at a time.

The lock is an advisory fcntl.flock on <repo>/.jj/repoactive.lock. flock
releases automatically when the file descriptor is closed *and* when the holding
process dies, so a killed run never leaves a stale lock behind — no PID-liveness
probing is needed. The lock file lives in .jj (which jj keeps git-ignored),
so it is never tracked or pushed.

Unix only: fcntl is not available on Windows, which repoactive does not
target.
"""

import contextlib
import fcntl
import os
import socket
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

LOCK_FILENAME = "repoactive.lock"


class RunLockHeldError(Exception):
    """Raised when another repoactive run already holds the repository lock.

    Carries the holder diagnostics recorded in the lock file (pid/host/start
    time) so the caller can tell the user who holds it; the message falls back to
    the path when the file is empty or unreadable.
    """

    def __init__(self, lock_path: Path, holder: str) -> None:
        self.lock_path = lock_path
        self.holder = holder
        detail = f" ({holder})" if holder else ""
        super().__init__(
            f"another repoactive run is in progress on this repository{detail}; "
            f"lock held at {lock_path}"
        )


def _lock_path(repo_path: Path) -> Path:
    # resolve() so different spellings of the same repo (symlink, relative path)
    # map to the same lock file.
    return repo_path.resolve() / ".jj" / LOCK_FILENAME


def _holder_description(fd: int) -> str:
    """Best-effort one-line description of the current lock holder for messages."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 4096).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _write_holder(fd: int) -> None:
    """Record who holds the lock, for diagnostics only (never read for correctness)."""
    info = (
        f"pid={os.getpid()} host={socket.gethostname()} "
        f"started={datetime.now(UTC).replace(microsecond=0).isoformat()}\n"
    )
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, info.encode("utf-8"))


def _config_workspace_lock_path(repo_path: Path, name: str) -> Path:
    # Keyed by workspace name, not by the workspace directory: a reclaiming
    # command knows the name from jj workspace list, and the directory of an
    # abandoned workspace may be gone.
    return repo_path.resolve() / ".jj" / f"{name}.lock"


@contextlib.contextmanager
def config_workspace_lock(repo_path: Path, name: str) -> Generator[None]:
    """Hold the advisory lock for a configuration workspace.

    The kernel releases the lock when the process ends. This behavior identifies
    abandoned workspaces without the use of process identifiers. See ADR 0021.
    """
    path = _config_workspace_lock_path(repo_path, name)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_holder(fd)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def config_workspace_is_free(repo_path: Path, name: str) -> bool:
    """Report whether no live command holds the config workspace name.

    A missing lock file counts as free: the command that made the workspace is
    gone and removed its lock, or never had one. Callers use this only to
    decide that a registered workspace can be forgotten, so the worst a wrong
    'free' can do is forget a workspace nobody is using.
    """
    path = _config_workspace_lock_path(repo_path, name)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


@contextlib.contextmanager
def run_lock(repo_path: Path) -> Generator[None]:
    """Hold an exclusive per-repository run lock for the duration of the block.

    Fail-fast: if another run holds the lock this raises RunLockHeldError
    immediately rather than waiting. The lock is released (and the descriptor
    closed) on exit, including on exceptions; the lock file itself is left in
    place so a waiter that has already opened it does not race a deletion.
    """
    path = _lock_path(repo_path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RunLockHeldError(path, _holder_description(fd)) from e
        _write_holder(fd)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
