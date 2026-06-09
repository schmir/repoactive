from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, call, patch

import pytest

from repoactive.jj import (
    JJError,
    abandon,
    bookmark_exists,
    bookmark_set,
    describe,
    get_remote_url,
    git_push,
    is_empty,
    new,
)

REPO = Path("/repo")
_BASE = ["jj", "--no-pager", "--color=never"]
_KWARGS = {"cwd": REPO, "capture_output": True, "text": True, "check": True}


def _call(*args: str) -> object:
    return call([*_BASE, *args], **_KWARGS)


class TestJJError:
    def test_subprocess_failure_raises_jj_error(self) -> None:
        err = CalledProcessError(1, "jj", stderr="bad state")
        with (
            patch("repoactive.jj.subprocess.run", side_effect=err),
            pytest.raises(JJError, match="bad state"),
        ):
            new("trunk()", cwd=REPO)

    def test_empty_stderr_in_error(self) -> None:
        err = CalledProcessError(1, "jj", stderr="")
        with (
            patch("repoactive.jj.subprocess.run", side_effect=err),
            pytest.raises(JJError),
        ):
            new("trunk()", cwd=REPO)


class TestNew:
    @patch("repoactive.jj.subprocess.run")
    def test_single_parent(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        new("trunk()", cwd=REPO)
        assert mock_run.call_args == _call("new", "trunk()")

    @patch("repoactive.jj.subprocess.run")
    def test_multiple_parents(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        new("repoactive/a", "repoactive/b", cwd=REPO)
        assert mock_run.call_args == _call("new", "repoactive/a", "repoactive/b")


class TestBookmarkSet:
    @patch("repoactive.jj.subprocess.run")
    def test_sets_bookmark_at_working_copy(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        bookmark_set("repoactive/foo", cwd=REPO)
        assert mock_run.call_args == _call(
            "bookmark", "set", "repoactive/foo", "--revision", "@", "--allow-backwards"
        )


class TestBookmarkExists:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_true_when_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "repoactive/foo"
        assert bookmark_exists("repoactive/foo", cwd=REPO) is True

    def test_returns_false_when_not_found(self) -> None:
        err = CalledProcessError(1, "jj", stderr="not found")
        with patch("repoactive.jj.subprocess.run", side_effect=err):
            assert bookmark_exists("repoactive/foo", cwd=REPO) is False


class TestIsEmpty:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_false_when_not_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "false"
        assert is_empty(cwd=REPO) is False
        assert mock_run.call_args == _call(
            "log", "-r", "@", "--no-graph", "--template", "json(self.empty())"
        )

    @patch("repoactive.jj.subprocess.run")
    def test_returns_true_when_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "true"
        assert is_empty(cwd=REPO) is True


class TestAbandon:
    @patch("repoactive.jj.subprocess.run")
    def test_abandons_working_copy(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        abandon(cwd=REPO)
        assert mock_run.call_args == _call("abandon", "@")


class TestDescribe:
    @patch("repoactive.jj.subprocess.run")
    def test_sets_message(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        describe("chore: update deps", cwd=REPO)
        assert mock_run.call_args == _call("describe", "--message", "chore: update deps")

    @patch("repoactive.jj.subprocess.run")
    def test_multiline_message(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        describe("title\n\nbody", cwd=REPO)
        assert mock_run.call_args == _call("describe", "--message", "title\n\nbody")


class TestGitPush:
    @patch("repoactive.jj.subprocess.run")
    def test_pushes_bookmark(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        git_push("repoactive/foo", cwd=REPO)
        assert mock_run.call_args == _call("git", "push", "--bookmark", "repoactive/foo")


class TestGetRemoteUrl:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_origin_url(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "origin  https://gitlab.com/org/repo.git\n"
        assert get_remote_url(cwd=REPO) == "https://gitlab.com/org/repo.git"

    @patch("repoactive.jj.subprocess.run")
    def test_returns_named_remote(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = (
            "origin  https://gitlab.com/org/repo.git\n"
            "upstream  https://gitlab.com/upstream/repo.git\n"
        )
        assert get_remote_url("upstream", cwd=REPO) == "https://gitlab.com/upstream/repo.git"

    @patch("repoactive.jj.subprocess.run")
    def test_raises_when_remote_not_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "origin  https://gitlab.com/org/repo.git\n"
        with pytest.raises(JJError, match="'upstream' not found"):
            get_remote_url("upstream", cwd=REPO)

    @patch("repoactive.jj.subprocess.run")
    def test_raises_when_no_remotes(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        with pytest.raises(JJError):
            get_remote_url(cwd=REPO)
