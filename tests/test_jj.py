"""Fast tests for pure JJ helpers and repository validation.

Behavior that invokes jj is covered against a real repository in
test_jj_integration.py.
"""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from repoactive.jj import (
    WORKSPACE_PREFIX,
    CommandFailedError,
    JJNotFoundError,
    MissingGitDirError,
    NotAJJRepoError,
    NotColocatedGitRepoError,
    _jj_timestamp,
    require_colocated_repo,
    require_jj_on_path,
    workspace_name,
)


class TestCommandFailedError:
    def test_includes_command_and_stderr(self) -> None:
        error = CommandFailedError("jj", ("new", "missing"), "bad state\n")

        assert str(error) == "jj new missing failed:\nbad state"

    def test_accepts_empty_stderr(self) -> None:
        error = CommandFailedError("jj", ("new", "missing"), "")

        assert str(error) == "jj new missing failed:\n"


class TestWorkspaceName:
    def test_prefixes_job_name(self) -> None:
        assert workspace_name("foo") == f"{WORKSPACE_PREFIX}foo"


class TestRequireJJOnPath:
    def test_accepts_when_on_path(self) -> None:
        require_jj_on_path()

    def test_rejects_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "")

        with pytest.raises(JJNotFoundError, match=r"docs\.jj-vcs\.dev"):
            require_jj_on_path()


class TestRequireColocatedRepo:
    def test_accepts_colocated_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".jj").mkdir()
        (tmp_path / ".git").mkdir()

        require_colocated_repo(tmp_path)

    def test_rejects_git_only_with_colocate_hint(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()

        with pytest.raises(NotColocatedGitRepoError, match=r"jj git init --colocate"):
            require_colocated_repo(tmp_path)

    def test_rejects_missing_both(self, tmp_path: Path) -> None:
        with pytest.raises(NotAJJRepoError, match=r"no \.jj directory"):
            require_colocated_repo(tmp_path)

    def test_rejects_missing_git(self, tmp_path: Path) -> None:
        (tmp_path / ".jj").mkdir()

        with pytest.raises(MissingGitDirError, match=r"no \.git directory"):
            require_colocated_repo(tmp_path)

    def test_rejects_jj_that_is_a_file(self, tmp_path: Path) -> None:
        (tmp_path / ".jj").write_text("")
        (tmp_path / ".git").mkdir()

        with pytest.raises(NotColocatedGitRepoError, match=r"no \.jj directory"):
            require_colocated_repo(tmp_path)

    def test_rejects_non_root_directory(self, tmp_path: Path) -> None:
        (tmp_path / ".jj").mkdir()
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        with pytest.raises(NotAJJRepoError):
            require_colocated_repo(subdir)


class TestJjTimestamp:
    def test_strips_microseconds(self) -> None:
        dt = datetime(2024, 3, 15, 10, 30, 45, 123456, tzinfo=UTC)
        assert _jj_timestamp(dt) == "2024-03-15T10:30:45+00:00"

    def test_zero_microseconds_unchanged(self) -> None:
        dt = datetime(2024, 3, 15, 10, 30, 45, 0, tzinfo=UTC)
        assert _jj_timestamp(dt) == "2024-03-15T10:30:45+00:00"

    def test_preserves_timezone(self) -> None:
        tz = timezone(timedelta(hours=2))
        dt = datetime(2024, 6, 1, 12, 0, 0, 999999, tzinfo=tz)
        assert _jj_timestamp(dt) == "2024-06-01T12:00:00+02:00"
