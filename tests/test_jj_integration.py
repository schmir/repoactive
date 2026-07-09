"""Integration tests for the JJ wrapper — runs against a real jj repository."""

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from repoactive.jj import (
    JJ,
    WORKSPACE_PREFIX,
    CommandFailedError,
    MissingGitDirError,
    NotAJJRepoError,
    NotColocatedGitRepoError,
    RemoteNotFoundError,
    require_colocated_repo,
)

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


def _has_conflict(jj: JJ, rev: str) -> bool:
    return (
        subprocess.run(
            ["jj", "--no-pager", "log", "-r", rev, "--no-graph", "-T", "conflict"],
            cwd=jj.cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == "true"
    )


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


class TestGetDescription:
    def test_returns_description_of_revision(self, repo: JJ) -> None:
        repo.describe("my message")
        assert repo.get_description("@") == "my message\n"

    def test_multiline_description(self, repo: JJ) -> None:
        repo.describe("title\n\nbody")
        assert repo.get_description("@") == "title\n\nbody\n"

    def test_empty_description(self, repo: JJ) -> None:
        assert repo.get_description("@") == ""

    def test_returns_description_of_ancestor(self, repo: JJ) -> None:
        repo.describe("parent")
        repo.new("@")
        assert repo.get_description("@-") == "parent\n"

    def test_raises_for_invalid_revision(self, repo: JJ) -> None:
        with pytest.raises(CommandFailedError):
            repo.get_description("does-not-exist")


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
        with pytest.raises(CommandFailedError):
            repo.change_id("does-not-exist")


class TestOpId:
    def test_returns_non_empty_id(self, repo: JJ) -> None:
        assert repo.op_id()

    def test_matches_op_log_head(self, repo: JJ) -> None:
        head = subprocess.run(
            ["jj", "--no-pager", "op", "log", "--no-graph", "--limit", "1", "-T", "id.short()"],
            cwd=repo.cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert repo.op_id() == head

    def test_changes_after_operation(self, repo: JJ) -> None:
        before = repo.op_id()
        repo.describe("a message")
        assert repo.op_id() != before


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
        repo.restore(source_rev="@-", destination_rev="@")
        assert repo.is_empty()

    def test_copies_file_content_from_source_into_destination(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("from source")
        repo.describe("source")
        repo.bookmark_set("source")
        repo.new("@")
        (repo.cwd / "file.txt").write_text("from destination")
        repo.describe("destination")
        repo.bookmark_set("destination")
        repo.new("@")
        repo.restore(source_rev="source", destination_rev="destination")
        repo.edit("destination")
        assert (repo.cwd / "file.txt").read_text() == "from source"

    def test_resolves_conflict_in_destination(self, repo: JJ) -> None:
        # Simulate the bug: rebase causes a conflict in "old", then restore
        # should copy "new"s content into "old" to resolve it.
        (repo.cwd / "file.txt").write_text("base")
        repo.describe("base")
        repo.bookmark_set("base")

        repo.new("base")
        (repo.cwd / "file.txt").write_text("new content")
        repo.describe("new")
        repo.bookmark_set("new")

        repo.new("base")
        (repo.cwd / "file.txt").write_text("conflicting")
        repo.describe("old")
        repo.bookmark_set("old")

        repo.rebase("new")
        assert _has_conflict(repo, "old")

        repo.restore(source_rev="new", destination_rev="old")
        assert not _has_conflict(repo, "old")
        repo.edit("old")
        assert (repo.cwd / "file.txt").read_text() == "new content"


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


class TestSameContent:
    def test_identical_commits_return_true(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("hello")
        repo.describe("first")
        repo.bookmark_set("first")
        repo.new("first")
        (repo.cwd / "file.txt").write_text("hello")
        repo.describe("second")
        assert repo.same_content("first", "@") is True

    def test_different_commits_return_false(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("hello")
        repo.describe("first")
        repo.bookmark_set("first")
        repo.new("first")
        (repo.cwd / "file.txt").write_text("world")
        repo.describe("second")
        assert repo.same_content("first", "@") is False

    def test_extra_symlink_detected(self, repo: JJ) -> None:
        (repo.cwd / "file.txt").write_text("hello")
        repo.describe("first")
        repo.bookmark_set("first")
        repo.new("first")
        (repo.cwd / "file.txt").write_text("hello")
        (repo.cwd / "link.txt").symlink_to("file.txt")
        repo.describe("second")
        assert repo.same_content("first", "@") is False

    def test_identical_binary_files_return_true(self, repo: JJ) -> None:
        (repo.cwd / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
        repo.describe("first")
        repo.bookmark_set("first")
        repo.new("first")
        (repo.cwd / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
        repo.describe("second")
        assert repo.same_content("first", "@") is True

    def test_different_binary_files_return_false(self, repo: JJ) -> None:
        (repo.cwd / "data.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
        repo.describe("first")
        repo.bookmark_set("first")
        repo.new("first")
        (repo.cwd / "data.bin").write_bytes(b"\x00\x01\x02\xff\x00")
        repo.describe("second")
        assert repo.same_content("first", "@") is False


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

    def test_does_not_raise_for_nonexistent_local_bookmark(self, repo: JJ) -> None:
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


class TestBookmarkTrack:
    def test_tracks_fetched_remote_bookmark(
        self, repo_with_remote: tuple[JJ, Path], tmp_path: Path
    ) -> None:
        local, remote = repo_with_remote
        # A second clone pushes a bookmark to the shared remote.
        other = _init_repo(tmp_path / "other")
        subprocess.run(
            ["jj", "--no-pager", "git", "remote", "add", "origin", str(remote)],
            cwd=other.cwd,
            check=True,
            capture_output=True,
        )
        other.describe("shared work")
        other.bookmark_set("feature")
        other.git_push_bookmarks("feature")
        # local fetches it; with jj's default the remote bookmark stays untracked,
        # so no local "feature" bookmark exists yet.
        subprocess.run(
            ["jj", "--no-pager", "git", "fetch"],
            cwd=local.cwd,
            check=True,
            capture_output=True,
        )
        assert local.bookmark_exists("feature") is False
        local.bookmark_track("feature")
        assert local.bookmark_exists("feature") is True

    def test_tracks_multiple_bookmarks(
        self, repo_with_remote: tuple[JJ, Path], tmp_path: Path
    ) -> None:
        local, remote = repo_with_remote
        other = _init_repo(tmp_path / "other")
        subprocess.run(
            ["jj", "--no-pager", "git", "remote", "add", "origin", str(remote)],
            cwd=other.cwd,
            check=True,
            capture_output=True,
        )
        other.describe("shared work")
        other.bookmark_set("feature-a")
        other.bookmark_set("feature-b")
        other.git_push_bookmarks("feature-a", "feature-b")
        subprocess.run(
            ["jj", "--no-pager", "git", "fetch"],
            cwd=local.cwd,
            check=True,
            capture_output=True,
        )
        local.bookmark_track("feature-a", "feature-b")
        assert local.bookmark_exists("feature-a") is True
        assert local.bookmark_exists("feature-b") is True

    def test_no_bookmarks_is_noop(self, repo: JJ) -> None:
        repo.bookmark_track()  # must not raise


class TestGetRemoteUrl:
    def test_returns_origin_url(self, repo_with_remote: tuple[JJ, Path]) -> None:
        local, remote = repo_with_remote
        assert local.get_remote_url() == str(remote)

    def test_raises_when_no_remotes(self, repo: JJ) -> None:
        with pytest.raises(RemoteNotFoundError, match="not found"):
            repo.get_remote_url()


class TestJJError:
    def test_raised_for_invalid_revision(self, repo: JJ) -> None:
        with pytest.raises(CommandFailedError):
            repo.edit("this-revision-does-not-exist")


class TestLastJobCommitDate:
    @staticmethod
    def _day_ago() -> datetime:
        return datetime.now(UTC) - timedelta(days=1)

    @staticmethod
    def _day_ahead() -> datetime:
        return datetime.now(UTC) + timedelta(days=1)

    def test_finds_commit_with_trailer_in_window(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="@", since=self._day_ago())
            is not None
        )

    def test_commit_outside_window_not_found(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        # since is in the future, so the just-made commit predates it
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="@", since=self._day_ahead())
            is None
        )

    def test_commit_without_trailer_not_found(self, repo: JJ) -> None:
        repo.describe("upgrade deps")
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="@", since=self._day_ago())
            is None
        )

    def test_different_job_name_not_found(self, repo: JJ) -> None:
        repo.describe("upgrade deps\n\nRepoactive-Job: other-job")
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="@", since=self._day_ago())
            is None
        )

    def test_matches_any_of_several_names(self, repo: JJ) -> None:
        # A commit carrying a superset's trailer is found when that name is among
        # job_names (cooldown_on, ADR 0015), and not found when it is absent.
        repo.describe("full upgrade\n\nRepoactive-Job: full-lock")
        assert (
            repo.last_job_commit_date(
                job_names={"dev-lock", "full-lock"}, base="@", since=self._day_ago()
            )
            is not None
        )
        assert (
            repo.last_job_commit_date(job_names={"dev-lock"}, base="@", since=self._day_ago())
            is None
        )

    def test_trailer_like_body_line_not_matched(self, repo: JJ) -> None:
        # The matching line is in the body, not the final (trailer) paragraph.
        repo.describe("Repoactive-Job: my-job\n\nthe real body comes after")
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="@", since=self._day_ago())
            is None
        )

    def test_matches_only_on_given_base(self, repo: JJ) -> None:
        repo.describe("on main\n\nRepoactive-Job: my-job")
        repo.bookmark_set("main")
        # a sibling branch off the root, not descending from the job commit
        repo.new("root()")
        repo.describe("unrelated work")
        repo.bookmark_set("other")
        # the trailer is reachable from "main" but not from the sibling "other"
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="main", since=self._day_ago())
            is not None
        )
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="other", since=self._day_ago())
            is None
        )

    def test_finds_commit_in_ancestor_not_just_tip(self, repo: JJ) -> None:
        # The job commit is an ancestor of base, not base itself.
        repo.describe("upgrade deps\n\nRepoactive-Job: my-job")
        repo.new("@")
        repo.describe("later unrelated work")
        # base "@" is the tip; the trailer lives one commit below it
        assert (
            repo.last_job_commit_date(job_names={"my-job"}, base="@", since=self._day_ago())
            is not None
        )


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
        assert commit.job_names == {"my-job"}
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
        assert [c.job_names for c in commits] == [{"job-b"}, {"job-a"}]

    def test_collects_multiple_trailer_values(self, repo: JJ) -> None:
        repo.describe("shared work\n\nRepoactive-Job: job-a\nRepoactive-Job: job-b")
        commits = repo.recent_job_commits(self._day_ago())
        assert len(commits) == 1
        assert commits[0].job_names == {"job-a", "job-b"}

    def test_revset_restricts_to_matching_commits(self, repo: JJ) -> None:
        repo.describe("on main\n\nRepoactive-Job: my-job")
        repo.bookmark_set("main")
        # a sibling branch off the root, not descending from the job commit
        repo.new("root()")
        repo.describe("unrelated\n\nRepoactive-Job: other-job")
        repo.bookmark_set("other")
        names = {
            name
            for c in repo.recent_job_commits(self._day_ago(), revset="::main")
            for name in c.job_names
        }
        assert names == {"my-job"}

    def test_empty_when_no_job_commits(self, repo: JJ) -> None:
        assert repo.recent_job_commits(self._day_ago()) == []


