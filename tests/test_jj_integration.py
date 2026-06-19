"""Integration tests for the JJ wrapper — runs against a real jj repository."""

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from repoactive.jj import JJ, WORKSPACE_PREFIX, JJError

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _init_repo(path: Path, *, colocate: bool = True) -> JJ:
    path.mkdir(parents=True, exist_ok=True)
    args = (
        ["jj", "git", "init", "--colocate", str(path)]
        if colocate
        else ["jj", "--config=git.colocate=false", "git", "init", str(path)]
    )
    subprocess.run(args, check=True, capture_output=True)
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


def _commit_id(jj: JJ, rev: str = "@") -> str:
    return subprocess.run(
        ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "commit_id"],
        cwd=jj.cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
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


class TestChangeId:
    def test_returns_short_prefix_of_full_change_id(self, repo: JJ) -> None:
        short = repo.change_id()
        full = _change_id(repo)
        assert short
        assert full.startswith(short)
        assert len(short) < len(full)

    def test_stable_across_describe(self, repo: JJ) -> None:
        before = repo.change_id()
        repo.describe("a message")
        assert repo.change_id() == before

    def test_changes_after_new(self, repo: JJ) -> None:
        before = repo.change_id()
        repo.new("@")
        assert repo.change_id() != before

    def test_defaults_to_working_copy(self, repo: JJ) -> None:
        assert repo.change_id() == repo.change_id("@")

    def test_returns_change_id_of_named_revision(self, repo: JJ) -> None:
        repo.describe("parent")
        parent = repo.change_id()
        repo.new("@")
        assert repo.change_id("@-") == parent

    def test_raises_for_invalid_revision(self, repo: JJ) -> None:
        with pytest.raises(JJError):
            repo.change_id("does-not-exist")


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


