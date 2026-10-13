"""Fast tests for pure JJ helpers and repository validation.

Behavior that invokes jj is covered against a real repository in
test_jj_integration.py.
"""

import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from repoactive.jj import (
    CONFIG_WORKSPACE_PREFIX,
    MIN_JJ_VERSION,
    WORKSPACE_PREFIX,
    CommandFailedError,
    JJNotFoundError,
    JJVersionError,
    MissingGitDirError,
    NotAJJRepoError,
    NotColocatedGitRepoError,
    _jj_timestamp,
    config_workspace_name,
    parse_jj_version,
    require_colocated_repo,
    require_jj_on_path,
    require_min_jj_version,
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

    def test_config_workspace_name_is_prefixed_and_fresh(self) -> None:
        name = config_workspace_name()
        assert name.startswith(CONFIG_WORKSPACE_PREFIX)
        # Concurrent commands must use different names.
        assert name != config_workspace_name()

    def test_config_workspace_name_is_outside_the_job_prefix(self) -> None:
        # A config workspace is created without the run lock, so a run's stale
        # sweep must not be able to match it (ADR 0021).
        assert not config_workspace_name().startswith(WORKSPACE_PREFIX)
        assert not CONFIG_WORKSPACE_PREFIX.startswith(WORKSPACE_PREFIX)


class TestRequireJJOnPath:
    def test_accepts_when_on_path(self) -> None:
        require_jj_on_path()

    def test_rejects_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "")

        with pytest.raises(JJNotFoundError, match=r"docs\.jj-vcs\.dev"):
            require_jj_on_path()


def _fake_jj_version(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 0) -> None:
    """Make subprocess.run report the given output for jj --version."""

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="boom")

    monkeypatch.setattr("repoactive.jj.subprocess.run", fake_run)


class TestParseJJVersion:
    def test_parses_plain_version(self) -> None:
        assert parse_jj_version("jj 0.46.0\n") == (0, 46, 0)

    def test_parses_version_with_build_suffix(self) -> None:
        assert parse_jj_version("jj 0.47.1-6a3f1c2d") == (0, 47, 1)

    def test_rejects_garbage(self) -> None:
        with pytest.raises(JJVersionError, match="could not parse"):
            parse_jj_version("not a version")


class TestRequireMinJJVersion:
    def test_accepts_minimum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_jj_version(monkeypatch, "jj 0.46.0\n")
        require_min_jj_version()

    def test_accepts_newer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_jj_version(monkeypatch, "jj 1.0.0\n")
        require_min_jj_version()

    def test_rejects_older(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_jj_version(monkeypatch, "jj 0.45.9\n")
        with pytest.raises(JJVersionError, match=r"0\.46\.0 or newer, but found 0\.45\.9"):
            require_min_jj_version()

    def test_rejects_failed_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_jj_version(monkeypatch, "", returncode=1)
        with pytest.raises(JJVersionError, match="failed"):
            require_min_jj_version()

    def test_minimum_is_0_46_0(self) -> None:
        assert MIN_JJ_VERSION == (0, 46, 0)


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