class TestUnmergedJobNames:
    @staticmethod
    def _establish_trunk(local: JJ) -> None:
        # Push a "main" bookmark so trunk() resolves to it instead of root().
        local.describe("base")
        local.bookmark_set("main")
        local.git_push_bookmarks("main")
        local.new("main")

    def test_returns_trailer_of_unmerged_commit(self, repo_with_remote: tuple[JJ, Path]) -> None:
        local, _ = repo_with_remote
        self._establish_trunk(local)
        local.describe("work\n\nRepoactive-Job: job-x")
        assert local.pending_job_names() == {"job-x"}

    def test_excludes_commit_merged_into_trunk(self, repo_with_remote: tuple[JJ, Path]) -> None:
        local, _ = repo_with_remote
        self._establish_trunk(local)
        local.describe("work\n\nRepoactive-Job: job-x")
        # land the job commit on trunk: it becomes an ancestor of trunk()
        local.bookmark_set("main")
        local.git_push_bookmarks("main")
        assert local.pending_job_names() == set()

    def test_excludes_unmerged_commit_without_trailer(
        self, repo_with_remote: tuple[JJ, Path]
    ) -> None:
        local, _ = repo_with_remote
        self._establish_trunk(local)
        local.describe("plain work, no trailer")
        assert local.pending_job_names() == set()

    def test_returns_multiple_job_names(self, repo_with_remote: tuple[JJ, Path]) -> None:
        local, _ = repo_with_remote
        self._establish_trunk(local)
        local.describe("a\n\nRepoactive-Job: job-a")
        local.new("main")
        local.describe("b\n\nRepoactive-Job: job-b")
        assert local.pending_job_names() == {"job-a", "job-b"}

    def test_empty_when_nothing_unmerged(self, repo_with_remote: tuple[JJ, Path]) -> None:
        local, _ = repo_with_remote
        self._establish_trunk(local)
        # only the empty working copy sits above trunk; it has no trailer
        assert local.pending_job_names() == set()


