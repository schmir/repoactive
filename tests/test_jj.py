"""Tests for the JJ wrapper class."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import MagicMock, call, patch

import pytest

from repoactive.jj import (
    JJ,
    WORKSPACE_PREFIX,
    Bookmark,
    CommandFailedError,
    JJNotFoundError,
    MissingGitDirError,
    NotAJJRepoError,
    NotColocatedGitRepoError,
    RemoteNotFoundError,
    _jj_timestamp,
    require_colocated_repo,
    require_jj_on_path,
    workspace_name,
)

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
            pytest.raises(CommandFailedError, match="bad state"),
        ):
            _jj().new("trunk()")

    def test_empty_stderr_in_error(self) -> None:
        err = CalledProcessError(1, "jj", stderr="")
        with (
            patch("repoactive.jj.subprocess.run", side_effect=err),
            pytest.raises(CommandFailedError),
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


class TestGitInitColocate:
    @patch("repoactive.jj.subprocess.run")
    def test_runs_git_init_colocate(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().git_init_colocate()
        assert mock_run.call_args == _call("git", "init", "--colocate")


class TestOp:
    @patch("repoactive.jj.subprocess.run")
    def test_op_id_returns_stripped_id(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "abc123\n"
        assert _jj().op_id() == "abc123"
        assert mock_run.call_args == _call(
            "op", "log", "--no-graph", "--limit", "1", "-T", "id.short()"
        )


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


class TestRemoteBookmarkExists:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_true_when_remote_bookmark_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "1\n"
        assert _jj().remote_bookmark_exists("repoactive/foo") is True

    @patch("repoactive.jj.subprocess.run")
    def test_returns_false_when_no_remote_bookmark(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        assert _jj().remote_bookmark_exists("repoactive/foo") is False

    @patch("repoactive.jj.subprocess.run")
    def test_passes_name_in_template(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().remote_bookmark_exists("repoactive/my-job")
        template = mock_run.call_args[0][0][-1]
        assert '"repoactive/my-job"' in template


_TEMPLATE = """
        if(self.remote(), "",
           if(self.normal_target(),
              self.normal_target().change_id() ++ " " ++ self.name() ++ "\\n",
              ""
           )
        )
        """
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

    @patch("repoactive.jj.subprocess.run")
    def test_does_not_call_jj_when_no_bookmarks(self, mock_run: MagicMock) -> None:
        _jj().git_push_bookmarks()
        mock_run.assert_not_called()


class TestEdit:
    @patch("repoactive.jj.subprocess.run")
    def test_edits_revision(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().edit("repoactive/foo")
        assert mock_run.call_args == _call("edit", "repoactive/foo")


class TestRestore:
    @patch("repoactive.jj.subprocess.run")
    def test_restores_from_source_into_destination(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().restore(source_rev="repoactive/foo", destination_rev="@")
        assert mock_run.call_args == _call("restore", "--from", "repoactive/foo", "--into", "@")


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


class TestWorkspaceAdd:
    @patch("repoactive.jj.subprocess.run")
    def test_not_colocated_only_adds_workspace(self, mock_run: MagicMock) -> None:
        # REPO has no .git directory, so no git worktree setup happens
        mock_run.return_value.stdout = ""
        _jj()._workspace_add("ws", Path("/work/ws"))
        assert mock_run.call_args_list == [_call("workspace", "add", "--name", "ws", "/work/ws")]


class TestWorkspaceName:
    def test_prefixes_job_name(self) -> None:
        assert workspace_name("foo") == f"{WORKSPACE_PREFIX}foo"


class TestForgetStaleWorkspaces:
    @patch("repoactive.jj.subprocess.run")
    def test_forgets_only_prefixed_workspaces(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = f"default\n{WORKSPACE_PREFIX}a\n{WORKSPACE_PREFIX}b\n"
        _jj().forget_stale_workspaces()
        # REPO has no .git directory, so no worktree prune happens.
        assert mock_run.call_args_list == [
            _call("workspace", "list", "-T", 'name ++ "\\n"'),
            _call("workspace", "forget", f"{WORKSPACE_PREFIX}a"),
            _call("workspace", "forget", f"{WORKSPACE_PREFIX}b"),
        ]

    @patch("repoactive.jj.subprocess.run")
    def test_keeps_unrelated_workspaces(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "default\nmine\n"
        _jj().forget_stale_workspaces()
        assert mock_run.call_args_list == [_call("workspace", "list", "-T", 'name ++ "\\n"')]


class TestGitSyncHead:
    @patch("repoactive.jj.subprocess.run")
    def test_noop_when_not_colocated(self, mock_run: MagicMock) -> None:
        _jj().git_sync_head()
        mock_run.assert_not_called()


class TestGitWorktreePrune:
    @patch("repoactive.jj.subprocess.run")
    def test_noop_when_not_colocated(self, mock_run: MagicMock) -> None:
        _jj().git_worktree_prune()
        mock_run.assert_not_called()


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
        with pytest.raises(RemoteNotFoundError, match="'upstream' not found"):
            _jj().get_remote_url("upstream")

    @patch("repoactive.jj.subprocess.run")
    def test_raises_when_no_remotes(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        with pytest.raises(RemoteNotFoundError):
            _jj().get_remote_url()


class TestRequireJJOnPath:
    def test_accepts_when_on_path(self) -> None:
        with patch("repoactive.jj.shutil.which", return_value="/usr/bin/jj"):
            require_jj_on_path()  # does not raise

    def test_rejects_when_missing(self) -> None:
        with (
            patch("repoactive.jj.shutil.which", return_value=None),
            pytest.raises(JJNotFoundError, match=r"docs\.jj-vcs\.dev"),
        ):
            require_jj_on_path()


class TestEnsureColocatedRepo:
    def test_accepts_colocated_repo(self, tmp_path: Path) -> None:
        (tmp_path / ".jj").mkdir()
        (tmp_path / ".git").mkdir()
        require_colocated_repo(tmp_path)  # does not raise

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


class TestUnmergedJobNames:
    @patch("repoactive.jj.subprocess.run")
    def test_parses_job_names(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "uv-lock-upgrade\nprek-autoupdate\n"
        assert _jj().pending_job_names() == {"uv-lock-upgrade", "prek-autoupdate"}

    @patch("repoactive.jj.subprocess.run")
    def test_splits_multiple_trailers_on_one_commit(self, mock_run: MagicMock) -> None:
        # A generated job's commit carries its own name and the generator's,
        # comma-joined; both must be returned (see ADR 0004).
        mock_run.return_value.stdout = "deps-pkg-a,per-package\ndeps-pkg-b,per-package\n"
        assert _jj().pending_job_names() == {"deps-pkg-a", "deps-pkg-b", "per-package"}

    @patch("repoactive.jj.subprocess.run")
    def test_empty_when_no_unmerged_commits(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        assert _jj().pending_job_names() == set()

    @patch("repoactive.jj.subprocess.run")
    def test_queries_unmerged_revset(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().pending_job_names()
        args = mock_run.call_args[0][0]
        assert "log" in args
        assert "~(::trunk())" in args

    @patch("repoactive.jj.subprocess.run")
    def test_revset_wraps_descendants_and_filters_unmerged(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().pending_job_names(revset="present(repoactive/a)")
        args = mock_run.call_args[0][0]
        joined = " ".join(args)
        assert "descendants(present(repoactive/a))" in joined
        assert "~(::trunk())" in joined

    @patch("repoactive.jj.subprocess.run")
    def test_revset_parses_job_names(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "my-job\n"
        result = _jj().pending_job_names(revset="present(repoactive/a)")
        assert result == {"my-job"}

    @patch("repoactive.jj.subprocess.run")
    def test_empty_revset_returns_empty_without_querying(self, mock_run: MagicMock) -> None:
        result = _jj().pending_job_names(revset="")
        assert result == set()
        mock_run.assert_not_called()


class TestAbandonRevision:
    @patch("repoactive.jj.subprocess.run")
    def test_abandons_specific_revision(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().abandon_revision("abc123")
        assert mock_run.call_args == _call("abandon", "abc123")


class TestSameContent:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_true_when_diff_is_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        assert _jj().same_content("abc123", "def456") is True
        assert mock_run.call_args == _call("diff", "--git", "--from", "abc123", "--to", "def456")

    @patch("repoactive.jj.subprocess.run")
    def test_returns_false_when_diff_is_nonempty(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "diff --git a/foo b/foo\n+content\n"
        assert _jj().same_content("abc123", "def456") is False


class TestBookmarkChangeId:
    @patch("repoactive.jj.subprocess.run")
    def test_returns_change_id_for_existing_bookmark(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "zzzzabc repoactive/foo\n"
        assert _jj().bookmark_change_id("repoactive/foo") == "zzzzabc"

    @patch("repoactive.jj.subprocess.run")
    def test_returns_none_for_missing_bookmark(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = "zzzzabc repoactive/other\n"
        assert _jj().bookmark_change_id("repoactive/foo") is None

    @patch("repoactive.jj.subprocess.run")
    def test_returns_none_when_no_bookmarks(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        assert _jj().bookmark_change_id("repoactive/foo") is None


class TestRebaseRevision:
    @patch("repoactive.jj.subprocess.run")
    def test_single_parent(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().rebase_revision("abc123", "trunk()")
        assert mock_run.call_args == _call("rebase", "-r", "abc123", "--onto", "trunk()")

    @patch("repoactive.jj.subprocess.run")
    def test_multiple_parents(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().rebase_revision("abc123", "repoactive/a", "repoactive/b")
        assert mock_run.call_args == _call(
            "rebase", "-r", "abc123", "--onto", "repoactive/a", "--onto", "repoactive/b"
        )


class TestDescribeRevision:
    @patch("repoactive.jj.subprocess.run")
    def test_sets_message_on_specific_revision(self, mock_run: MagicMock) -> None:
        mock_run.return_value.stdout = ""
        _jj().describe_revision("abc123", "chore: update deps")
        assert mock_run.call_args == _call(
            "describe", "-r", "abc123", "--message", "chore: update deps"
        )


class TestUnmergedJobNamesWithRevset:
    @patch("repoactive.jj.subprocess.run")
    def test_multi_bookmark_revset_includes_both_in_descendants_query(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.return_value.stdout = ""
        _jj().pending_job_names(revset="present(repoactive/a) | present(repoactive/b)")
        args = mock_run.call_args[0][0]
        assert "present(repoactive/a)" in " ".join(args)
        assert "present(repoactive/b)" in " ".join(args)


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
