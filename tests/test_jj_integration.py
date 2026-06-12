"""Integration tests for the JJ wrapper — runs against a real jj repository."""

import subprocess
from pathlib import Path

import pytest

from repoactive.jj import JJ, JJError

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _init_repo(path: Path) -> JJ:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["jj", "git", "init", "--colocate", str(path)], check=True, capture_output=True)
    (path / ".jj" / "repo" / "config.toml").write_text(
        '[user]\nname = "Test User"\nemail = "test@test.com"\n'
    )
    return JJ(path)


def _description(jj: JJ, rev: str = "@") -> str:
    return subprocess.run(
        ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "description"],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _change_id(jj: JJ, rev: str = "@") -> str:
    return subprocess.run(
        ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "change_id"],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> JJ:
    return _init_repo(tmp_path / "repo")


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[JJ, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    local = _init_repo(tmp_path / "local")
    subprocess.run(
        ["jj", "--no-pager", "git", "remote", "add", "origin", str(remote)],
        cwd=local.cwd,
        check=True,
        capture_output=True,
    )
    return local, remote


class TestIsEmpty:
    def test_new_repo_is_empty(self, repo: JJ) -> None:
        assert repo.is_empty() is True

    def test_false_after_file_added(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("hello")
        assert repo.is_empty() is False


class TestDescribe:
    def test_sets_message(self, repo: JJ) -> None:
        repo.describe("my message")
        assert _description(repo) == "my message"

    def test_multiline_message(self, repo: JJ) -> None:
        repo.describe("title\n\nbody")
        assert _description(repo) == "title\n\nbody"


class TestNew:
    def test_creates_child_of_given_parent(self, repo: JJ) -> None:
        repo.describe("parent")
        repo.new("@")
        assert _description(repo, "@-") == "parent"

    def test_new_commit_is_empty(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("hello")
        repo.new("@")
        assert repo.is_empty() is True

    def test_multiple_parents(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("root")
        repo.new("root")
        repo.describe("a")
        repo.bookmark_set("a")
        repo.new("root")
        repo.describe("b")
        repo.bookmark_set("b")
        repo.new("a", "b")
        parent_descs = set(
            subprocess.run(
                [
                    "jj",
                    "--no-pager",
                    "log",
                    "-r",
                    "parents(@)",
                    "--no-graph",
                    "-T",
                    "description\n",
                ],
                cwd=repo.cwd,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
        )
        assert parent_descs == {"a", "b"}


class TestDiffStat:
    def test_shows_changed_file(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("hello")
        assert "file.txt" in repo.diff_stat()

    def test_unchanged_file_not_in_stat(self, repo: JJ) -> None:
        (repo.cwd / "a.txt").write_text("a")
        repo.describe("base")
        repo.new("@")
        assert "a.txt" not in repo.diff_stat()


class TestEdit:
    def test_switches_working_copy_to_revision(self, repo: JJ) -> None:
        repo.describe("first")
        repo.bookmark_set("first")
        repo.new("@")
        repo.describe("second")
        repo.edit("first")
        assert _description(repo) == "first"


class TestRestore:
    def test_discards_current_commits_own_changes(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("hello")
        assert not repo.is_empty()
        repo.restore("@")
        assert repo.is_empty()


class TestRebase:
    def test_moves_commit_onto_new_parent(self, repo: JJ) -> None:
        repo.describe("root")
        repo.bookmark_set("root")
        repo.new("root")
        repo.describe("a")
        repo.bookmark_set("a")
        repo.new("root")
        repo.describe("b")
        repo.bookmark_set("b")
        repo.edit("a")
        repo.rebase("b")
        assert _description(repo, "@-") == "b"


class TestAbandon:
    def test_deletes_bookmark_on_abandoned_commit(self, repo: JJ) -> None:
        repo.new("@")
        repo.bookmark_set("to-abandon")
        repo.abandon()
        assert not repo.bookmark_exists("to-abandon")


class TestBookmarkSet:
    def test_creates_bookmark_at_working_copy(self, repo: JJ) -> None:
        repo.bookmark_set("mybranch")
        assert repo.bookmark_exists("mybranch") is True

    def test_bookmark_change_id_matches_working_copy(self, repo: JJ) -> None:
        repo.bookmark_set("mybranch")
        bookmarks = repo.bookmark_list()
        b = next(b for b in bookmarks if b.name == "mybranch")
        assert b.change_id == _change_id(repo)

    def test_allows_moving_bookmark_backwards(self, repo: JJ) -> None:
        repo.describe("first")
        repo.bookmark_set("mybranch")
        repo.new("@")
        repo.bookmark_set("mybranch")
        # move back to first — requires --allow-backwards
        repo.bookmark_set("mybranch", "@-")
        bookmarks = repo.bookmark_list()
        b = next(b for b in bookmarks if b.name == "mybranch")
        assert b.change_id == _change_id(repo, "@-")


class TestBookmarkDelete:
    def test_removes_existing_bookmark(self, repo: JJ) -> None:
        repo.bookmark_set("mybranch")
        repo.bookmark_delete("mybranch")
        assert repo.bookmark_exists("mybranch") is False

    def test_raises_for_nonexistent_local_bookmark(self, repo: JJ) -> None:
        # jj exits 0 with a warning for unknown bookmarks — this is expected behaviour
        repo.bookmark_delete("nonexistent")  # must not raise


class TestBookmarkList:
    def test_empty_when_no_bookmarks(self, repo: JJ) -> None:
        assert repo.bookmark_list() == []

    def test_includes_created_bookmark(self, repo: JJ) -> None:
        repo.bookmark_set("mybranch")
        assert any(b.name == "mybranch" for b in repo.bookmark_list())

    def test_excludes_deleted_tracked_bookmark(self, repo_with_remote: tuple[JJ, Path]) -> None:
        local, _ = repo_with_remote
        local.describe("initial commit")
        local.bookmark_set("test/branch")
        local.git_push_bookmarks("test/branch")
        local.bookmark_delete("test/branch")
        # remote-tracking entry survives deletion; must still return False
        assert local.bookmark_exists("test/branch") is False


class TestBookmarkExists:
    def test_true_when_bookmark_exists(self, repo: JJ) -> None:
        repo.bookmark_set("mybranch")
        assert repo.bookmark_exists("mybranch") is True

    def test_false_when_bookmark_missing(self, repo: JJ) -> None:
        assert repo.bookmark_exists("nonexistent") is False

    def test_no_partial_name_match(self, repo: JJ) -> None:
        repo.bookmark_set("mybranch-long")
        assert repo.bookmark_exists("mybranch") is False


class TestGetRemoteUrl:
    def test_returns_origin_url(self, repo_with_remote: tuple[JJ, Path]) -> None:
        local, remote = repo_with_remote
        assert local.get_remote_url() == str(remote)

    def test_raises_when_no_remotes(self, repo: JJ) -> None:
        with pytest.raises(JJError, match="not found"):
            repo.get_remote_url()


class TestJJError:
    def test_raised_for_invalid_revision(self, repo: JJ) -> None:
        with pytest.raises(JJError):
            repo.edit("this-revision-does-not-exist")