class TestRequireColocatedRepo:
    def test_accepts_colocated_repo(self, repo: JJ) -> None:
        require_colocated_repo(repo.cwd)  # must not raise

    def test_rejects_non_colocated_jj_repo(self, tmp_path: Path) -> None:
        plain = _init_repo(tmp_path / "plain", colocate=False)
        assert not (plain.cwd / ".git").exists()
        with pytest.raises(MissingGitDirError, match=r"no \.git directory"):
            require_colocated_repo(plain.cwd)

    def test_rejects_git_only_repo(self, tmp_path: Path) -> None:
        path = tmp_path / "gitonly"
        path.mkdir()
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        with pytest.raises(NotColocatedGitRepoError, match="not colocated with jj"):
            require_colocated_repo(path)

    def test_rejects_plain_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "empty"
        path.mkdir()
        with pytest.raises(NotAJJRepoError, match="not a jj repository"):
            require_colocated_repo(path)


class TestGit:
    def test_runs_git_subcommand(self, repo: JJ) -> None:
        assert repo._git("rev-parse", "--git-dir").strip()

    def test_unknown_subcommand_raises_jj_error(self, repo: JJ) -> None:
        with pytest.raises(CommandFailedError, match="git no-such-subcommand failed"):
            repo._git("no-such-subcommand")


