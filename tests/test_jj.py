from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, call, patch

import pytest

from repoactive.jj import JJ, Bookmark, JJError

REPO = Path("/repo")
_BASE = ["jj", "--no-pager", "--color=never"]
_KWARGS = {"cwd": REPO, "capture_output": True, "text": True, "check": True}


def _jj() -> JJ:
    return JJ(REPO)


def _call(*args: str) -> object:
    return call([*_BASE, *args], **_KWARGS)


class TestJJError:
    def test_subprocess_failure_raises_jj_error(self) -> None:
        err = CalledProcessError(1, "jj", stderr="bad state")
        with (
            patch("repoactive.jj.subprocess.run", side_effect=err),
            pytest.raises(JJError, match="bad state"),
        ):
            _jj().new("trunk()")

    def test_empty_stderr_in_error(self) -> None:
        err = CalledProcessError(1, "jj", stderr="")
        with (
            patch("repoactive.jj.subprocess.run", side_effect=err),
            pytest.raises(JJError),
        ):
            _jj().new("trunk()")


class TestNew:
    @patch("repoactive.jj.subprocess.run")
    def test_single_parent(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().new("trunk()")
        assert mock_run.call_args == _call("new", "trunk()")

    @patch("repoactive.jj.subprocess.run")
    def test_multiple_parents(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().new("repoactive/a", "repoactive/b")
        assert mock_run.call_args == _call("new", "repoactive/a", "repoactive/b")


class TestBookmarkSet:
    @patch("repoactive.jj.subprocess.run")
    def test_sets_bookmark_at_working_copy(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().bookmark_set("repoactive/foo")
        assert mock_run.call_args == _call(
            "bookmark", "set", "repoactive/foo", "--revision", "@", "--allow-backwards"
        )


class TestBookmarkDelete:
    @patch("repoactive.jj.subprocess.run")
    def test_deletes_bookmark(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().bookmark_delete("repoactive/foo")
        assert mock_run.call_args == _call("bookmark", "delete", "repoactive/foo")


class TestBookmarkExists:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_true_when_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = _BOOKMARKS_OUTPUT
        assert _jj().bookmark_exists("rschmitt/alpine") is True

    @patch("repoactive.jj.subprocess.run")
    def test_returns_false_when_not_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = _BOOKMARKS_OUTPUT
        assert _jj().bookmark_exists("repoactive/missing") is False

    @patch("repoactive.jj.subprocess.run")
    def test_returns_false_when_no_bookmarks(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        assert _jj().bookmark_exists("repoactive/foo") is False

    @patch("repoactive.jj.subprocess.run")
    def test_no_partial_name_match(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "abcdefghijklmnopqrstuvwxyzabcdef rschmitt/foobar\n"
        assert _jj().bookmark_exists("rschmitt/foo") is False


_TEMPLATE = 'if(self.remote(), "", if(self.normal_target(), self.normal_target().change_id() ++ " " ++ self.name() ++ "\\n", ""))'
_BOOKMARKS_OUTPUT = (
    "uxpywmluxktrqztvnqywwlpzwvnyrlzk main\n"
    "klmkpoomqllrzxynwkoozmypqowtpyys rschmitt/alpine\n"
    "wylnnznqvxyvkmxssnmqonostsxysxzx rschmitt/dev\n"
)


class TestBookmarksList:
    @patch("repoactive.jj.subprocess.run")
    def test_calls_correct_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().bookmark_list()
        assert mock_run.call_args == _call("bookmark", "list", "-T", _TEMPLATE)

    @patch("repoactive.jj.subprocess.run")
    def test_parses_multiple_bookmarks(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = _BOOKMARKS_OUTPUT
        result = _jj().bookmark_list()
        assert result == [
            Bookmark(change_id="uxpywmluxktrqztvnqywwlpzwvnyrlzk", name="main"),
            Bookmark(change_id="klmkpoomqllrzxynwkoozmypqowtpyys", name="rschmitt/alpine"),
            Bookmark(change_id="wylnnznqvxyvkmxssnmqonostsxysxzx", name="rschmitt/dev"),
        ]

    @patch("repoactive.jj.subprocess.run")
    def test_empty_output_returns_empty_list(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        assert _jj().bookmark_list() == []

    @patch("repoactive.jj.subprocess.run")
    def test_deleted_tracked_bookmark_excluded(self, mock_run: MagicMock) -> None:
        # jj emits an empty string for bookmarks with no local target (the if() guard)
        mock_run.return_value.stdout = "uxpywmluxktrqztvnqywwlpzwvnyrlzk main\n"
        result = _jj().bookmark_list()
        assert result == [Bookmark(change_id="uxpywmluxktrqztvnqywwlpzwvnyrlzk", name="main")]

    @patch("repoactive.jj.subprocess.run")
    def test_name_with_spaces_parsed_correctly(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "abcdefghijklmnopqrstuvwxyzabcdef my/branch name\n"
        result = _jj().bookmark_list()
        assert result == [
            Bookmark(change_id="abcdefghijklmnopqrstuvwxyzabcdef", name="my/branch name")
        ]


class TestIsEmpty:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_false_when_not_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "false"
        assert _jj().is_empty() is False
        assert mock_run.call_args == _call(
            "log", "-r", "@", "--no-graph", "--template", "json(self.empty())"
        )

    @patch("repoactive.jj.subprocess.run")
    def test_returns_true_when_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "true"
        assert _jj().is_empty() is True


class TestAbandon:
    @patch("repoactive.jj.subprocess.run")
    def test_abandons_working_copy(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().abandon()
        assert mock_run.call_args == _call("abandon", "@")


class TestDescribe:
    @patch("repoactive.jj.subprocess.run")
    def test_sets_message(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().describe("chore: update deps")
        assert mock_run.call_args == _call("describe", "--message", "chore: update deps")

    @patch("repoactive.jj.subprocess.run")
    def test_multiline_message(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().describe("title\n\nbody")
        assert mock_run.call_args == _call("describe", "--message", "title\n\nbody")


class TestGitPushBookmarks:
    @patch("repoactive.jj.subprocess.run")
    def test_pushes_bookmark(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().git_push_bookmarks("repoactive/foo")
        assert mock_run.call_args == _call("git", "push", "--bookmark", "repoactive/foo")

    @patch("repoactive.jj.subprocess.run")
    def test_pushes_multiple_bookmarks(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().git_push_bookmarks("repoactive/a", "repoactive/b")
        assert mock_run.call_args == _call(
            "git", "push", "--bookmark", "repoactive/a", "--bookmark", "repoactive/b"
        )


class TestEdit:
    @patch("repoactive.jj.subprocess.run")
    def test_edits_revision(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().edit("repoactive/foo")
        assert mock_run.call_args == _call("edit", "repoactive/foo")


class TestRestore:
    @patch("repoactive.jj.subprocess.run")
    def test_restores_changes_in(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().restore("repoactive/foo")
        assert mock_run.call_args == _call("restore", "--changes-in", "repoactive/foo")


class TestRebase:
    @patch("repoactive.jj.subprocess.run")
    def test_single_onto(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().rebase("trunk()")
        assert mock_run.call_args == _call("rebase", "-r", "@", "--onto", "trunk()")

    @patch("repoactive.jj.subprocess.run")
    def test_multiple_onto(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().rebase("repoactive/a", "repoactive/b")
        assert mock_run.call_args == _call(
            "rebase", "-r", "@", "--onto", "repoactive/a", "--onto", "repoactive/b"
        )


class TestGetRemoteUrl:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_origin_url(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "origin  https://gitlab.com/org/repo.git\n"
        assert _jj().get_remote_url() == "https://gitlab.com/org/repo.git"

    @patch("repoactive.jj.subprocess.run")
    def test_returns_named_remote(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = (
            "origin  https://gitlab.com/org/repo.git\n"
            "upstream  https://gitlab.com/upstream/repo.git\n"
        )
        assert _jj().get_remote_url("upstream") == "https://gitlab.com/upstream/repo.git"

    @patch("repoactive.jj.subprocess.run")
    def test_raises_when_remote_not_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "origin  https://gitlab.com/org/repo.git\n"
        with pytest.raises(JJError, match="'upstream' not found"):
            _jj().get_remote_url("upstream")

    @patch("repoactive.jj.subprocess.run")
    def test_raises_when_no_remotes(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        with pytest.raises(JJError):
            _jj().get_remote_url()