class TestHasRecentJobCommit:
    @staticmethod
    def _day_ago() -> datetime:
        return datetime.now(UTC) - timedelta(days=1)

    @staticmethod
    def _day_ahead() -> datetime:
        return datetime.now(UTC) + timedelta(days=1)

    def test_finds_commit_with_trailer_in_window(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        assert repo.has_recent_job_commit("my-job", "@", self._day_ago()) is True

    def test_commit_outside_window_not_found(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        # since is in the future, so the just-made commit predates it
        assert repo.has_recent_job_commit("my-job", "@", self._day_ahead()) is False

    def test_commit_without_trailer_not_found(self, repo: JJ) -> None:
        repo.describe("upgrade deps")
        assert repo.has_recent_job_commit("my-job", "@", self._day_ago()) is False

    def test_different_job_name_not_found(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: other-job")
        assert repo.has_recent_job_commit("my-job", "@", self._day_ago()) is False

    def test_trailer_like_body_line_not_matched(self, repo: JJ) -> None:
        # The matching line is in the body, not the final (trailer) paragraph.
        repo.describe("Repoactive-Job: my-job\n\nthe real body comes after")
        assert repo.has_recent_job_commit("my-job", "@", self._day_ago()) is False

    def test_matches_only_on_given_base(self, repo: JJ) -> None:
        repo.describe("on main\n\nRepoactive-Job: my-job")
        repo.bookmark_set("main")
        # a sibling branch off the root, not descending from the job commit
        repo.new("root()")
        repo.describe("unrelated work")
        repo.bookmark_set("other")
        # the trailer is reachable from "main" but not from the sibling "other"
        assert repo.has_recent_job_commit("my-job", "main", self._day_ago()) is True
        assert repo.has_recent_job_commit("my-job", "other", self._day_ago()) is False

    def test_finds_commit_in_ancestor_not_just_tip(self, repo: JJ) -> None:
        # The job commit is an ancestor of base, not base itself.
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        repo.new("@")
        repo.describe("later unrelated work")
        # base "@" is the tip; the trailer lives one commit below it
        assert repo.has_recent_job_commit("my-job", "@", self._day_ago()) is True


class TestRecentJobCommits:
    @staticmethod
    def _day_ago() -> datetime:
        return datetime.now(UTC) - timedelta(days=1)

    @staticmethod
    def _day_ahead() -> datetime:
        return datetime.now(UTC) + timedelta(days=1)

    def test_returns_commit_with_populated_fields(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        commits = repo.recent_job_commits(self._day_ago())
        assert len(commits) == 1
        commit = commits[0]
        assert commit.job_name == "my-job"
        assert commit.subject == "upgrade deps"
        assert commit.commit_id == _commit_id(repo)[: len(commit.commit_id)]
        assert commit.change_id == _change_id(repo)[: len(commit.change_id)]
        assert commit.relative_age  # non-empty human-readable age

    def test_excludes_commit_without_trailer(self, repo: JJ) -> None:
        repo.describe("upgrade deps")
        assert repo.recent_job_commits(self._day_ago()) == []

    def test_excludes_commit_outside_window(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        # since is in the future, so the just-made commit predates it
        assert repo.recent_job_commits(self._day_ahead()) == []

    def test_returns_multiple_commits_newest_first(self, repo: JJ) -> None:
        repo.describe("older\n\nRepoactive-Job: job-a")
        repo.new("@")
        repo.describe("newer\n\nRepoactive-Job: job-b")
        commits = repo.recent_job_commits(self._day_ago())
        assert [c.job_name for c in commits] == ["job-b", "job-a"]

    def test_joins_multiple_trailer_values(self, repo: JJ) -> None:
        repo.describe("shared work\n\nRepoactive-Job: job-a\nRepoactive-Job: job-b")
        commits = repo.recent_job_commits(self._day_ago())
        assert len(commits) == 1
        assert commits[0].job_name == "job-a,job-b"

    def test_revset_restricts_to_matching_commits(self, repo: JJ) -> None:
        repo.describe("on main\n\nRepoactive-Job: my-job")
        repo.bookmark_set("main")
        # a sibling branch off the root, not descending from the job commit
        repo.new("root()")
        repo.describe("unrelated\n\nRepoactive-Job: other-job")
        repo.bookmark_set("other")
        names = {c.job_name for c in repo.recent_job_commits(self._day_ago(), revset="::main")}
        assert names == {"my-job"}

    def test_empty_when_no_job_commits(self, repo: JJ) -> None:
        assert repo.recent_job_commits(self._day_ago()) == []


class TestGit:
    def test_runs_git_subcommand(self, repo: JJ) -> None:
        assert repo._git("rev-parse", "--git-dir").strip()

    def test_unknown_subcommand_raises_jj_error(self, repo: JJ) -> None:
        with pytest.raises(JJError, match="git no-such-subcommand failed"):
            repo._git("no-such-subcommand")


class TestWorkspaceColocation:
    @staticmethod
    def _commit(repo: JJ, filename: str, message: str) -> None:
        (repo.cwd / filename).write_text(filename)
        repo.describe(message)
        repo.new("@")

    def test_workspace_is_colocated(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)
        assert (ws_path / ".git").is_file()
        ws = JJ(ws_path)
        assert _git(ws_path, "rev-parse", "HEAD") == _commit_id(ws, "@-")

    def test_git_status_clean_in_new_workspace(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)
        assert _git(ws_path, "status", "--porcelain") == ""

    def test_git_sync_head_after_moving_working_copy(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "first")
        repo.bookmark_set("base", "@-")
        self._commit(repo, "b.txt", "second")
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)
        ws = JJ(ws_path)
        ws.new("base")
        ws.git_sync_head()
        assert _git(ws_path, "rev-parse", "HEAD") == _commit_id(ws, "@-")
        assert _git(ws_path, "status", "--porcelain") == ""

    def test_git_sync_head_noop_without_git(self, repo: JJ, tmp_path: Path) -> None:
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)  # empty repo: colocation is skipped
        JJ(ws_path).git_sync_head()  # must not raise

    def test_empty_repo_workspace_not_colocated(self, repo: JJ, tmp_path: Path) -> None:
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)
        assert not (ws_path / ".git").exists()
        assert JJ(ws_path).is_empty() is True  # jj still works in the workspace

    def test_non_colocated_repo_workspace(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "plain", colocate=False)
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)
        assert not (ws_path / ".git").exists()

    def test_jj_and_git_agree_after_commit(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)
        ws = JJ(ws_path)
        (ws_path / "b.txt").write_text("b")
        ws.describe("from workspace")
        ws.new("@")
        ws.git_sync_head()
        assert _git(ws_path, "log", "-1", "--format=%s") == "from workspace"
        assert _git(ws_path, "status", "--porcelain") == ""

    def test_prune_after_forget(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo.workspace_add("ws", ws_path)
        repo.workspace_forget("ws")
        shutil.rmtree(ws_path)
        repo.git_worktree_prune()
        assert _git(repo.cwd, "worktree", "list", "--porcelain").count("worktree ") == 1

    def test_workspace_names_lists_added_workspace(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        repo.workspace_add("ws", tmp_path / "ws")
        assert sorted(repo.workspace_names()) == ["default", "ws"]

    def test_forget_stale_workspaces_drops_only_prefixed(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        stale = f"{WORKSPACE_PREFIX}job"
        repo.workspace_add(stale, tmp_path / "stale")
        repo.workspace_add("mine", tmp_path / "mine")
        shutil.rmtree(tmp_path / "stale")

        repo.forget_stale_workspaces()

        assert sorted(repo.workspace_names()) == ["default", "mine"]
        # The dead git worktree of the forgotten workspace is pruned too.
        worktrees = _git(repo.cwd, "worktree", "list", "--porcelain")
        assert f"worktree {tmp_path / 'stale'}" not in worktrees
        assert f"worktree {tmp_path / 'mine'}" in worktrees
