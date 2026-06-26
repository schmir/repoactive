import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repoactive.lock import LOCK_FILENAME, RunLockHeldError, run_lock


def _repo(tmp_path: Path) -> Path:
    """A fake repo root with the .jj directory the lock file lives in."""
    (tmp_path / ".jj").mkdir()
    return tmp_path


def test_acquire_release_reacquire(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with run_lock(repo):
        pass
    # Releasing on exit must let a fresh acquisition succeed.
    with run_lock(repo):
        pass


def test_second_acquisition_fails_fast(tmp_path: Path) -> None:
    # flock treats independent open() descriptions separately, even within one
    # process, so a nested acquisition is denied immediately.
    repo = _repo(tmp_path)
    with run_lock(repo), pytest.raises(RunLockHeldError), run_lock(repo):
        pass


def test_error_reports_holder(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with run_lock(repo), pytest.raises(RunLockHeldError) as excinfo, run_lock(repo):
        pass
    assert f"pid={os.getpid()}" in excinfo.value.holder
    assert str(excinfo.value.lock_path) == str(repo.resolve() / ".jj" / LOCK_FILENAME)


def test_lock_file_created_in_jj(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    lock_file = repo / ".jj" / LOCK_FILENAME
    assert not lock_file.exists()
    with run_lock(repo):
        assert lock_file.exists()
        assert f"pid={os.getpid()}" in lock_file.read_text()


def test_released_on_exception(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="boom"), run_lock(repo):
        raise ValueError("boom")
    # The lock must be released even though the body raised.
    with run_lock(repo):
        pass


def test_resolves_symlinked_repo_to_same_lock(tmp_path: Path) -> None:
    # Two spellings of the same repo must contend for one lock.
    repo = _repo(tmp_path)
    link = tmp_path.parent / (tmp_path.name + "-link")
    link.symlink_to(tmp_path)
    try:
        with run_lock(repo), pytest.raises(RunLockHeldError), run_lock(link):
            pass
    finally:
        link.unlink()


_HOLD_SCRIPT = """
import fcntl, os, sys, time
lock = sys.argv[1]
ready = sys.argv[2]
fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
open(ready, "w").close()
time.sleep(30)
"""


def test_released_when_holder_process_dies(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    lock_file = repo / ".jj" / LOCK_FILENAME
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c", _HOLD_SCRIPT, str(lock_file), str(ready)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child never acquired the lock"

        # While the child holds it, we cannot acquire.
        with pytest.raises(RunLockHeldError), run_lock(repo):
            pass

        # The OS releases the flock when the holder dies, with no cleanup on our part.
        child.kill()
        child.wait()
        with run_lock(repo):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