class TestTempWorkspace:
    @staticmethod
    def _commit(repo: JJ, filename: str, message: str) -> None:
        (repo.cwd / filename).write_text(filename)
        repo.describe(message)
        repo.new("@")

    def test_yields_usable_workspace(self, repo: JJ) -> None:
        self._commit(repo, "a.txt", "initial")
        with repo.temp_workspace("ws") as ws:
            assert ws.cwd.is_dir()
            assert "ws" in repo.workspace_names()
            ws.new("@")
            ws.git_sync_head()
            assert _git(ws.cwd, "status", "--porcelain") == ""

    def test_cleans_up_on_exit(self, repo: JJ) -> None:
        self._commit(repo, "a.txt", "initial")
        with repo.temp_workspace("ws") as ws:
            ws_path = ws.cwd
        assert not ws_path.exists()
        assert "ws" not in repo.workspace_names()
        worktrees = _git(repo.cwd, "worktree", "list", "--porcelain")
        assert str(ws_path) not in worktrees

    def test_cleans_up_on_exception(self, repo: JJ) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path: Path | None = None
        raised = False
        try:
            with repo.temp_workspace("ws") as ws:
                ws_path = ws.cwd
                raise RuntimeError("boom")
        except RuntimeError:
            raised = True
        assert raised
        assert ws_path is not None
        assert not ws_path.exists()
        assert "ws" not in repo.workspace_names()

    def test_uses_given_workspace_name(self, repo: JJ) -> None:
        self._commit(repo, "a.txt", "initial")
        with repo.temp_workspace(f"{WORKSPACE_PREFIX}myjob"):
            assert f"{WORKSPACE_PREFIX}myjob" in repo.workspace_names()


class TestWorkspaceColocation:
    @staticmethod
    def _commit(repo: JJ, filename: str, message: str) -> None:
        (repo.cwd / filename).write_text(filename)
        repo.describe(message)
        repo.new("@")

    def test_workspace_is_colocated(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo._workspace_add("ws", ws_path)
        assert (ws_path / ".git").is_file()
        ws = JJ(ws_path)
        assert _git(ws_path, "rev-parse", "HEAD") == _commit_id(ws, "@-")

    def test_git_status_clean_in_new_workspace(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo._workspace_add("ws", ws_path)
        assert _git(ws_path, "status", "--porcelain") == ""

    def test_git_sync_head_after_moving_working_copy(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "first")
        repo.bookmark_set("base", "@-")
        self._commit(repo, "b.txt", "second")
        ws_path = tmp_path / "ws"
        repo._workspace_add("ws", ws_path)
        ws = JJ(ws_path)
        ws.new("base")
        ws.git_sync_head()
        assert _git(ws_path, "rev-parse", "HEAD") == _commit_id(ws, "@-")
        assert _git(ws_path, "status", "--porcelain") == ""

    def test_git_sync_head_noop_without_git(self, repo: JJ, tmp_path: Path) -> None:
        ws_path = tmp_path / "ws"
        repo._workspace_add("ws", ws_path)  # empty repo: colocation is skipped
        JJ(ws_path).git_sync_head()  # must not raise

    def test_empty_repo_workspace_not_colocated(self, repo: JJ, tmp_path: Path) -> None:
        ws_path = tmp_path / "ws"
        repo._workspace_add("ws", ws_path)
        assert not (ws_path / ".git").exists()
        assert JJ(ws_path).is_empty() is True  # jj still works in the workspace

    def test_non_colocated_repo_workspace(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "plain", colocate=False)
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo._workspace_add("ws", ws_path)
        assert not (ws_path / ".git").exists()

    def test_jj_and_git_agree_after_commit(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        ws_path = tmp_path / "ws"
        repo._workspace_add("ws", ws_path)
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
        repo._workspace_add("ws", ws_path)
        repo.workspace_forget("ws")
        shutil.rmtree(ws_path)
        repo.git_worktree_prune()
        assert _git(repo.cwd, "worktree", "list", "--porcelain").count("worktree ") == 1

    def test_workspace_names_lists_added_workspace(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        repo._workspace_add("ws", tmp_path / "ws")
        assert sorted(repo.workspace_names()) == ["default", "ws"]

    def test_forget_stale_workspaces_drops_only_prefixed(self, repo: JJ, tmp_path: Path) -> None:
        self._commit(repo, "a.txt", "initial")
        stale = f"{WORKSPACE_PREFIX}job"
        repo._workspace_add(stale, tmp_path / "stale")
        repo._workspace_add("mine", tmp_path / "mine")
        shutil.rmtree(tmp_path / "stale")

        repo.forget_stale_workspaces()

        assert sorted(repo.workspace_names()) == ["default", "mine"]
        # The dead git worktree of the forgotten workspace is pruned too.
        worktrees = _git(repo.cwd, "worktree", "list", "--porcelain")
        assert f"worktree {tmp_path / 'stale'}" not in worktrees
        assert f"worktree {tmp_path / 'mine'}" in worktrees
